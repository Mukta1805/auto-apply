"""Job-fit scoring with Claude.

Realtime mode: concurrent requests (bounded), the first request warms the prompt cache before the rest fan out.
Batch mode: Message Batches API at 50% of the price; results not ready in time are collected next run.
Both modes use structured outputs (JSON schema) so every result parses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..kb import KnowledgeBase
from .costs import TokenUsage, UsageTotals, compute_usage
from .prompts import OUTPUT_SCHEMA, job_message, system_blocks

log = logging.getLogger("jobagent.scoring")


@dataclass
class ScoreOutcome:
    scored: list[dict[str, Any]] = field(default_factory=list)      # rows for Store.save_scores
    errors: list[tuple[str, int]] = field(default_factory=list)     # (error, job_id)
    usage_rows: list[tuple] = field(default_factory=list)           # rows for Store.record_usage
    totals: UsageTotals = field(default_factory=UsageTotals)
    skipped_budget: int = 0
    pending_batch_id: str | None = None


def band_for(score: int, thresholds: dict) -> str:
    if score >= int(thresholds["high"]):
        return "High"
    if score >= int(thresholds["medium"]):
        return "Medium"
    return "Low"


def parse_result(job_id: int, message: Any, model: str, thresholds: dict) -> dict[str, Any]:
    """Turn a Message into a save_scores row. Raises ValueError if unusable."""
    stop = getattr(message, "stop_reason", None)
    if stop in ("max_tokens", "refusal"):
        raise ValueError(f"stop_reason={stop}")
    text = "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text").strip()
    if not text:
        raise ValueError("empty response")
    data = json.loads(text)
    score = max(0, min(100, int(data["score"])))
    if data.get("sponsorship") == "blocked":
        score = min(score, 20)
    if data.get("location_ok") is False:
        score = min(score, 20)
    return {
        "job_id": job_id, "score": score, "band": band_for(score, thresholds), "track": data["track"],
        "reason": (data.get("reason") or "").strip()[:400],
        "matched_skills": json.dumps((data.get("matched_skills") or [])[:5], ensure_ascii=False),
        "gaps": json.dumps((data.get("gaps") or [])[:3], ensure_ascii=False),
        "seniority_fit": data.get("seniority_fit") or "unclear",
        "sponsorship": data.get("sponsorship") or "not_mentioned", "model": model,
    }


class Scorer:
    def __init__(self, client: Any, kb: KnowledgeBase, cfg: dict, pricing: dict, run_id: str,
                 budget_left_usd: float):
        self.client = client
        self.cfg = cfg
        self.pricing = pricing
        self.run_id = run_id
        self.model = cfg["model"]
        self.cache_ttl = cfg.get("cache_ttl", "1h")
        self.system = system_blocks(kb, self.cache_ttl)
        self.thresholds = cfg["thresholds"]
        self.budget_left = budget_left_usd

    def _params(self, job: dict) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(self.cfg.get("max_output_tokens", 700)),
            "system": self.system,
            "messages": [{"role": "user", "content": job_message(job, int(self.cfg["max_description_chars"]))}],
            "output_config": {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        }
        if self.cfg.get("effort"):
            params["output_config"]["effort"] = self.cfg["effort"]
        return params

    def _account(self, out: ScoreOutcome, job_id: int, usage: Any, batch: bool) -> TokenUsage:
        u = compute_usage(usage, self.model, self.pricing, batch=batch, cache_ttl=self.cache_ttl)
        out.totals.add(self.model, u)
        out.usage_rows.append((self.run_id, job_id, "score", self.model, int(batch), u.input_tokens, u.output_tokens,
                               u.cache_read_tokens, u.cache_write_tokens, u.cost_usd))
        return u

    # ---- realtime -----------------------------------------------------
    async def score_realtime(self, jobs: list[dict], progress: Callable[[int, int], None] | None = None) -> ScoreOutcome:
        out = ScoreOutcome()
        if not jobs:
            return out
        sem = asyncio.Semaphore(int(self.cfg.get("max_concurrency", 8)))
        done = 0

        async def one(job: dict) -> None:
            nonlocal done
            if out.totals.cost_usd >= self.budget_left:
                out.skipped_budget += 1
                return
            async with sem:
                try:
                    msg = await self.client.messages.create(**self._params(job))
                except Exception as exc:  # SDK already retried transient errors
                    log.warning("scoring failed for job %s (%s): %s", job["id"], job.get("title"), exc)
                    out.errors.append((f"{type(exc).__name__}: {exc}"[:300], job["id"]))
                    return
            self._account(out, job["id"], msg.usage, batch=False)
            try:
                out.scored.append(parse_result(job["id"], msg, self.model, self.thresholds))
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("unparseable score for job %s: %s", job["id"], exc)
                out.errors.append((f"parse: {exc}"[:300], job["id"]))
            done += 1
            if progress and (done % 25 == 0 or done == len(jobs)):
                progress(done, len(jobs))

        # Warm the prompt cache with one request, then fan out so the rest read from cache.
        await one(jobs[0])
        await asyncio.gather(*(one(j) for j in jobs[1:]))
        return out

    # ---- batch --------------------------------------------------------
    async def submit_batch(self, jobs: list[dict]) -> str:
        requests = [{"custom_id": f"job-{j['id']}", "params": self._params(j)} for j in jobs]
        batch = await self.client.messages.batches.create(requests=requests)
        log.info("submitted scoring batch %s with %d jobs", batch.id, len(requests))
        return batch.id

    async def wait_for_batch(self, batch_id: str, poll_s: int, max_wait_s: int,
                             sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> bool:
        waited = 0
        while True:
            batch = await self.client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                return True
            if waited >= max_wait_s:
                log.info("batch %s still processing after %ds; will collect next run", batch_id, waited)
                return False
            await sleep(poll_s)
            waited += poll_s

    async def collect_batch(self, batch_id: str) -> ScoreOutcome:
        out = ScoreOutcome()
        results = await self.client.messages.batches.results(batch_id)
        async for item in results:
            job_id = int(item.custom_id.split("-", 1)[1])
            res = item.result
            if res.type != "succeeded":
                detail = getattr(res, "error", None)
                out.errors.append((f"batch {res.type}: {detail}"[:300], job_id))
                continue
            self._account(out, job_id, res.message.usage, batch=True)
            try:
                out.scored.append(parse_result(job_id, res.message, self.model, self.thresholds))
            except (ValueError, KeyError, TypeError) as exc:
                out.errors.append((f"parse: {exc}"[:300], job_id))
        return out

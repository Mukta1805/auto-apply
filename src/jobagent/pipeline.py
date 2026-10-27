"""The daily run: sync companies -> read workbook edits -> fetch -> filter -> hydrate -> score -> export.

Each stage is timed and logged. A failing stage marks the run "partial" but never stops the
workbook from being written. Only one run can hold the lock at a time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import __version__
from .companies import SUPPORTED, load_companies
from .config import Config
from .db import Store, utcnow
from .export import build_workbook, read_user_edits
from .fetchers import FETCHERS
from .filters import JobFilter
from .http import HttpClient
from .kb import load_kb
from .logging_setup import end_run_log, stage, start_run_log
from .models import content_hash
from .scoring import Scorer

log = logging.getLogger("jobagent.pipeline")


@dataclass
class RunOptions:
    score: bool = True
    fetch: bool = True
    limit: int | None = None                 # max jobs to score this run
    companies: list[str] = field(default_factory=list)   # restrict fetch to these names (testing)


class RunLock:
    def __init__(self, path: Path, stale_hours: float):
        self.path = path
        self.stale_s = stale_hours * 3600

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.stale_s:
            log.warning("removing stale lock %s", self.path)
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError(f"Another run is in progress (lock file {self.path}). "
                               "If no run is active, delete the lock file.") from None
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {utcnow()}")
        return self

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


class Pipeline:
    def __init__(self, cfg: Config, store: Store, http: HttpClient, llm_factory: Callable[[], Any] | None,
                 options: RunOptions | None = None):
        self.cfg = cfg
        self.store = store
        self.http = http
        self.llm_factory = llm_factory
        self.opts = options or RunOptions()
        self.stats: dict[str, Any] = {"version": __version__}
        self.failed_stages: list[str] = []
        now = datetime.now()
        self.run_id = now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}"
        self.cost = 0.0

    # ---- orchestration ------------------------------------------------
    async def run(self) -> dict[str, Any]:
        run_log = start_run_log(self.cfg.path("logs_dir"), self.run_id)
        log.info("run %s started (version %s); log: %s", self.run_id, __version__, run_log)
        self.store.start_run(self.run_id)
        t0 = time.perf_counter()
        error = None
        try:
            await self._stage("companies", self.sync_companies)
            await self._stage("edits", self.import_edits)
            if self.opts.score:
                await self._stage("batches", self.collect_open_batches)
            if self.opts.fetch:
                await self._stage("fetch", self.fetch_all)
            await self._stage("filter", self.filter_jobs)
            await self._stage("hydrate", self.hydrate)
            await self._stage("filter2", self.filter_jobs)
            if self.opts.score:
                await self._stage("score", self.score)
            await self._stage("export", self.export)
            await self._stage("maintain", lambda st: st.update(self.store.maintenance()))
        except Exception as exc:  # unexpected: still record the run
            error = f"{type(exc).__name__}: {exc}"
            log.critical("run aborted: %s\n%s", error, traceback.format_exc())
        status = "failed" if error else ("partial" if self.failed_stages else "ok")
        self.stats["failed_stages"] = self.failed_stages
        self.stats["duration_s"] = round(time.perf_counter() - t0, 1)
        self.store.finish_run(self.run_id, status, self.stats, self.cost, error)
        log.info("run %s finished: status=%s in %.1fs, AI cost $%.4f, stats=%s",
                 self.run_id, status, self.stats["duration_s"], self.cost, self.stats)
        end_run_log()
        self.stats["status"] = status
        return self.stats

    async def _stage(self, name: str, fn) -> None:
        try:
            with stage(name, log) as s:
                result = fn(s)
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            self.failed_stages.append(name)   # already logged with traceback by stage()

    # ---- stages -------------------------------------------------------
    def sync_companies(self, s: dict) -> None:
        companies = load_companies(self.cfg.path("companies_csv"))
        s["companies"] = self.store.sync_companies(companies)
        s["fetchable"] = len(self.store.fetchable_companies(SUPPORTED))
        unknown = sum(1 for c in companies if c.ats == "unknown")
        if unknown:
            log.warning("%d companies have no job board detected yet; run `python -m jobagent detect`", unknown)

    def import_edits(self, s: dict) -> None:
        edits = read_user_edits(self.cfg.path("workbook"))
        s["rows_read"] = self.store.apply_user_statuses(edits) if edits else 0

    async def fetch_all(self, s: dict) -> None:
        companies = self.store.fetchable_companies(SUPPORTED)
        if self.opts.companies:
            wanted = {c.lower() for c in self.opts.companies}
            companies = [c for c in companies if c["name"].lower() in wanted]
        counts = {"ok": 0, "not_modified": 0, "not_found": 0, "error": 0}
        totals = {"new": 0, "changed": 0, "unchanged": 0, "closed": 0, "seen": 0}

        async def one(c):
            fetcher = FETCHERS[c["ats"]]
            return c, await fetcher.fetch(self.http, c["name"], c["slug"], c["etag"], c["last_modified"])

        # Write each board to the database as soon as it arrives (network and DB work overlap).
        for fut in asyncio.as_completed([one(c) for c in companies]):
            c, res = await fut
            counts[res.status] += 1
            now = utcnow()
            if res.status == "ok":
                r = self.store.upsert_jobs(c["id"], res.jobs, now)
                for k in r:
                    totals[k] += r[k]
                totals["seen"] += len(res.jobs)
                self.store.record_company_fetch(c["id"], "ok", len(res.jobs), res.etag, res.last_modified)
                log.debug("%s (%s): %d jobs, %d new, %d changed, %d closed in %dms", c["name"], c["ats"],
                          len(res.jobs), r["new"], r["changed"], r["closed"], res.elapsed_ms)
            elif res.status == "not_modified":
                self.store.touch_company_jobs(c["id"], now)
                self.store.record_company_fetch(c["id"], "not_modified", None, res.etag, res.last_modified)
            else:
                self.store.record_company_fetch(c["id"], res.status, None, None, None)
                log.warning("%s (%s/%s): %s", c["name"], c["ats"], c["slug"], res.error)
        s.update(companies=len(companies), **counts, jobs_seen=totals["seen"], jobs_new=totals["new"],
                 jobs_changed=totals["changed"], jobs_closed=totals["closed"], http=dict(self.http.stats))
        self.stats.update(companies_ok=counts["ok"] + counts["not_modified"], companies_failed=counts["error"],
                          boards_not_found=counts["not_found"], jobs_seen=totals["seen"], jobs_new=totals["new"],
                          jobs_changed=totals["changed"])

    def filter_jobs(self, s: dict) -> None:
        kb = load_kb(self.cfg.path("kb_dir"))
        jf = JobFilter(self.cfg.data["filters"], avoid_companies=kb.avoid_companies,
                       applied_keys=kb.already_applied,
                       detail_ats=[a for a, f in FETCHERS.items() if f.needs_detail])
        rows = self.store.jobs_to_filter()
        decisions = [(jf.decide(dict(r)), r["id"]) for r in rows]
        self.store.save_filter_results([d.as_row(jid) for d, jid in decisions])
        tally: dict[str, int] = {}
        reasons: dict[str, int] = {}
        for d, _ in decisions:
            tally[d.status] = tally.get(d.status, 0) + 1
            if d.status == "excluded":
                key = d.reason.split(":")[0].split("(")[0].strip()
                reasons[key] = reasons.get(key, 0) + 1
        s.update(checked=len(rows), **tally)
        if reasons:
            log.info("exclusion reasons: %s", dict(sorted(reasons.items(), key=lambda kv: -kv[1])))
        agg = self.stats.setdefault("filter", {})
        for k, v in tally.items():
            agg[k] = agg.get(k, 0) + v

    async def hydrate(self, s: dict) -> None:
        rows = self.store.jobs_needing_detail()
        if not rows:
            s["jobs"] = 0
            return
        sem = asyncio.Semaphore(int(self.cfg.fetch.get("hydrate_concurrency", 8)))

        async def one(r):
            async with sem:
                return r, await FETCHERS[r["ats"]].hydrate(self.http, r["company_slug"], r["external_id"])

        updates, failed = [], 0
        for r, res in await asyncio.gather(*(one(r) for r in rows)):
            if res is None:
                failed += 1
                continue
            desc, apply_url = res
            updates.append((desc, apply_url, content_hash(r["title"], r["location"], desc, r["salary"]), r["id"]))
        self.store.save_descriptions(updates)
        s.update(jobs=len(rows), hydrated=len(updates), failed=failed)

    def _budget_left(self) -> float:
        tz = ZoneInfo(self.cfg.run.get("timezone", "America/New_York"))
        local_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        spent = self.store.spend_since(local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds"))
        return max(0.0, float(self.cfg.budget["max_daily_usd"]) - spent)

    def _scorer(self):
        if self.llm_factory is None:
            return None
        kb = load_kb(self.cfg.path("kb_dir"))
        return Scorer(self.llm_factory(), kb, self.cfg.data["scoring"], self.cfg.data["pricing"], self.run_id,
                      self._budget_left())

    def _save_outcome(self, out) -> None:
        self.store.save_scores(out.scored)
        if out.errors:
            self.store.save_score_errors(out.errors)
        if out.usage_rows:
            self.store.record_usage(out.usage_rows)
        self.cost += out.totals.cost_usd
        bands: dict[str, int] = {}
        for r in out.scored:
            bands[r["band"]] = bands.get(r["band"], 0) + 1
        self.stats["scored"] = self.stats.get("scored", 0) + len(out.scored)
        self.stats["score_errors"] = self.stats.get("score_errors", 0) + len(out.errors)
        for b, n in bands.items():
            self.stats[f"new_{b.lower()}"] = self.stats.get(f"new_{b.lower()}", 0) + n
        tokens = self.stats.setdefault("tokens", {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        tokens["input"] += out.totals.input_tokens
        tokens["output"] += out.totals.output_tokens
        tokens["cache_read"] += out.totals.cache_read_tokens
        tokens["cache_write"] += out.totals.cache_write_tokens

    async def collect_open_batches(self, s: dict) -> None:
        batches = self.store.open_batches()
        s["open"] = len(batches)
        if not batches:
            return
        scorer = self._scorer()
        if scorer is None:
            log.warning("open batches exist but no API key is configured")
            return
        for b in batches:
            done = await scorer.wait_for_batch(b["batch_id"], 0, 0)
            if not done:
                continue
            out = await scorer.collect_batch(b["batch_id"])
            self._save_outcome(out)
            self.store.close_batch(b["batch_id"], "collected")
            log.info("collected batch %s: %d scored, %d errors", b["batch_id"], len(out.scored), len(out.errors))

    async def score(self, s: dict) -> None:
        scorer = self._scorer()
        if scorer is None:
            log.warning("ANTHROPIC_API_KEY not set: skipping scoring")
            s["skipped"] = "no api key"
            return
        cfg = self.cfg.scoring
        limit = min(self.opts.limit or 10**9, int(cfg["max_jobs_per_run"]))
        jobs = [dict(r) for r in self.store.jobs_to_score(limit)]
        backlog = int(self.store.scalar(
            "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND filter_status='pass' "
            "AND (scored_hash IS NULL OR scored_hash<>content_hash)") or 0)
        s.update(to_score=len(jobs), backlog=backlog, budget_left=round(scorer.budget_left, 2), mode=cfg["mode"])
        if not jobs:
            return
        if scorer.budget_left <= 0:
            log.warning("daily AI budget reached ($%s); scoring skipped", self.cfg.budget["max_daily_usd"])
            return
        if cfg["mode"] == "batch":
            batch_id = await scorer.submit_batch(jobs)
            self.store.add_pending_batch(batch_id, self.run_id, "score", scorer.model, [j["id"] for j in jobs])
            done = await scorer.wait_for_batch(batch_id, int(cfg["batch_poll_seconds"]),
                                               int(cfg["batch_max_wait_minutes"]) * 60)
            if done:
                out = await scorer.collect_batch(batch_id)
                self.store.close_batch(batch_id, "collected")
            else:
                s["pending_batch"] = batch_id
                return
        else:
            out = await scorer.score_realtime(jobs, progress=lambda d, n: log.info("scored %d/%d", d, n))
        self._save_outcome(out)
        s.update(scored=len(out.scored), errors=len(out.errors), skipped_budget=out.skipped_budget,
                 **out.totals.as_dict())

    def export(self, s: dict) -> None:
        self.store.update_run_progress(self.run_id, self.stats, self.cost)   # so the dashboard shows this run
        path = build_workbook(self.store, self.cfg.path("workbook"), self.cfg.data["workbook"], self.stats)
        s["path"] = str(path)
        self.stats["workbook"] = str(path)

"""Auto-apply: choose today's jobs, fill each form, and submit (or stop before submit in dry-run mode).

Outcomes per job (jobs.apply_status):
  applied   submitted and a confirmation was seen (or no confirmation; flagged "verify" in apply_error)
  dry_run   form filled and screenshotted, not submitted
  manual    the tool can't finish it (CAPTCHA challenge, custom upload, sensitive question...): Manual tab
  failed    unexpected error; retried once on a later run
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..db import Store, utcnow
from .answers import Answers
from .browser import FormDriver, human_pause, wait_for_outcome
from .documents import write_cover_letter
from .mapper import Action, FieldMapper, summarize

log = logging.getLogger("jobagent.apply")
DONE_USER_STATUSES = ("skip", "rejected", "applied (manual)", "interviewing", "offer")


@dataclass
class ApplyResult:
    status: str                       # applied | dry_run | manual | failed
    reason: str = ""
    screenshot: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _local_midnight_utc(tz: str) -> str:
    now = datetime.now(ZoneInfo(tz))
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")


def select_jobs(store: Store, cfg: dict, run_cfg: dict, dry_run: bool) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Return (jobs to auto-apply now, [(job, reason)] for the Manual tab)."""
    target = run_cfg.get("daily_application_target", {"min": 25, "max": 50})
    split = run_cfg.get("track_split", {"business": 0.6, "research": 0.4})
    auto_boards = set(cfg.get("auto_boards", ["greenhouse", "lever", "ashby"]))
    since_today = _local_midnight_utc(run_cfg.get("timezone", "America/New_York"))
    applied_today = int(store.scalar("SELECT COUNT(*) FROM jobs WHERE apply_status='applied' AND applied_at>=?",
                                     (since_today,)) or 0)
    remaining = max(0, int(target["max"]) - applied_today)

    retry_states = "(j.apply_status IS NULL OR (j.apply_status='failed' AND COALESCE(j.apply_attempts,0) < 2)"
    retry_states += ")" if dry_run else " OR j.apply_status='dry_run')"
    ph = ",".join("?" * len(DONE_USER_STATUSES))
    rows = [dict(r) for r in store.query(
        f"""SELECT j.*, c.priority FROM jobs j JOIN companies c ON c.id=j.company_id
            WHERE j.is_active=1 AND (j.band IN ('High','Medium') OR j.user_status='apply')
              AND COALESCE(j.user_status,'') NOT IN ({ph})
              AND COALESCE(j.sponsorship_flag,'') <> 'blocked' AND {retry_states}
            ORDER BY (j.user_status='apply') DESC, c.priority, j.score DESC, COALESCE(j.posted_at, j.first_seen_at) DESC""",
        DONE_USER_STATUSES)]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    per_company = {r["company_id"]: r["n"] for r in store.query(
        "SELECT company_id, COUNT(*) n FROM jobs WHERE apply_status='applied' AND applied_at>=? GROUP BY company_id",
        (cutoff,))}
    cap = int(cfg.get("max_per_company_30d", 2))
    quotas = {t: round(remaining * float(share)) for t, share in split.items()}
    auto: list[dict] = []
    manual: list[tuple[dict, str]] = []
    overflow: list[dict] = []
    for r in rows:
        if r["ats"] not in auto_boards:
            if r.get("apply_status") is None:
                manual.append((r, f"{r['ats']} forms are not auto-filled yet"))
            continue
        if per_company.get(r["company_id"], 0) >= cap:
            continue
        track = r.get("track") or "business"
        if quotas.get(track, 0) > 0 and len(auto) < remaining:
            auto.append(r)
            quotas[track] -= 1
            per_company[r["company_id"]] = per_company.get(r["company_id"], 0) + 1
        else:
            overflow.append(r)
    # Unused quota of one track goes to the other track's next-best jobs.
    for r in overflow:
        if len(auto) >= remaining:
            break
        if per_company.get(r["company_id"], 0) < cap:
            auto.append(r)
            per_company[r["company_id"]] = per_company.get(r["company_id"], 0) + 1
    if len(auto) + applied_today < int(target["min"]):
        log.info("only %d jobs eligible for auto-apply today (minimum target %s); the bar is not lowered",
                 len(auto) + applied_today, target["min"])
    return auto, manual


class ApplyEngine:
    def __init__(self, store: Store, cfg: dict, run_cfg: dict, answers: Answers, kb_dir: Path, out_dir: Path,
                 driver_factory: Callable[[], Any], writer: Any | None, run_id: str):
        self.store = store
        self.cfg = cfg
        self.run_cfg = run_cfg
        self.answers = answers
        self.kb_dir = kb_dir
        self.shots = out_dir / "screenshots"
        self.letters = out_dir / "cover_letters"
        self.driver_factory = driver_factory
        self.writer = writer
        self.run_id = run_id
        self.dry_run = bool(cfg.get("dry_run", True))

    def resume_for(self, track: str | None) -> Path:
        name = "resume_research.pdf" if track == "research" else "resume_business.pdf"
        return self.kb_dir / "resumes" / name

    async def run(self, s: dict) -> dict[str, Any]:
        missing = self.answers.missing()
        if missing:
            log.error("auto-apply skipped: kb/answers.yaml is missing %s", ", ".join(missing))
            s["skipped"] = f"answers.yaml incomplete ({len(missing)} values)"
            return s
        auto, manual = select_jobs(self.store, self.cfg, self.run_cfg, self.dry_run)
        for job, reason in manual:
            self._record(job, ApplyResult("manual", reason))
        s.update(selected=len(auto), to_manual_unsupported=len(manual), mode="dry run" if self.dry_run else "submit")
        if not auto:
            return s
        tally: dict[str, int] = {}
        deadline = time.monotonic() + float(self.cfg.get("max_minutes_per_run", 150)) * 60
        async with self.driver_factory() as driver:
            for i, job in enumerate(auto):
                if time.monotonic() > deadline:
                    log.warning("apply time budget reached after %d jobs; the rest wait for the next run", i)
                    break
                try:
                    res = await self.apply_one(driver, job)
                except Exception as exc:                     # one bad page must not stop the run
                    log.exception("apply crashed for job %s (%s)", job["id"], job["title"])
                    res = ApplyResult("failed", f"{type(exc).__name__}: {exc}"[:300])
                self._record(job, res)
                tally[res.status] = tally.get(res.status, 0) + 1
                log.info("[%d/%d] %s - %s: %s %s", i + 1, len(auto), job["company"], job["title"], res.status,
                         f"({res.reason})" if res.reason else "")
                if i < len(auto) - 1 and res.status in ("applied", "dry_run"):
                    await driver.pause(human_pause(self.cfg.get("pause_seconds", [45, 120])))
        if self.writer is not None and self.writer.usage_rows:
            self.store.record_usage(self.writer.usage_rows)
            self.writer.usage_rows = []
        s.update(tally)
        return s

    async def apply_one(self, driver: FormDriver, job: dict) -> ApplyResult:
        url = job.get("apply_url") or job.get("url")
        if not url:
            return ApplyResult("manual", "no application URL")
        await driver.goto(url)
        form = await driver.extract()
        fields = form.get("fields") or []
        if len(fields) < 3 or not form.get("submit"):
            return ApplyResult("manual", "application form not found on the page (external careers site?)")
        visible = [c for c in form.get("captcha") or [] if not c.get("invisible")]
        if visible:
            return ApplyResult("manual", "visible CAPTCHA on the form")

        mapper = FieldMapper(self.answers, self.resume_for(job.get("track")), self.writer is not None,
                             bool(self.answers.get("cover_letter.auto_write", True)))
        plan = mapper.plan(form)
        if plan.ok and plan.writer_fields:
            answers = await self.writer.answer_questions(job, plan.writer_fields)
            mapper.add_writer_answers(plan, answers)
        if plan.ok and plan.cover_letter_field is not None:
            letter = await self.writer.cover_letter(job)
            if not letter:
                plan.manual_reasons.append("couldn't write a confident cover letter")
            else:
                path = write_cover_letter(letter, self.letters, f"{job['id']}_{job['company']}_cover_letter",
                                          plan.cover_letter_field.get("accept", ""))
                plan.actions.append(Action(plan.cover_letter_field["selector"], "upload", str(path),
                                           label="Cover letter", category="cover_letter_file", source="writer",
                                           required=True))
        details = {"plan": summarize(plan), "form_url": form.get("url"),
                   "answers": [{"label": a.label[:120], "value": a.value[:500] if a.kind != "upload" else Path(a.value).name,
                                "source": a.source} for a in plan.actions]}
        if not plan.ok:
            return ApplyResult("manual", "; ".join(plan.manual_reasons)[:400], details=details)

        failures, required_failures = [], []
        for a in plan.actions:
            try:
                await self._do(driver, a)
            except Exception as exc:
                failures.append(f"{a.label[:60]}: {type(exc).__name__}")
                if a.required:
                    required_failures.append(a.label[:60])
                    log.warning("required field failed on job %s: %s (%s)", job["id"], a.label, exc)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = self.shots / f"{job['id']}_{stamp}_filled.png"
        await driver.screenshot(shot)
        details["fill_failures"] = failures
        if required_failures:
            return ApplyResult("manual", "could not fill: " + "; ".join(required_failures)[:300], str(shot), details)
        if self.dry_run:
            return ApplyResult("dry_run", "filled, not submitted (dry run)", str(shot), details)

        await driver.click(form["submit"][-1]["selector"])
        outcome, detail = await wait_for_outcome(driver, float(self.cfg.get("submit_timeout_seconds", 35)))
        done_shot = self.shots / f"{job['id']}_{stamp}_after_submit.png"
        await driver.screenshot(done_shot)
        details["outcome"] = {"result": outcome, "detail": detail}
        if outcome == "confirmed":
            return ApplyResult("applied", "", str(done_shot), details)
        if outcome == "unknown":
            return ApplyResult("applied", "submitted; no confirmation seen - please verify", str(done_shot), details)
        return ApplyResult("manual", detail, str(done_shot), details)

    @staticmethod
    async def _do(driver: FormDriver, a: Action) -> None:
        if a.kind == "fill":
            await driver.fill(a.selector, a.value)
        elif a.kind == "select":
            await driver.select(a.selector, a.value)
        elif a.kind in ("radio", "checkbox"):
            await driver.check(a.option_selector or a.selector)
        elif a.kind == "upload":
            await driver.upload(a.selector, a.value)
        elif a.kind == "combobox":
            picked = await driver.combobox(a.selector, a.value)
            if picked is None:
                raise ValueError(f"no dropdown option matched {a.value!r}")

    def _record(self, job: dict, res: ApplyResult) -> None:
        applied_at = utcnow() if res.status == "applied" else None
        self.store.conn.execute(
            """UPDATE jobs SET apply_status=?, apply_error=?, apply_screenshot=COALESCE(NULLIF(?, ''), apply_screenshot),
               apply_method=?, applied_at=COALESCE(?, applied_at), apply_attempts=COALESCE(apply_attempts,0)+?,
               apply_details=? WHERE id=?""",
            (res.status, res.reason, res.screenshot, "auto" if res.status in ("applied", "dry_run") else "manual",
             applied_at, 1 if res.status in ("applied", "failed", "dry_run") else 0,
             json.dumps(res.details, ensure_ascii=False)[:20000], job["id"]))

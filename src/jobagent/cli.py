"""Command line: python -m jobagent <command>

  run         the daily pipeline (what the scheduled task calls)
  apply       fill (and optionally submit) applications for today's selected jobs
  outreach    Gmail sync, follow-ups and new emails only (the late-morning scheduled task)
  auth-gmail  one-time Gmail sign-in (opens Google's consent screen)
  inspect-form URL   show how one application form would be filled (nothing is typed or submitted)
  detect      find the job board (Greenhouse/Lever/...) for companies marked "unknown" in companies.csv
  export      rebuild the Excel workbook from the database only
  stats       print a summary of the database and recent runs
  doctor      check setup: config, knowledge base, keys, database, network
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import sys
from pathlib import Path

from . import __version__
from .companies import detect_boards, load_companies, save_companies
from .config import Config, load_config
from .db import Store
from .export import build_workbook
from .http import HttpClient
from .kb import load_kb
from .logging_setup import setup_logging
from .pipeline import Pipeline, RunLock, RunOptions

log = logging.getLogger("jobagent.cli")


def _http(cfg: Config) -> HttpClient:
    f = cfg.fetch
    contact = cfg.secret("CONTACT_EMAIL")
    ua = f"riya-job-agent/{__version__} (personal job search{'; ' + contact if contact else ''})"
    return HttpClient(max_concurrency=int(f["max_concurrency"]), per_host=int(f["per_host_concurrency"]),
                      timeout=float(f["timeout_seconds"]), max_retries=int(f["max_retries"]), user_agent=ua,
                      host_limits=dict(f.get("host_limits") or {}))


def _llm_factory(cfg: Config):
    key = cfg.secret("ANTHROPIC_API_KEY")
    if not key:
        return None

    def make():
        import anthropic
        return anthropic.AsyncAnthropic(api_key=key, max_retries=4, timeout=90.0)

    return make


async def cmd_run(cfg: Config, args, opts: RunOptions | None = None) -> int:
    opts = opts or RunOptions(score=not args.no_score, fetch=not args.no_fetch, limit=args.limit,
                              companies=args.company or [])
    store = Store(cfg.path("db"))
    try:
        with RunLock(cfg.path("db").with_name("run.lock"), float(cfg.run.get("stale_lock_hours", 6))):
            async with _http(cfg) as http:
                stats = await Pipeline(cfg, store, http, _llm_factory(cfg), opts).run()
    finally:
        store.close()
    print(json.dumps({k: stats.get(k) for k in ("status", "duration_s", "jobs_seen", "jobs_new", "scored",
                                                "new_high", "new_medium", "apply", "outreach", "workbook")
                      if stats.get(k) is not None}, indent=2, default=str))
    return {"ok": 0, "partial": 1}.get(stats.get("status"), 2)


async def cmd_apply(cfg: Config, args) -> int:
    dry = True if args.dry_run else (False if args.submit else None)
    return await cmd_run(cfg, args, RunOptions(fetch=False, score=False, apply=True, outreach=False, dry_run=dry,
                                               apply_limit=args.limit))


async def cmd_inspect_form(cfg: Config, args) -> int:
    """Open one application page and print how each field would be filled. Nothing is typed or submitted."""
    from .apply.answers import load_answers
    from .apply.browser import PlaywrightDriver
    from .apply.mapper import FieldMapper
    answers = load_answers(cfg.path("kb_dir") / "answers.yaml")
    resume = cfg.path("kb_dir") / "resumes" / f"resume_{args.track}.pdf"
    acfg = cfg.data["apply"]
    async with PlaywrightDriver(cfg.path("browser_profile"), headless=bool(acfg.get("headless", False)),
                                executable_path=acfg.get("browser_executable") or None) as d:
        await d.goto(args.url)
        form = await d.extract()
    plan = FieldMapper(answers, resume, writer_available=True).plan(form)
    print(f"{len(form.get('fields', []))} fields, submit button: {bool(form.get('submit'))}, captcha: {form.get('captcha')}")
    for a in plan.actions:
        print(f"  {a.kind:9s} {a.label[:60]:60s} <- {('[file] ' + Path(a.value).name) if a.kind == 'upload' else a.value[:60]}")
    for f in plan.writer_fields:
        print(f"  {'claude':9s} {f.get('label', '')[:60]:60s} <- written per job")
    if plan.cover_letter_field:
        print("  upload    Cover letter                                                 <- written per job")
    for r in plan.manual_reasons:
        print(f"  MANUAL: {r}")
    return 0


async def cmd_outreach(cfg: Config, args) -> int:
    return await cmd_run(cfg, args, RunOptions(only_outreach=True, outreach=True))


async def cmd_auth_gmail(cfg: Config, args) -> int:
    from .apply.answers import load_answers
    from .outreach.gmail import GmailClient, authorize_interactive
    creds = cfg.path("credentials_dir")
    secret = creds / "client_secret.json"
    if not secret.exists():
        print(f"Missing {secret}. Download the OAuth client (Desktop app) JSON from Google Cloud Console "
              "and save it there (see README, 'Connect Gmail').")
        return 1
    authorize_interactive(secret, creds / "gmail_token.json")
    async with _http(cfg) as http:
        email = await GmailClient(http, creds / "gmail_token.json").profile_email()
    expected = load_answers(cfg.path("kb_dir") / "answers.yaml").get("identity.email")
    print(f"Gmail connected: {email}")
    if expected and email.lower() != str(expected).lower():
        print(f"WARNING: answers.yaml identity.email is {expected}, but you signed in as {email}.")
        return 1
    return 0


async def cmd_detect(cfg: Config, args) -> int:
    path = cfg.path("companies_csv")
    companies = load_companies(path)
    async with _http(cfg) as http:
        stats = await detect_boards(http, companies, redetect=args.redetect)
    save_companies(path, companies)
    by_ats: dict[str, int] = {}
    for c in companies:
        by_ats[c.ats] = by_ats.get(c.ats, 0) + 1
    log.info("detection done: %s; companies by board: %s", stats, by_ats)
    print(json.dumps({"detection": stats, "by_board": by_ats}, indent=2))
    return 0


def cmd_export(cfg: Config, args) -> int:
    store = Store(cfg.path("db"))
    try:
        path = build_workbook(store, cfg.path("workbook"), {**cfg.data["workbook"],
                              "follow_up_days": cfg.data["outreach"].get("follow_up_days", [4, 9])})
    finally:
        store.close()
    print(f"Workbook written: {path}")
    return 0


def cmd_stats(cfg: Config, args) -> int:
    store = Store(cfg.path("db"))
    try:
        out = {
            "companies_by_board": {r["ats"]: r["n"] for r in store.query(
                "SELECT ats, COUNT(*) n FROM companies WHERE active=1 GROUP BY ats")},
            "active_jobs": store.scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1"),
            "by_filter": {r["s"] or "pending": r["n"] for r in store.query(
                "SELECT filter_status s, COUNT(*) n FROM jobs WHERE is_active=1 GROUP BY s")},
            "by_band": {f"{r['track']}/{r['band']}": r["n"] for r in store.query(
                "SELECT track, band, COUNT(*) n FROM jobs WHERE is_active=1 AND band IS NOT NULL GROUP BY track, band")},
            "ai_spend_total_usd": round(store.scalar("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage") or 0, 4),
            "recent_runs": [dict(r) for r in store.query(
                "SELECT run_id, status, cost_usd, error FROM runs ORDER BY started_at DESC LIMIT 5")],
        }
    finally:
        store.close()
    print(json.dumps(out, indent=2, default=str))
    return 0


async def cmd_doctor(cfg: Config, args) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python >= 3.11", sys.version_info >= (3, 11), platform.python_version()))
    try:
        kb = load_kb(cfg.path("kb_dir"))
        checks.append(("knowledge base", True, f"{len(kb.business) + len(kb.research) + len(kb.candidate)} chars"))
    except FileNotFoundError as exc:
        checks.append(("knowledge base", False, str(exc)))
    answers = cfg.path("kb_dir") / "answers.yaml"
    checks.append(("kb/answers.yaml", answers.exists(), "needed for auto-apply"))
    for name in ("resume_business.pdf", "resume_research.pdf"):
        checks.append((f"kb/resumes/{name}", (cfg.path("kb_dir") / "resumes" / name).exists(), ""))
    checks.append(("ANTHROPIC_API_KEY set", bool(cfg.secret("ANTHROPIC_API_KEY")), "in .env"))
    from .apply.answers import RECOMMENDED_KEYS, load_answers
    answers = load_answers(cfg.path("kb_dir") / "answers.yaml")
    missing = answers.missing()
    checks.append(("answers.yaml complete (auto-apply)", not missing,
                   ("missing: " + ", ".join(missing)) if missing else "ok"))
    soft = answers.missing(RECOMMENDED_KEYS)
    if soft:
        checks.append(("answers.yaml recommended", True, "empty (optional): " + ", ".join(soft)))
    import importlib.util
    has_pw = importlib.util.find_spec("playwright") is not None
    checks.append(("Playwright installed (auto-apply)", has_pw,
                   "" if has_pw else "pip install playwright && python -m playwright install chromium"))
    checks.append(("APOLLO_API_KEY set (outreach)", bool(cfg.secret("APOLLO_API_KEY")), "in .env"))
    token = cfg.path("credentials_dir") / "gmail_token.json"
    checks.append(("Gmail connected (outreach)", token.exists(), "run: python -m jobagent auth-gmail"))
    checks.append(("apply.enabled / dry_run", True, f"{cfg.apply.get('enabled')} / {cfg.apply.get('dry_run')}"))
    checks.append(("outreach.enabled / mode", True, f"{cfg.outreach.get('enabled')} / {cfg.outreach.get('mode')}"))
    try:
        companies = load_companies(cfg.path("companies_csv"))
        unknown = sum(1 for c in companies if c.ats == "unknown")
        checks.append(("companies.csv", True, f"{len(companies)} companies, {unknown} not yet detected"))
    except Exception as exc:
        checks.append(("companies.csv", False, str(exc)))
    try:
        Store(cfg.path("db")).close()
        checks.append(("database writable", True, str(cfg.path("db"))))
    except Exception as exc:
        checks.append(("database writable", False, str(exc)))
    if args.online:
        async with _http(cfg) as http:
            probes = {"greenhouse": "https://boards-api.greenhouse.io/v1/boards/benchling/jobs",
                      "lever": "https://api.lever.co/v0/postings/lever?mode=json&limit=1",
                      "ashby": "https://api.ashbyhq.com/posting-api/job-board/ashby"}
            for name, url in probes.items():
                res = await http.get_json(url, expect_404=True)
                checks.append((f"reach {name}", res.status in (200, 404), res.error or f"HTTP {res.status}"))
        factory = _llm_factory(cfg)
        if factory:
            try:
                client = factory()
                await client.models.retrieve(cfg.scoring["model"])
                checks.append(("Anthropic API key works", True, cfg.scoring["model"]))
            except Exception as exc:
                checks.append(("Anthropic API key works", False, f"{type(exc).__name__}: {exc}"[:200]))
    width = max(len(c[0]) for c in checks)
    ok = True
    optional = ("kb/answers.yaml", "answers.yaml complete (auto-apply)", "Playwright installed (auto-apply)",
                "APOLLO_API_KEY set (outreach)", "Gmail connected (outreach)")
    for name, passed, detail in checks:
        ok &= passed or name in optional
        print(f"  [{'OK ' if passed else 'XX '}] {name.ljust(width)}  {detail}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobagent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="run the daily pipeline")
    p_run.add_argument("--no-score", action="store_true", help="skip AI scoring (no API cost)")
    p_run.add_argument("--no-fetch", action="store_true", help="skip fetching; re-filter/score what is stored")
    p_run.add_argument("--limit", type=int, help="score at most N jobs this run")
    p_run.add_argument("--company", action="append", help="only fetch this company (repeatable)")
    p_app = sub.add_parser("apply", help="apply to today's selected jobs (no fetching or scoring)")
    p_app.add_argument("--limit", type=int, help="at most N applications this run")
    g = p_app.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="fill forms but don't submit (overrides config)")
    g.add_argument("--submit", action="store_true", help="really submit (overrides config dry_run)")
    sub.add_parser("outreach", help="Gmail sync, follow-ups and new emails only")
    p_ins = sub.add_parser("inspect-form", help="show how a job's application form would be filled (no typing)")
    p_ins.add_argument("url")
    p_ins.add_argument("--track", choices=["business", "research"], default="business")
    sub.add_parser("auth-gmail", help="one-time Gmail sign-in")
    p_det = sub.add_parser("detect", help="detect job boards for companies marked unknown")
    p_det.add_argument("--redetect", action="store_true", help="also retry companies marked unsupported")
    sub.add_parser("export", help="rebuild the workbook from the database")
    sub.add_parser("stats", help="print database summary")
    p_doc = sub.add_parser("doctor", help="check setup")
    p_doc.add_argument("--online", action="store_true", help="also test network and API key")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    lc = cfg.logging
    setup_logging(cfg.path("logs_dir"), lc.get("level", "INFO"), lc.get("console_level", "INFO"),
                  int(lc.get("retention_days", 30)), bool(lc.get("json_log", True)))
    log.debug("command %s on %s, python %s", args.command, platform.platform(), platform.python_version())
    try:
        if args.command == "run":
            return asyncio.run(cmd_run(cfg, args))
        if args.command == "apply":
            return asyncio.run(cmd_apply(cfg, args))
        if args.command == "inspect-form":
            return asyncio.run(cmd_inspect_form(cfg, args))
        if args.command == "outreach":
            return asyncio.run(cmd_outreach(cfg, args))
        if args.command == "auth-gmail":
            return asyncio.run(cmd_auth_gmail(cfg, args))
        if args.command == "detect":
            return asyncio.run(cmd_detect(cfg, args))
        if args.command == "export":
            return cmd_export(cfg, args)
        if args.command == "stats":
            return cmd_stats(cfg, args)
        if args.command == "doctor":
            return asyncio.run(cmd_doctor(cfg, args))
    except RuntimeError as exc:
        log.error("%s", exc)
        return 3
    except Exception:
        log.exception("unhandled error in %s", args.command)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

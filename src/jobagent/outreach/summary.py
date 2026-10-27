"""A short daily summary email to Riya herself (sent once a day, after the morning run)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..db import Store, utcnow
from .gmail import GmailClient, build_message

log = logging.getLogger("jobagent.summary")


def compose_summary(store: Store, stats: dict, tz: str) -> tuple[str, str]:
    today = datetime.now(ZoneInfo(tz))
    start = today.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")
    top = store.query("""SELECT company, title, score, track FROM jobs WHERE is_active=1 AND band='High'
                         AND first_seen_at>=? ORDER BY score DESC LIMIT 8""", (start,))
    applied = store.scalar("SELECT COUNT(*) FROM jobs WHERE apply_status='applied' AND applied_at>=?", (start,)) or 0
    manual = store.scalar("SELECT COUNT(*) FROM jobs WHERE apply_status='manual' AND COALESCE(user_status,'')=''") or 0
    drafts = store.scalar("SELECT COUNT(*) FROM emails WHERE status='drafted' AND kind<>'summary'") or 0
    sent = store.scalar("SELECT COUNT(*) FROM emails WHERE status='sent' AND sent_at>=? AND kind<>'summary'", (start,)) or 0
    replies = store.scalar("SELECT COUNT(*) FROM emails WHERE replied_at>=?", (start,)) or 0
    lines = [f"Good morning! Here's {today.strftime('%A, %b %d')}:", "",
             f"- New High matches: {stats.get('new_high', 0)} (Medium: {stats.get('new_medium', 0)})",
             f"- Applied today: {applied}",
             f"- Waiting for you in the Manual tab: {manual}",
             f"- Emails sent today: {sent}; drafts waiting in Gmail: {drafts}",
             f"- New replies: {replies}"]
    if top:
        lines += ["", "Top new matches:"]
        lines += [f"  {r['score']}  {r['company']} - {r['title']} ({(r['track'] or '').title()})" for r in top]
    if stats.get("failed_stages"):
        lines += ["", f"Note: these steps had problems today: {', '.join(stats['failed_stages'])} (see logs/errors.log)."]
    lines += ["", "Open output/riya_job_tracker.xlsx for the full list.", "", "- your job agent"]
    subject = f"Job search update: {applied} applied, {stats.get('new_high', 0)} new High matches"
    return subject, "\n".join(lines)


async def send_summary(store: Store, gmail: GmailClient, sender: str, stats: dict, tz: str) -> bool:
    start = datetime.now(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    if store.scalar("SELECT 1 FROM emails WHERE kind='summary' AND created_at>=?",
                    (start.astimezone(timezone.utc).isoformat(timespec="seconds"),)):
        return False
    subject, body = compose_summary(store, stats, tz)
    raw, rfc = build_message(sender, "Job agent", sender, subject, body)
    res = await gmail.send(raw)
    store.conn.execute("""INSERT INTO emails(kind, status, subject, body, gmail_message_id, gmail_thread_id,
                          rfc_message_id, created_at, sent_at) VALUES('summary','sent',?,?,?,?,?,?,?)""",
                       (subject, body, res.get("message_id"), res.get("thread_id"), rfc, utcnow(), utcnow()))
    return True

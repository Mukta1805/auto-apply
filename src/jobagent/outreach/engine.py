"""Outreach: contacts for applied jobs, first emails, follow-ups, and reply/bounce tracking.

Order each run:
  1. contacts   find one verified contact per newly applied job (Apollo); warm connections are flagged instead
  2. sync       Gmail threads: drafts Riya sent herself, replies, bounces (replies stop all follow-ups)
  3. follow-ups day 4 and day 9 after the first email, in the same thread, only if nobody replied
  4. new        first emails for new contacts, within the daily cap (drafts in "draft" mode)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time as dtime, timedelta, timezone
from email.utils import parseaddr
from typing import Any
from zoneinfo import ZoneInfo

from ..db import Store, utcnow
from .apollo import TITLES, ApolloClient, rank_people
from .gmail import GmailClient, build_message
from .writer import OPT_OUT, EmailWriter, follow_up, signature

log = logging.getLogger("jobagent.outreach")
OPT_OUT_REPLY = re.compile(r"unsubscribe|not interested|stop (emailing|contacting)|remove me|do not contact", re.I)
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery", re.I)


class OutreachEngine:
    def __init__(self, store: Store, cfg: dict, run_cfg: dict, answers: Any, lists: dict, run_id: str,
                 apollo: ApolloClient | None, gmail: GmailClient | None, writer: EmailWriter | None):
        self.store = store
        self.cfg = cfg
        self.tz = ZoneInfo(run_cfg.get("timezone", "America/New_York"))
        self.answers = answers
        self.lists = lists or {}
        self.run_id = run_id
        self.apollo = apollo
        self.gmail = gmail
        self.writer = writer
        self.mode = cfg.get("mode", "draft")
        self.sender = (answers.get("identity.email") or "").lower()
        self.sender_name = answers.full_name
        self.sig = signature(self.sender_name, answers.get("identity.linkedin_url"), answers.get("identity.phone"))

    # ---- helpers --------------------------------------------------------------
    def _today_start_utc(self) -> str:
        now = datetime.now(self.tz)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")

    def in_send_window(self, now: datetime | None = None) -> bool:
        start, end = self.cfg.get("send_window", ["09:00", "17:30"])
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        if now.weekday() >= 5 and not self.cfg.get("send_on_weekends", False):
            return False
        h1, m1 = map(int, start.split(":"))
        h2, m2 = map(int, end.split(":"))
        return dtime(h1, m1) <= now.time() <= dtime(h2, m2)

    def suppressed(self, email: str) -> bool:
        e = (email or "").lower()
        if not e:
            return True
        if e in {x.lower() for x in self.lists.get("already_contacted") or []}:
            return True
        return bool(self.store.scalar("SELECT 1 FROM suppression WHERE email=?", (e,)))

    # ---- 1. contacts ------------------------------------------------------------
    async def find_contacts(self, s: dict) -> None:
        if self.apollo is None:
            s["contacts"] = "skipped (no APOLLO_API_KEY)"
            return
        cooldown = (datetime.now(timezone.utc) - timedelta(days=int(self.cfg.get("company_cooldown_days", 14)))
                    ).isoformat(timespec="seconds")
        jobs = [dict(r) for r in self.store.query(
            """SELECT j.*, c.domain AS company_domain, c.id AS cid FROM jobs j JOIN companies c ON c.id=j.company_id
               WHERE (j.apply_status='applied' OR j.user_status='applied (manual)')
                 AND NOT EXISTS (SELECT 1 FROM contacts k WHERE k.job_id=j.id)
               ORDER BY j.applied_at DESC LIMIT 200""")]
        warm = {str(w.get("company", "")).lower(): w for w in self.lists.get("warm_connections") or [] if w}
        max_reveals = int(self.cfg.get("max_reveals_per_day", 30))
        revealed_today = int(self.store.scalar(
            "SELECT COUNT(*) FROM contacts WHERE source LIKE 'apollo%' AND created_at>=?", (self._today_start_utc(),)) or 0)
        found = no_email = warm_n = cooled = 0
        for job in jobs:
            company = job["company"]
            if company.lower() in warm:
                w = warm[company.lower()]
                self._save_contact(job, None, w.get("name", ""), "", w.get("email", ""), "warm", "warm connection",
                                   title=w.get("relation", ""))
                warm_n += 1
                continue
            # One first email per company per cooldown: counts contacts already chosen (even earlier in this
            # same run) as well as emails, so two same-day applications don't email the same person twice.
            recent = self.store.scalar(
                """SELECT 1 FROM contacts k WHERE k.company_id=? AND k.email_status='verified' AND k.created_at>=?
                   UNION SELECT 1 FROM emails e JOIN contacts k ON k.id=e.contact_id
                   WHERE k.company_id=? AND e.kind='initial' AND e.created_at>=?""",
                (job["cid"], cooldown, job["cid"], cooldown))
            if recent:
                cooled += 1
                continue                       # one first email per company per cooldown window
            if revealed_today >= max_reveals:
                log.info("Apollo reveal cap reached (%d today); remaining contacts tomorrow", max_reveals)
                break
            domain = job.get("company_domain") or await self.apollo.find_domain(company)
            if not domain:
                self._save_contact(job, None, "", "", "", "none", "apollo: company domain not found")
                no_email += 1
                continue
            if not job.get("company_domain"):
                self.store.conn.execute("UPDATE companies SET domain=? WHERE id=?", (domain, job["cid"]))
            track = job.get("track") or "business"
            people = rank_people(await self.apollo.search_people(domain, TITLES[track] + TITLES["recruiting"]), job)
            chosen = None
            for p in people[:3]:
                if not p.has_email:
                    continue
                p = await self.apollo.reveal(p)
                revealed_today += 1
                if p.email and p.email_status == "verified" and not self.suppressed(p.email):
                    chosen = p
                    break
            if chosen:
                self._save_contact(job, chosen.apollo_id, chosen.name, chosen.first_name, chosen.email, "verified",
                                   "apollo", title=chosen.title, linkedin=chosen.linkedin_url)
                found += 1
            else:
                self._save_contact(job, None, "", "", "", "none", "apollo: no verified email")
                no_email += 1
        s.update(contacts_found=found, contacts_none=no_email, warm=warm_n, company_cooldown=cooled)

    def _save_contact(self, job, apollo_id, name, first, email, status, source, title="", linkedin="") -> None:
        self.store.conn.execute(
            """INSERT INTO contacts(company_id, job_id, apollo_id, name, first_name, title, email, email_status,
               linkedin_url, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (job["company_id"], job["id"], apollo_id, name, first or (name.split(" ")[0] if name else ""), title,
             (email or "").lower(), status, linkedin, source, utcnow()))

    # ---- 2. sync ----------------------------------------------------------------
    async def sync_threads(self, s: dict) -> None:
        since = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
        rows = self.store.query(
            """SELECT * FROM emails WHERE gmail_thread_id IS NOT NULL AND status IN ('drafted','sent')
               AND replied_at IS NULL AND bounced=0 AND created_at>=? AND kind<>'summary'""", (since,))
        seen_threads: dict[str, list[dict]] = {}
        handled: set[str] = set()              # reply/bounce is per thread, not per email in it
        sent_n = replied = bounced = 0
        for e in rows:
            tid = e["gmail_thread_id"]
            if tid not in seen_threads:
                seen_threads[tid] = await self.gmail.thread(tid)
            msgs = seen_threads[tid]
            if e["status"] == "drafted":
                mine = [m for m in msgs if "SENT" in m["labels"]]
                if mine:
                    ts = datetime.fromtimestamp(int(mine[0]["internal_date"]) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                    self.store.conn.execute("UPDATE emails SET status='sent', sent_at=?, rfc_message_id=? WHERE id=?",
                                            (ts, mine[0]["rfc_id"], e["id"]))
                    sent_n += 1
                elif not msgs:
                    self.store.conn.execute("UPDATE emails SET status='skipped', error='draft deleted' WHERE id=?", (e["id"],))
            if tid in handled:
                continue
            for m in msgs:
                addr = parseaddr(m["from"])[1].lower()
                if not addr or addr == self.sender or "SENT" in m["labels"]:
                    continue
                if BOUNCE_FROM.search(m["from"]):
                    self.store.conn.execute("UPDATE emails SET bounced=1 WHERE gmail_thread_id=?", (tid,))
                    to = self.store.scalar("SELECT k.email FROM contacts k WHERE k.id=?", (e["contact_id"],))
                    if to:
                        self.store.conn.execute("INSERT OR IGNORE INTO suppression VALUES(?,?,?)", (to, "bounced", utcnow()))
                    bounced += 1
                    handled.add(tid)
                else:
                    ts = datetime.fromtimestamp(int(m["internal_date"]) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                    self.store.conn.execute("UPDATE emails SET replied_at=? WHERE gmail_thread_id=?", (ts, tid))
                    if OPT_OUT_REPLY.search(m.get("snippet", "")):
                        self.store.conn.execute("INSERT OR IGNORE INTO suppression VALUES(?,?,?)", (addr, "opted out", utcnow()))
                    replied += 1
                    handled.add(tid)
                break
        s.update(drafts_sent_by_riya=sent_n, replies=replied, bounces=bounced)

    # ---- 3. follow-ups ----------------------------------------------------------
    async def send_follow_ups(self, s: dict) -> None:
        days = [int(d) for d in self.cfg.get("follow_up_days", [4, 9])]
        cap = int(self.cfg.get("max_follow_ups_per_day", 40))
        done_today = int(self.store.scalar("SELECT COUNT(*) FROM emails WHERE kind LIKE 'follow_up%' AND created_at>=?",
                                           (self._today_start_utc(),)) or 0)
        now = datetime.now(timezone.utc)
        initials = self.store.query(
            """SELECT e.*, k.email AS to_email, k.first_name, j.title, j.company, j.track, j.id AS jid
               FROM emails e JOIN contacts k ON k.id=e.contact_id JOIN jobs j ON j.id=e.job_id
               WHERE e.kind='initial' AND e.status='sent' AND e.replied_at IS NULL AND e.bounced=0""")
        sent = 0
        for e in initials:
            if done_today + sent >= cap:
                break
            if self.suppressed(e["to_email"]):
                continue
            prior = {r["kind"]: r for r in self.store.query(
                "SELECT * FROM emails WHERE job_id=? AND kind LIKE 'follow_up%' AND status IN ('sent','drafted')", (e["jid"],))}
            first_sent = datetime.fromisoformat(e["sent_at"])
            for n, d in enumerate(days, start=1):
                kind = f"follow_up_{n}"
                if kind in prior:
                    continue
                if n > 1 and prior.get(f"follow_up_{n - 1}", {"status": None})["status"] != "sent":
                    break
                if now < first_sent + timedelta(days=d):
                    break
                _, body = follow_up(n, e["first_name"] or "", dict(e), self.sig)
                subject = e["subject"] if e["subject"].lower().startswith("re:") else f"Re: {e['subject']}"
                await self._deliver(e["jid"], e["contact_id"], kind, e["to_email"], subject, body,
                                    thread_id=e["gmail_thread_id"], in_reply_to=e["rfc_message_id"])
                sent += 1
                break
        s["follow_ups"] = sent

    # ---- 4. first emails -------------------------------------------------------
    async def send_new(self, s: dict) -> None:
        if self.writer is None:
            s["new_emails"] = "skipped (no ANTHROPIC_API_KEY)"
            return
        cap = int(self.cfg.get("max_new_emails_per_day", 30))
        done_today = int(self.store.scalar("SELECT COUNT(*) FROM emails WHERE kind='initial' AND created_at>=?",
                                           (self._today_start_utc(),)) or 0)
        rows = [dict(r) for r in self.store.query(
            """SELECT k.id AS contact_id, k.email, k.first_name, k.title AS contact_title, j.*
               FROM contacts k JOIN jobs j ON j.id=k.job_id
               WHERE k.email_status='verified' AND k.email<>''
                 AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.contact_id=k.id)
                 AND NOT EXISTS (SELECT 1 FROM emails e JOIN contacts k2 ON k2.id=e.contact_id
                                 WHERE k2.email=k.email AND e.kind='initial' AND e.status<>'failed')
               ORDER BY j.score DESC""")]
        made = 0
        for r in rows:
            if done_today + made >= cap:
                log.info("daily cap of %d new emails reached", cap)
                break
            if self.suppressed(r["email"]):
                continue
            text = await self.writer.initial(r, r["first_name"], r["contact_title"])
            if text is None:
                self.store.conn.execute("""INSERT INTO emails(job_id, contact_id, kind, status, created_at, error)
                                           VALUES(?,?, 'initial', 'skipped', ?, 'writer not confident')""",
                                        (r["id"], r["contact_id"], utcnow()))
                continue
            subject, body = text
            body = f"{body}\n\n{OPT_OUT}\n\nBest,\n{self.sig}"
            await self._deliver(r["id"], r["contact_id"], "initial", r["email"], subject, body)
            made += 1
        if self.writer.usage_rows:
            self.store.record_usage(self.writer.usage_rows)
            self.writer.usage_rows = []
        s["new_emails"] = made

    async def _deliver(self, job_id, contact_id, kind, to, subject, body, thread_id=None, in_reply_to=None) -> None:
        raw, rfc_id = build_message(self.sender, self.sender_name, to, subject, body, in_reply_to)
        try:
            if self.mode == "send":
                res = await self.gmail.send(raw, thread_id)
                status, draft_id, sent_at = "sent", None, utcnow()
            else:
                res = await self.gmail.create_draft(raw, thread_id)
                status, draft_id, sent_at = "drafted", res.get("draft_id"), None
            self.store.conn.execute(
                """INSERT INTO emails(job_id, contact_id, kind, status, subject, body, gmail_draft_id, gmail_message_id,
                   gmail_thread_id, rfc_message_id, created_at, sent_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, contact_id, kind, status, subject, body, draft_id, res.get("message_id"), res.get("thread_id"),
                 rfc_id, utcnow(), sent_at))
            log.info("%s %s to %s for job %s", "sent" if status == "sent" else "drafted", kind, to, job_id)
        except Exception as exc:
            log.warning("%s to %s failed: %s", kind, to, exc)
            self.store.conn.execute(
                """INSERT INTO emails(job_id, contact_id, kind, status, subject, body, created_at, error)
                   VALUES(?,?,?, 'failed', ?,?,?,?)""", (job_id, contact_id, kind, subject, body, utcnow(), str(exc)[:300]))

    # ---- orchestration ----------------------------------------------------------
    async def run(self, s: dict) -> dict:
        await self.find_contacts(s)
        if self.gmail is None or not self.gmail.configured:
            s["email"] = "skipped (Gmail not connected: run `python -m jobagent auth-gmail`)"
            return s
        if not self.sender:
            s["email"] = "skipped (identity.email missing in answers.yaml)"
            return s
        await self.sync_threads(s)
        if self.mode == "send" and not self.in_send_window():
            s["sending"] = "outside the send window; follow-ups and new emails wait for the next outreach run"
            return s
        await self.send_follow_ups(s)
        await self.send_new(s)
        return s

"""Outreach with Apollo and Gmail mocked at the HTTP level (the real request code runs)."""

import base64
import json
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from jobagent.config import load_config
from jobagent.db import Store, utcnow
from jobagent.http import HttpClient
from jobagent.models import Company, Job
from jobagent.outreach.apollo import ApolloClient, Person, rank_people
from jobagent.outreach.engine import OutreachEngine
from jobagent.outreach.gmail import GmailClient
from jobagent.outreach.summary import send_summary

from .test_apply_engine import full_answers

APOLLO = "https://api.apollo.io/api/v1"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class FakeEmailWriter:
    def __init__(self):
        self.usage_rows = []

    async def initial(self, job, first, title):
        return f"Applied: {job['title']}", f"Hi {first},\n\nI just applied for the {job['title']} role at {job['company']}."


def decode(raw: str):
    return message_from_bytes(base64.urlsafe_b64decode(raw.encode()))


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "t.db")
    store.sync_companies([Company(name="Acme Bio", ats="greenhouse", slug="acme")])
    cid = store.scalar("SELECT id FROM companies")
    store.upsert_jobs(cid, [Job(ats="greenhouse", company="Acme Bio", company_slug="acme", external_id="1",
                                title="Business Development Associate", url="u", department="Business Development",
                                description="Join our BD team.")], utcnow())
    store.conn.execute("UPDATE jobs SET apply_status='applied', applied_at=?, track='business', score=88", (utcnow(),))
    token = tmp_path / "gmail_token.json"
    token.write_text(json.dumps({"token": "old", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                 "token_uri": TOKEN_URL, "expiry": "2000-01-01T00:00:00Z"}))
    cfg = load_config()
    yield store, cfg, token
    store.close()


def mock_apollo(email_status="verified"):
    respx.post(f"{APOLLO}/mixed_companies/search").mock(return_value=httpx.Response(200, json={
        "organizations": [{"name": "Acme Bio", "primary_domain": "acmebio.com"}]}))
    people = respx.post(f"{APOLLO}/mixed_people/api_search").mock(return_value=httpx.Response(200, json={"people": [
        {"id": "p2", "first_name": "Rob", "last_name_obfuscated": "K***", "title": "Technical Recruiter", "has_email": True},
        {"id": "p3", "first_name": "Sam", "title": "Software Engineer", "has_email": False},
        {"id": "p1", "first_name": "Dana", "last_name_obfuscated": "S***", "title": "Head of Business Development", "has_email": True},
    ]}))
    reveal = respx.post(f"{APOLLO}/people/match").mock(return_value=httpx.Response(200, json={"person": {
        "first_name": "Dana", "last_name": "Smith", "email": "dana@acmebio.com", "email_status": email_status}}))
    return people, reveal


def mock_gmail(thread_messages=None):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600}))
    drafts = respx.post(f"{GMAIL}/drafts").mock(return_value=httpx.Response(200, json={
        "id": "d1", "message": {"id": "m1", "threadId": "t1"}}))
    send = respx.post(f"{GMAIL}/messages/send").mock(return_value=httpx.Response(200, json={"id": "m2", "threadId": "t1"}))
    thread = respx.get(f"{GMAIL}/threads/t1").mock(return_value=httpx.Response(200, json={"messages": thread_messages or []}))
    return drafts, send, thread


def msg(frm, labels, days_ago=0, snippet="", rfc="<orig@gmail.com>"):
    ts = int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000)
    return {"id": f"x{ts}", "labelIds": labels, "internalDate": str(ts), "snippet": snippet,
            "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Message-ID", "value": rfc}]}}


def make_engine(store, cfg, http, token, mode="draft", **over):
    ocfg = {**cfg.data["outreach"], "mode": mode, **over}
    lists = {"already_contacted": [], "warm_connections": []}
    return OutreachEngine(store, ocfg, cfg.data["run"], full_answers(), lists, "run1",
                          ApolloClient(http, "key"), GmailClient(http, token), FakeEmailWriter())


@respx.mock
async def test_contact_found_ranked_and_draft_created(world):
    store, cfg, token = world
    people, reveal = mock_apollo()
    drafts, send, _ = mock_gmail()
    async with HttpClient(max_retries=0) as http:
        s = await make_engine(store, cfg, http, token).run({})
    assert s["contacts_found"] == 1 and s["new_emails"] == 1
    assert reveal.call_count == 1 and "id=p1" in str(reveal.calls[0].request.url)       # BD head ranked above recruiter
    assert people.calls[0].request.headers["X-Api-Key"] == "key"
    assert "acmebio.com" in str(people.calls[0].request.url)
    contact = store.query("SELECT * FROM contacts")[0]
    assert contact["email"] == "dana@acmebio.com" and contact["name"] == "Dana Smith"
    assert store.scalar("SELECT domain FROM companies") == "acmebio.com"                   # cached for next time
    body = json.loads(drafts.calls[0].request.content)
    m = decode(body["message"]["raw"])
    assert m["To"] == "dana@acmebio.com" and m["Subject"] == "Applied: Business Development Associate"
    text = m.get_payload()
    assert "won't follow up" in text and "Riya Sancheti" in text and "linkedin.com/in/example" in text
    assert send.call_count == 0                                                            # draft mode never sends
    assert store.query("SELECT status FROM emails")[0]["status"] == "drafted"
    assert json.loads(token.read_text())["token"] == "new"                                 # expired token refreshed


@respx.mock
async def test_unverified_email_is_not_used(world):
    store, cfg, token = world
    mock_apollo(email_status="unverified")
    drafts, *_ = mock_gmail()
    async with HttpClient(max_retries=0) as http:
        s = await make_engine(store, cfg, http, token).run({})
    assert s["contacts_none"] == 1 and drafts.call_count == 0
    assert store.query("SELECT email_status FROM contacts")[0]["email_status"] == "none"


@respx.mock
async def test_riya_sends_draft_then_reply_stops_follow_ups(world):
    store, cfg, token = world
    mock_apollo()
    mock_gmail()
    async with HttpClient(max_retries=0) as http:
        await make_engine(store, cfg, http, token).run({})                  # creates the draft
    respx.reset()
    _, send, thread = mock_gmail([msg("Riya Sancheti <riya@example.com>", ["SENT"], days_ago=5)])
    async with HttpClient(max_retries=0) as http:
        eng = make_engine(store, cfg, http, token, mode="send")
        eng.in_send_window = lambda now=None: True
        s = await eng.run({})
    assert s["drafts_sent_by_riya"] == 1 and s["follow_ups"] == 1                       # 5 days old -> follow-up 1
    fu = decode(json.loads(send.calls[0].request.content)["raw"])
    assert fu["In-Reply-To"] == "<orig@gmail.com>" and fu["Subject"].startswith("Re: ")
    assert json.loads(send.calls[0].request.content)["threadId"] == "t1"
    respx.reset()
    _, send, _ = mock_gmail([msg("Riya <riya@example.com>", ["SENT"], 10),
                             msg("Dana Smith <dana@acmebio.com>", ["INBOX"], 1, "Thanks Riya, let's talk")])
    async with HttpClient(max_retries=0) as http:
        eng = make_engine(store, cfg, http, token, mode="send")
        eng.in_send_window = lambda now=None: True
        s = await eng.run({})
    assert s["replies"] == 1 and s["follow_ups"] == 0 and send.call_count == 0
    assert store.scalar("SELECT COUNT(*) FROM emails WHERE replied_at IS NOT NULL") >= 1


@respx.mock
async def test_bounce_and_opt_out_add_suppression(world):
    store, cfg, token = world
    mock_apollo()
    mock_gmail()
    async with HttpClient(max_retries=0) as http:
        await make_engine(store, cfg, http, token).run({})
    respx.reset()
    mock_gmail([msg("Riya <riya@example.com>", ["SENT"], 2),
                msg("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", ["INBOX"], 1)])
    async with HttpClient(max_retries=0) as http:
        s = await make_engine(store, cfg, http, token).run({})
    assert s["bounces"] == 1
    assert store.scalar("SELECT reason FROM suppression WHERE email='dana@acmebio.com'") == "bounced"


@respx.mock
async def test_one_first_email_per_company_per_cooldown(world):
    store, cfg, token = world
    mock_apollo()
    drafts, *_ = mock_gmail()
    async with HttpClient(max_retries=0) as http:
        await make_engine(store, cfg, http, token).run({})
        cid = store.scalar("SELECT id FROM companies")
        store.upsert_jobs(cid, [Job(ats="greenhouse", company="Acme Bio", company_slug="acme", external_id="2",
                                    title="Strategy Analyst", url="u2")], utcnow())
        store.conn.execute("UPDATE jobs SET apply_status='applied', applied_at=?, track='business' WHERE external_id='2'",
                           (utcnow(),))
        s = await make_engine(store, cfg, http, token).run({})
    assert s["company_cooldown"] == 1 and drafts.call_count == 1


@respx.mock
async def test_warm_connection_flagged_instead_of_cold_email(world):
    store, cfg, token = world
    people, _ = mock_apollo()
    mock_gmail()
    async with HttpClient(max_retries=0) as http:
        eng = make_engine(store, cfg, http, token)
        eng.lists = {"warm_connections": [{"company": "Acme Bio", "name": "Priya N", "relation": "Nucleate"}]}
        s = await eng.run({})
    assert s["warm"] == 1 and people.call_count == 0
    assert store.query("SELECT email_status FROM contacts")[0]["email_status"] == "warm"


def test_send_window():
    cfg = load_config()
    eng = OutreachEngine(None, {**cfg.data["outreach"]}, cfg.data["run"], full_answers(), {}, "r", None, None, None)
    ny = ZoneInfo("America/New_York")
    assert eng.in_send_window(datetime(2026, 10, 28, 10, 30, tzinfo=ny))          # Wednesday morning
    assert not eng.in_send_window(datetime(2026, 10, 28, 6, 0, tzinfo=ny))        # before 9:00
    assert not eng.in_send_window(datetime(2026, 10, 31, 11, 0, tzinfo=ny))       # Saturday


def test_rank_people_prefers_function_match():
    job = {"track": "research", "title": "Process Development Associate", "department": "Process Development"}
    ppl = [Person("1", "A", "A", "Talent Acquisition Partner", True), Person("2", "B", "B", "Director, Process Development", True),
           Person("3", "C", "C", "Head of Research", False)]
    assert [p.apollo_id for p in rank_people(ppl, job)][0] == "2"


@respx.mock
async def test_daily_summary_sent_once(world):
    store, cfg, token = world
    _, send, _ = mock_gmail()
    async with HttpClient(max_retries=0) as http:
        gmail = GmailClient(http, token)
        assert await send_summary(store, gmail, "riya@example.com", {"new_high": 3}, "America/New_York")
        assert not await send_summary(store, gmail, "riya@example.com", {"new_high": 3}, "America/New_York")
    m = decode(json.loads(send.calls[0].request.content)["raw"])
    assert m["To"] == "riya@example.com" and "Job search update" in m["Subject"]

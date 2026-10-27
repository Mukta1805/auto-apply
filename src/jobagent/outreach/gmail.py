"""Gmail API client (drafts, send, threads) using Riya's own Google account.

One-time setup: `python -m jobagent auth-gmail` opens Google's consent screen in her browser and stores a
refresh token in credentials/gmail_token.json (git-ignored). After that the token is refreshed silently.
Scopes: gmail.compose (drafts and sending) and gmail.readonly (to notice replies and bounces).
Sends are never retried automatically, so an email can't go out twice.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any

from ..http import HttpClient

log = logging.getLogger("jobagent.gmail")
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"]


def authorize_interactive(client_secret: Path, token_path: Path) -> str:
    """Run Google's installed-app consent flow (opens the browser). Returns the authorized address."""
    from google_auth_oauthlib.flow import InstalledAppFlow    # only needed for this one-time step
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  authorization_prompt_message="A browser window will open to connect Gmail.")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return "authorized"


def build_message(from_addr: str, from_name: str, to: str, subject: str, body: str,
                  in_reply_to: str | None = None) -> tuple[str, str]:
    """Returns (base64url raw MIME, RFC Message-ID)."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to
    msg["Subject"] = subject
    domain = from_addr.split("@")[-1] if "@" in from_addr else "localhost"
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return raw, message_id


class GmailClient:
    def __init__(self, http: HttpClient, token_path: Path):
        self.http = http
        self.token_path = token_path
        self._token: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return self.token_path.exists()

    async def _auth(self) -> dict[str, str]:
        if self._token is None:
            self._token = json.loads(self.token_path.read_text(encoding="utf-8"))
        tok = self._token
        expiry = tok.get("expiry")
        fresh = False
        if expiry:
            try:
                exp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                fresh = exp - datetime.now(timezone.utc) > timedelta(seconds=90)
            except ValueError:
                fresh = False
        if not (tok.get("token") and fresh):
            res = await self.http.request_json(
                "POST", tok.get("token_uri", "https://oauth2.googleapis.com/token"), retry_post=True,
                data={"client_id": tok["client_id"], "client_secret": tok["client_secret"],
                      "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
            if not res.ok:
                raise RuntimeError(f"Gmail token refresh failed ({res.error}); run `python -m jobagent auth-gmail`")
            tok["token"] = res.data["access_token"]
            tok["expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=int(res.data.get("expires_in", 3600)))
                             ).isoformat()
            self.token_path.write_text(json.dumps(tok), encoding="utf-8")
        return {"Authorization": f"Bearer {tok['token']}"}

    async def _call(self, method: str, path: str, **kw) -> dict:
        res = await self.http.request_json(method, f"{API}{path}", headers=await self._auth(), **kw)
        if not res.ok:
            raise RuntimeError(f"Gmail {method} {path} failed: {res.error}")
        return res.data or {}

    async def profile_email(self) -> str:
        return (await self._call("GET", "/profile")).get("emailAddress", "")

    async def create_draft(self, raw: str, thread_id: str | None = None) -> dict[str, str]:
        msg: dict[str, Any] = {"raw": raw}
        if thread_id:
            msg["threadId"] = thread_id
        d = await self._call("POST", "/drafts", json_body={"message": msg})
        return {"draft_id": d.get("id", ""), "message_id": (d.get("message") or {}).get("id", ""),
                "thread_id": (d.get("message") or {}).get("threadId", "")}

    async def send(self, raw: str, thread_id: str | None = None) -> dict[str, str]:
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        d = await self._call("POST", "/messages/send", json_body=body)
        return {"message_id": d.get("id", ""), "thread_id": d.get("threadId", "")}

    async def thread(self, thread_id: str) -> list[dict]:
        """Messages in a thread as [{from, message_id, labels, internal_date}]; [] if the thread is gone."""
        res = await self.http.request_json(
            "GET", f"{API}/threads/{thread_id}", headers=await self._auth(), expect_404=True,
            params={"format": "metadata", "metadataHeaders": ["From", "Message-ID", "Subject"]})
        if res.status == 404:
            return []
        if not res.ok:
            raise RuntimeError(f"Gmail thread {thread_id} failed: {res.error}")
        out = []
        for m in (res.data or {}).get("messages") or []:
            headers = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers") or []}
            out.append({"id": m.get("id"), "from": headers.get("from", ""), "rfc_id": headers.get("message-id", ""),
                        "labels": m.get("labelIds") or [], "internal_date": m.get("internalDate"),
                        "snippet": m.get("snippet", "")})
        return out

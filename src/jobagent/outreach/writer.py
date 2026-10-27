"""Outreach email text.

The first email is written by Claude from Riya's profile and the job; follow-ups are short fixed templates
(cheaper, predictable). Every email ends with the same plain opt-out line.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..kb import KnowledgeBase
from ..scoring.costs import compute_usage
from ..text import truncate

log = logging.getLogger("jobagent.outreach.writer")

OPT_OUT = "If you're not the right person or this isn't a good time, just let me know and I won't follow up."

EMAIL_RULES = """You write one short cold email from Riya Sancheti to a person at a company where she has just
applied. Use ONLY facts from her profile; never invent achievements, numbers or connections.
Requirements:
- 90-130 words in the body, plain text, no bullet points, no links.
- Open with the role she applied for. Then ONE specific, relevant result from her profile that matches the job.
- One sentence on why this company specifically, based only on the job description.
- Close with a light ask: a 15-minute conversation, or a pointer to the right person.
- Address the recipient by first name. No flattery, no "I hope this email finds you well".
- Subject: under 9 words, specific (e.g. "Applied: Process Development Associate - GLP-1 purification").
- Do not include a signature or an opt-out line; they are added automatically.
Return confident=false if the profile has nothing relevant to say about this job."""

EMAIL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["subject", "body", "confident"],
                "properties": {"subject": {"type": "string"}, "body": {"type": "string"},
                               "confident": {"type": "boolean"}}}

STRENGTH = {"business": "GTM and commercial strategy experience in biotech",
            "research": "hands-on bioprocess and lab experience"}


def signature(name: str, linkedin: str, phone: str) -> str:
    """Name and contact lines (the sign-off word is added by each email)."""
    lines = [name]
    if phone:
        lines.append(phone)
    if linkedin:
        lines.append(linkedin)
    return "\n".join(lines)


def follow_up(n: int, first_name: str, job: dict, sig: str) -> tuple[str, str]:
    """Returns (subject_prefix, body). Subject reuses the original thread subject with "Re:"."""
    hi = f"Hi {first_name}," if first_name else "Hi,"
    strength = STRENGTH.get(job.get("track") or "business", STRENGTH["business"])
    if n == 1:
        body = (f"{hi}\n\nJust bumping this up in case it got buried. I'd love to learn more about the "
                f"{job['title']} role and how my {strength} could help {job['company']}. "
                f"Happy to work around your schedule.\n\n{OPT_OUT}\n\nBest,\n{sig}")
    else:
        body = (f"{hi}\n\nI'll close the loop here. If the {job['title']} role isn't the right fit or timing, no worries "
                f"at all. If there's someone else I should speak with, I'd be grateful for a pointer.\n\n"
                f"Thank you,\n{sig}")
    return "Re:", body


class EmailWriter:
    def __init__(self, client: Any, kb: KnowledgeBase, cfg: dict, pricing: dict, run_id: str):
        self.client = client
        self.model = cfg.get("model", "claude-sonnet-5-5")
        self.cfg = cfg
        self.pricing = pricing
        self.run_id = run_id
        self.usage_rows: list[tuple] = []
        self.system = [{"type": "text", "cache_control": {"type": "ephemeral", "ttl": "1h"}, "text": "\n\n".join([
            EMAIL_RULES, "<candidate>\n" + kb.candidate + "\n</candidate>",
            "<business_track>\n" + kb.business + "\n</business_track>",
            "<research_track>\n" + kb.research + "\n</research_track>"])}]

    async def initial(self, job: dict, contact_first: str, contact_title: str) -> tuple[str, str] | None:
        user = (f"<job>\ncompany: {job['company']}\ntitle: {job['title']}\ntrack: {job.get('track')}\n"
                f"description:\n{truncate(job.get('description') or '', 5000)}\n</job>\n"
                f"<recipient>first_name: {contact_first or '(unknown, use Hi there)'}\ntitle: {contact_title}</recipient>")
        params: dict[str, Any] = {"model": self.model, "max_tokens": 800, "system": self.system,
                                  "messages": [{"role": "user", "content": user}],
                                  "output_config": {"format": {"type": "json_schema", "schema": EMAIL_SCHEMA}}}
        if self.cfg.get("effort"):
            params["output_config"]["effort"] = self.cfg["effort"]
        try:
            msg = await self.client.messages.create(**params)
        except Exception as exc:
            log.warning("email writing failed for job %s: %s", job["id"], exc)
            return None
        u = compute_usage(msg.usage, self.model, self.pricing, cache_ttl="1h")
        self.usage_rows.append((self.run_id, job["id"], "email", self.model, 0, u.input_tokens, u.output_tokens,
                                u.cache_read_tokens, u.cache_write_tokens, u.cost_usd))
        try:
            data = json.loads("".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"))
        except ValueError:
            return None
        if not data.get("confident") or not data.get("body", "").strip():
            return None
        return data["subject"].strip()[:120], data["body"].strip()

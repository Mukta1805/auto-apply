"""Claude-written answers for custom application questions and cover letters.

Only facts from Riya's knowledge base are allowed. Any question the model cannot answer truthfully comes
back with confident=false, and a required one sends the job to the Manual tab.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..kb import KnowledgeBase
from ..scoring.costs import compute_usage
from ..text import truncate

log = logging.getLogger("jobagent.writer")

WRITER_RULES = """You fill in job-application questions for Riya Sancheti, using ONLY facts from her profile below.
Never invent experience, numbers, tools, publications, links or employers. If a question cannot be answered
truthfully from the profile, return confident=false with an empty answer.

Style: first person, specific, warm and concise. Default to 60-120 words for open questions, one sentence for
short factual ones. Tie her real experience to this company and role. No buzzword lists, no flattery, no
"I am excited to apply" openers. Never mention AI or that this text was generated.
For multiple-choice questions, `option` must be copied exactly from the given options.
Respect max_chars when given."""

ANSWERS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answers"],
    "properties": {"answers": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["field_id", "answer", "option", "confident"],
        "properties": {"field_id": {"type": "string"}, "answer": {"type": "string"},
                       "option": {"type": "string"}, "confident": {"type": "boolean"}}}}},
}
LETTER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["letter", "confident"],
    "properties": {"letter": {"type": "string"}, "confident": {"type": "boolean"}},
}


class AnswerWriter:
    def __init__(self, client: Any, kb: KnowledgeBase, answers_summary: str, cfg: dict, pricing: dict, run_id: str):
        self.client = client
        self.cfg = cfg
        self.model = cfg.get("model", "claude-sonnet-5-5")
        self.pricing = pricing
        self.run_id = run_id
        self.usage_rows: list[tuple] = []
        self.cost = 0.0
        self.system = [{"type": "text", "cache_control": {"type": "ephemeral", "ttl": "1h"}, "text": "\n\n".join([
            WRITER_RULES, "<candidate>\n" + kb.candidate + "\n</candidate>",
            "<business_track>\n" + kb.business + "\n</business_track>",
            "<research_track>\n" + kb.research + "\n</research_track>",
            "<logistics>\n" + answers_summary + "\n</logistics>"])}]

    async def _call(self, job_id: int, purpose: str, user: str, schema: dict, max_tokens: int) -> dict | None:
        params: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens, "system": self.system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if self.cfg.get("effort"):
            params["output_config"]["effort"] = self.cfg["effort"]
        try:
            msg = await self.client.messages.create(**params)
        except Exception as exc:
            log.warning("%s failed for job %s: %s", purpose, job_id, exc)
            return None
        u = compute_usage(msg.usage, self.model, self.pricing, cache_ttl="1h")
        self.cost += u.cost_usd
        self.usage_rows.append((self.run_id, job_id, purpose, self.model, 0, u.input_tokens, u.output_tokens,
                                u.cache_read_tokens, u.cache_write_tokens, u.cost_usd))
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        try:
            return json.loads(text)
        except ValueError:
            log.warning("%s for job %s returned unparseable output", purpose, job_id)
            return None

    @staticmethod
    def job_context(job: dict, max_chars: int = 6000) -> str:
        return (f"<job>\ncompany: {job.get('company')}\ntitle: {job.get('title')}\ntrack: {job.get('track')}\n"
                f"location: {job.get('location')}\ndescription:\n{truncate(job.get('description') or '', max_chars)}\n</job>")

    async def answer_questions(self, job: dict, fields: list[dict]) -> dict[str, dict]:
        if not fields:
            return {}
        questions = [{"field_id": f["selector"], "question": f.get("label", ""), "type": f.get("type"),
                      "required": bool(f.get("required")),
                      "options": [o.get("text") for o in f.get("options") or []] or None,
                      "max_chars": f.get("maxlength")} for f in fields]
        user = (self.job_context(job) + "\n<questions>\n" + json.dumps(questions, ensure_ascii=False, indent=1) +
                "\n</questions>\nAnswer every question. Use the field_id values exactly.")
        data = await self._call(job["id"], "answers", user, ANSWERS_SCHEMA, 2000)
        if not data:
            return {}
        return {a["field_id"]: a for a in data.get("answers", [])}

    async def cover_letter(self, job: dict) -> str | None:
        user = (self.job_context(job) + "\nWrite a cover letter for this role: 200-280 words, three short "
                "paragraphs, addressed 'Dear Hiring Team,' and signed with her full name from the profile. "
                "Plain text only.")
        data = await self._call(job["id"], "cover_letter", user, LETTER_SCHEMA, 1200)
        if not data or not data.get("confident") or not data.get("letter", "").strip():
            return None
        return data["letter"].strip()

"""Prompts and output schema for job-fit scoring.

The system prompt (rubric + Riya's full profile) is identical for every job, so it is
prompt-cached: after the first request it is billed at the cache-read rate.
"""

from __future__ import annotations

import json

from ..kb import KnowledgeBase
from ..text import truncate

RUBRIC = """You screen job postings for one candidate, Riya Sancheti, and score how well each posting fits her.
She is applying on two tracks at once: BUSINESS (strategy, GTM, BD, consulting, product, commercial) and
RESEARCH (R&D, process/analytical development, lab, organ-on-chip, research associate). Pick the track the
posting belongs to and score it against that track's profile.

Score 0-100 for how worthwhile an application is:
- 85-100: one of her target roles, entry level (0-2 years), strong overlap with her skills and domain.
- 75-84: clearly relevant role and level; some gaps she can credibly bridge.
- 50-74: plausible stretch (adjacent function, or asks a bit more experience than she has).
- 25-49: weak fit (wrong function, needs 3+ years, or far outside life sciences / healthcare).
- 0-24: not a fit (senior, unrelated field, non-US, or explicitly no visa sponsorship).

Rules:
- She is early career. Postings requiring 3+ years of full-time experience score at most 55; 5+ years at most 35.
- She needs visa sponsorship (OPT now, H-1B later). If the posting says no sponsorship, US citizens only,
  or security clearance required, set sponsorship="blocked" and score at most 20.
  If it says sponsorship is available, set sponsorship="positive". Otherwise "not_mentioned".
- location_ok is false only if the role must be performed outside the United States.
- Judge only from the posting text and her profile. Never assume experience she does not have.
- reason: one specific sentence, at most 25 words, naming the main match and the main gap.
- matched_skills: up to 5 short items from her profile that this posting asks for.
- gaps: up to 3 short items the posting wants that she lacks (empty list if none).
- seniority_fit: entry | mid | senior | unclear, describing what the posting asks for."""

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["track", "score", "reason", "matched_skills", "gaps", "seniority_fit", "sponsorship", "location_ok"],
    "properties": {
        "track": {"type": "string", "enum": ["business", "research"]},
        "score": {"type": "integer"},
        "reason": {"type": "string"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "seniority_fit": {"type": "string", "enum": ["entry", "mid", "senior", "unclear"]},
        "sponsorship": {"type": "string", "enum": ["blocked", "positive", "not_mentioned"]},
        "location_ok": {"type": "boolean"},
    },
}


def system_blocks(kb: KnowledgeBase, cache_ttl: str = "1h") -> list[dict]:
    text = "\n\n".join([
        RUBRIC,
        "<candidate>\n" + kb.candidate + "\n</candidate>",
        "<business_track>\n" + kb.business + "\n</business_track>",
        "<research_track>\n" + kb.research + "\n</research_track>",
    ])
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral", "ttl": cache_ttl}}]


def job_message(job: dict, max_chars: int) -> str:
    payload = {
        "company": job.get("company"),
        "title": job.get("title"),
        "location": job.get("location"),
        "remote": bool(job.get("is_remote")),
        "department": job.get("department") or None,
        "employment_type": job.get("employment_type") or None,
        "salary": job.get("salary") or None,
        "track_hint_from_title": job.get("track_hint") or None,
    }
    description = truncate(job.get("description") or "(no description provided)", max_chars)
    return ("Score this posting.\n<posting_meta>\n" + json.dumps(payload, ensure_ascii=False) +
            "\n</posting_meta>\n<description>\n" + description + "\n</description>")

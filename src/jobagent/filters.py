"""Cheap, rule-based filters that run before any AI scoring (no tokens spent on obvious misses)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from .location import classify


def _kw(pattern: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(pattern.strip().lower()) + r"(?![a-z0-9])", re.I)


@dataclass(slots=True)
class FilterDecision:
    status: str                  # pass | excluded | needs_detail
    reason: str = ""
    sponsorship: str = "not_mentioned"
    track_hint: str = ""
    country: str | None = None
    is_remote: bool = False

    def as_row(self, job_id: int) -> tuple:
        return (self.status, self.reason, self.sponsorship, self.track_hint, self.country, int(self.is_remote), job_id)


class JobFilter:
    def __init__(self, cfg: dict[str, Any], avoid_companies: Iterable[str] = (),
                 applied_keys: Iterable[str] = (), detail_ats: Iterable[str] = (), today: date | None = None):
        self.track_kw = {t: [_kw(k) for k in kws] for t, kws in cfg["role_keywords"].items()}
        self.exclude = [(k.strip(), _kw(k)) for k in cfg["exclude_title_keywords"]]
        self.exceptions = [_kw(k) for k in cfg.get("exclude_exceptions", [])] + \
            [re.compile(p, re.I) for p in cfg.get("exclude_exception_patterns", [])]
        self.exclude_manager = bool(cfg.get("exclude_manager_titles", True))
        self.manager = _kw("manager")
        self.blockers = [re.compile(p, re.I) for p in cfg["sponsorship_blockers"]]
        self.positive = [re.compile(p, re.I) for p in cfg.get("sponsorship_positive", [])]
        self.max_age = int(cfg.get("max_posting_age_days", 45))
        self.allow_remote_unknown = bool(cfg.get("allow_remote_without_country", True))
        self.avoid = {a.strip().lower() for a in avoid_companies if a.strip()}
        self.applied = {a.strip().lower() for a in applied_keys if a.strip()}
        self.detail_ats = set(detail_ats)
        self.today = today or date.today()

    def title_check(self, title: str) -> tuple[bool, str, str]:
        """Returns (ok, reason_if_not, track_hint).

        An exception phrase only cancels the exclusion words it actually covers: "Associate Product Marketing
        Manager" is allowed (the exception covers "manager"), but "Senior Research Associate" is still excluded
        because nothing covers "senior"."""
        t = title.lower()
        covered = [m.span() for p in self.exceptions for m in p.finditer(t)]

        def uncovered(m: re.Match) -> bool:
            return not any(a <= m.start() and m.end() <= b for a, b in covered)

        for word, pat in self.exclude:
            if any(uncovered(m) for m in pat.finditer(t)):
                return False, f"title excluded ({word})", ""
        if self.exclude_manager and any(uncovered(m) for m in self.manager.finditer(t)):
            return False, "title excluded (manager)", ""
        tracks = [name for name, pats in self.track_kw.items() if any(p.search(t) for p in pats)]
        if not tracks:
            return False, "title not relevant to either track", ""
        return True, "", tracks[0] if len(tracks) == 1 else "both"

    def sponsorship(self, description: str) -> tuple[str, str]:
        for pat in self.blockers:
            m = pat.search(description)
            if m:
                return "blocked", m.group(0)
        for pat in self.positive:
            if pat.search(description):
                return "positive", ""
        return "not_mentioned", ""

    def decide(self, job: dict[str, Any]) -> FilterDecision:
        company = (job.get("company") or "").lower()
        if company in self.avoid:
            return FilterDecision("excluded", "company on avoid list")
        if f"{company}|{(job.get('title') or '').lower()}" in self.applied:
            return FilterDecision("excluded", "already applied (lists.yaml)")
        ok, reason, track = self.title_check(job.get("title") or "")
        if not ok:
            return FilterDecision("excluded", reason)
        country, remote = classify(job.get("location") or "", job.get("country"), bool(job.get("is_remote")))
        if country == "XX":
            return FilterDecision("excluded", "outside the US", country=country, is_remote=remote)
        loc_note = ""
        if country is None:
            if remote and not self.allow_remote_unknown:
                return FilterDecision("excluded", "remote, country not stated", is_remote=remote)
            loc_note = "location not confirmed as US"
        posted = job.get("posted_at")
        if posted:
            try:
                if date.fromisoformat(posted[:10]) < self.today - timedelta(days=self.max_age):
                    return FilterDecision("excluded", f"posted over {self.max_age} days ago", country=country,
                                          is_remote=remote)
            except ValueError:
                pass
        description = job.get("description")
        if description is None and job.get("ats") in self.detail_ats:
            return FilterDecision("needs_detail", "", track_hint=track, country=country, is_remote=remote)
        flag, phrase = self.sponsorship(description or "")
        if flag == "blocked":
            return FilterDecision("excluded", f"no sponsorship: \"{phrase}\"", sponsorship=flag, track_hint=track,
                                  country=country, is_remote=remote)
        return FilterDecision("pass", loc_note, sponsorship=flag, track_hint=track, country=country, is_remote=remote)

"""Workable public widget API: apply.workable.com/api/v1/widget/accounts/{slug}?details=true"""

from __future__ import annotations

from ..location import classify
from ..models import Job
from ..text import html_to_text, to_iso_date
from .base import Fetcher


class WorkableFetcher(Fetcher):
    ats = "workable"
    host = "apply.workable.com"

    def board_url(self, slug: str) -> str:
        return f"https://apply.workable.com/api/v1/widget/accounts/{slug}"

    def board_params(self):
        return {"details": "true"}

    def looks_valid(self, data) -> bool:
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)

    def parse(self, data, company: str, slug: str) -> list[Job]:
        jobs = []
        for j in data.get("jobs", []):
            locs = [", ".join(x for x in (l.get("city"), l.get("region"), l.get("country")) if x)
                    for l in j.get("locations") or [] if not l.get("hidden")]
            if not locs:
                locs = [", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)]
            location = "; ".join(x for x in locs if x)
            codes = {(l.get("countryCode") or "").upper() for l in j.get("locations") or [] if l.get("countryCode")}
            hint = "US" if "US" in codes else (next(iter(codes)) if codes else None)
            country, remote = classify(location, hint, bool(j.get("telecommuting")))
            ext = j.get("shortcode") or j.get("code") or j.get("id")
            jobs.append(Job(
                ats=self.ats, company=company, company_slug=slug, external_id=str(ext),
                title=(j.get("title") or "").strip(), url=j.get("url") or j.get("shortlink", ""),
                apply_url=j.get("application_url") or j.get("url", ""), location=location, country=country,
                is_remote=remote, department=j.get("department", "") or "",
                employment_type=j.get("employment_type", "") or "",
                description=html_to_text(j.get("description")) if j.get("description") else None,
                posted_at=to_iso_date(j.get("published_on") or j.get("created_at")),
            ))
        return jobs

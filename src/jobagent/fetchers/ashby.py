"""Ashby public job board API: api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"""

from __future__ import annotations

from ..location import classify, normalize_country
from ..models import Job
from ..text import html_to_text, to_iso_date
from .base import Fetcher


class AshbyFetcher(Fetcher):
    ats = "ashby"
    host = "api.ashbyhq.com"

    def board_url(self, slug: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"

    def board_params(self):
        return {"includeCompensation": "true"}

    def looks_valid(self, data) -> bool:
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)

    def parse(self, data, company: str, slug: str) -> list[Job]:
        jobs = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            secondary = j.get("secondaryLocations") or []
            locs = [j.get("location") or ""] + [s.get("location", "") for s in secondary]
            location = "; ".join(x for x in locs if x)
            countries = [((a.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
                         for a in [j, *secondary]]
            countries = [c for c in countries if c]
            # Prefer a US address when any of the posting's locations is in the US.
            hint = next((c for c in countries if normalize_country(c) == "US"), countries[0] if countries else None)
            # Ashby sets isRemote=true for hybrid roles too; workplaceType is the reliable field.
            workplace = (j.get("workplaceType") or "").lower()
            remote = workplace == "remote" if workplace else bool(j.get("isRemote"))
            country, is_remote = classify(location, hint, remote)
            comp = j.get("compensation") or {}
            salary = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary") or ""
            jobs.append(Job(
                ats=self.ats, company=company, company_slug=slug, external_id=str(j["id"]),
                title=(j.get("title") or "").strip(), url=j.get("jobUrl", ""),
                apply_url=j.get("applyUrl") or (j.get("jobUrl", "") + "/application" if j.get("jobUrl") else ""),
                location=location, country=country, is_remote=is_remote,
                department=" / ".join(x for x in (j.get("department"), j.get("team")) if x),
                employment_type=j.get("employmentType", "") or "", salary=salary or "",
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                posted_at=to_iso_date(j.get("publishedAt")),
                extra={"workplace": workplace} if workplace else {},
            ))
        return jobs

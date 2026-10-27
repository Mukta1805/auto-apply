"""Greenhouse public job board API: boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"""

from __future__ import annotations

from ..location import classify
from ..models import Job
from ..text import html_to_text, to_iso_date
from .base import Fetcher


class GreenhouseFetcher(Fetcher):
    ats = "greenhouse"
    host = "boards-api.greenhouse.io"

    def board_url(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    def board_params(self):
        return {"content": "true"}

    def looks_valid(self, data) -> bool:
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)

    def parse(self, data, company: str, slug: str) -> list[Job]:
        jobs = []
        for j in data.get("jobs", []):
            location = (j.get("location") or {}).get("name", "") or ""
            offices = [o.get("name", "") for o in j.get("offices") or [] if o.get("name")]
            if offices and location and location not in offices:
                location = "; ".join([location, *offices])
            elif offices and not location:
                location = "; ".join(offices)
            country, remote = classify(location)
            departments = [d.get("name", "") for d in j.get("departments") or [] if d.get("name")]
            jobs.append(Job(
                ats=self.ats, company=company, company_slug=slug, external_id=str(j["id"]),
                title=(j.get("title") or "").strip(), url=j.get("absolute_url", ""), apply_url=j.get("absolute_url", ""),
                location=location, country=country, is_remote=remote, department=", ".join(departments),
                description=html_to_text(j.get("content")),
                posted_at=to_iso_date(j.get("first_published") or j.get("updated_at")),
            ))
        return jobs

"""Lever public postings API: api.lever.co/v0/postings/{slug}?mode=json"""

from __future__ import annotations

from ..location import classify
from ..models import Job
from ..text import html_to_text, to_iso_date
from .base import Fetcher


class LeverFetcher(Fetcher):
    ats = "lever"
    host = "api.lever.co"

    def board_url(self, slug: str) -> str:
        return f"https://api.lever.co/v0/postings/{slug}"

    def board_params(self):
        return {"mode": "json"}

    def looks_valid(self, data) -> bool:
        return isinstance(data, list)

    def parse(self, data, company: str, slug: str) -> list[Job]:
        jobs = []
        for p in data:
            cats = p.get("categories") or {}
            locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
            location = "; ".join(locations)
            workplace = (p.get("workplaceType") or "").lower()
            country, remote = classify(location, p.get("country"), workplace == "remote")
            parts = [p.get("descriptionPlain") or html_to_text(p.get("description"))]
            for section in p.get("lists") or []:
                parts.append(f"{section.get('text', '')}:\n{html_to_text(section.get('content'))}")
            parts.append(p.get("additionalPlain") or html_to_text(p.get("additional")))
            salary = ""
            sr = p.get("salaryRange") or {}
            if sr.get("min") and sr.get("max"):
                salary = f"{sr.get('currency', 'USD')} {sr['min']:,}-{sr['max']:,} {sr.get('interval', '')}".strip()
            jobs.append(Job(
                ats=self.ats, company=company, company_slug=slug, external_id=str(p["id"]),
                title=(p.get("text") or "").strip(), url=p.get("hostedUrl", ""), apply_url=p.get("applyUrl", ""),
                location=location, country=country, is_remote=remote,
                department=" / ".join(x for x in (cats.get("department"), cats.get("team")) if x),
                employment_type=cats.get("commitment", "") or "", salary=salary,
                description="\n\n".join(x for x in parts if x).strip(),
                posted_at=to_iso_date(p.get("createdAt")),
            ))
        return jobs

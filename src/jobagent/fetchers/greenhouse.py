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
            location = ((j.get("location") or {}).get("name") or "").strip().strip(";").strip()
            # Office records carry the full address ("Masthead, UK") even when the job's own location is
            # just a town ("Maidenhead"), so include them for country detection.
            office_places = []
            for o in j.get("offices") or []:
                place = o.get("location") or o.get("name")
                if place and place not in location and place not in office_places:
                    office_places.append(place)
            full_location = "; ".join(x for x in [location, *office_places] if x)
            workplace = ""
            for m in j.get("metadata") or []:
                if "workplace" in (m.get("name") or "").lower() and isinstance(m.get("value"), str):
                    workplace = m["value"].lower()
            remote = workplace in ("remote", "virtual", "fully remote") or None
            country, is_remote = classify(full_location, remote_hint=remote)
            departments = [d.get("name", "") for d in j.get("departments") or [] if d.get("name")]
            jobs.append(Job(
                ats=self.ats, company=company, company_slug=slug, external_id=str(j["id"]),
                title=(j.get("title") or "").strip(), url=j.get("absolute_url", ""), apply_url=j.get("absolute_url", ""),
                location=location or full_location, country=country, is_remote=is_remote,
                department=", ".join(departments), description=html_to_text(j.get("content")),
                posted_at=to_iso_date(j.get("first_published") or j.get("updated_at")),
                extra={"workplace": workplace} if workplace else {},
            ))
        return jobs

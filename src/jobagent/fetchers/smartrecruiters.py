"""SmartRecruiters public postings API: api.smartrecruiters.com/v1/companies/{slug}/postings

The listing has no descriptions, so details are fetched only for jobs that pass the cheap filters."""

from __future__ import annotations

from ..http import HttpClient
from ..location import classify
from ..models import Job
from ..text import html_to_text, to_iso_date
from .base import BoardResult, Fetcher

PAGE = 100
MAX_PAGES = 20


class SmartRecruitersFetcher(Fetcher):
    ats = "smartrecruiters"
    host = "api.smartrecruiters.com"
    needs_detail = True

    def board_url(self, slug: str) -> str:
        return f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"

    def looks_valid(self, data) -> bool:
        # Unknown companies return 200 with zero results, so require at least one posting.
        return isinstance(data, dict) and (data.get("totalFound") or 0) > 0

    async def fetch(self, http: HttpClient, company: str, slug: str,
                    etag: str | None = None, last_modified: str | None = None) -> BoardResult:
        jobs: list[Job] = []
        elapsed = 0
        for page in range(MAX_PAGES):
            res = await http.get_json(self.board_url(slug), params={"limit": PAGE, "offset": page * PAGE},
                                      expect_404=True)
            elapsed += res.elapsed_ms
            if res.status == 404:
                return BoardResult("not_found", error="board not found (404)", elapsed_ms=elapsed)
            if not res.ok:
                return BoardResult("error", error=res.error, elapsed_ms=elapsed)
            content = (res.data or {}).get("content") or []
            jobs.extend(self.parse(res.data, company, slug))
            if len(content) < PAGE:
                break
        return BoardResult("ok", jobs=jobs, elapsed_ms=elapsed)

    def parse(self, data, company: str, slug: str) -> list[Job]:
        jobs = []
        for p in data.get("content", []):
            loc = p.get("location") or {}
            location = loc.get("fullLocation") or ", ".join(
                x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x)
            country, remote = classify(location, loc.get("country"), bool(loc.get("remote")))
            ident = (p.get("company") or {}).get("identifier") or slug
            jobs.append(Job(
                ats=self.ats, company=company, company_slug=slug, external_id=str(p["id"]),
                title=(p.get("name") or "").strip(),
                url=f"https://jobs.smartrecruiters.com/{ident}/{p['id']}",
                location=location, country=country, is_remote=remote,
                department=((p.get("department") or {}).get("label") or ""),
                employment_type=((p.get("typeOfEmployment") or {}).get("label") or ""),
                description=None, posted_at=to_iso_date(p.get("releasedDate")),
            ))
        return jobs

    async def hydrate(self, http: HttpClient, slug: str, external_id: str) -> tuple[str, str] | None:
        res = await http.get_json(f"{self.board_url(slug)}/{external_id}")
        if not res.ok or not isinstance(res.data, dict):
            return None
        sections = ((res.data.get("jobAd") or {}).get("sections") or {})
        parts = []
        for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
            sec = sections.get(key) or {}
            if sec.get("text"):
                parts.append(f"{sec.get('title') or key}:\n{html_to_text(sec['text'])}")
        return "\n\n".join(parts), res.data.get("applyUrl") or res.data.get("postingUrl") or ""

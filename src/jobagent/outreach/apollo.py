"""Apollo.io contact finding.

Per the Apollo API docs (docs.apollo.io/reference/people-api-search, checked 2026-10-27):
  - POST /api/v1/mixed_people/api_search    people search, free, returns no emails
  - POST /api/v1/people/match                enrichment: reveals the work email (uses credits)
  - POST /api/v1/mixed_companies/search      organization search (used to find a company's domain)
Auth: X-Api-Key header. Only `verified` emails are used, and only one reveal per job.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from ..companies import names_match
from ..http import HttpClient

log = logging.getLogger("jobagent.apollo")
BASE = "https://api.apollo.io/api/v1"

TITLES = {
    "business": ["head of business development", "vp business development", "director of business development",
                 "chief business officer", "head of strategy", "vp strategy", "director of strategy",
                 "head of commercial", "vp commercial", "head of partnerships", "head of growth",
                 "chief operating officer", "chief of staff", "chief executive officer", "co-founder", "founder"],
    "research": ["head of research", "vp research", "director of research", "head of r&d", "vp r&d",
                 "head of process development", "director of process development", "director, process development",
                 "head of analytical development", "chief scientific officer", "principal scientist",
                 "associate director", "lab manager", "co-founder", "founder"],
    "recruiting": ["recruiter", "technical recruiter", "talent acquisition", "head of talent", "talent partner",
                   "people operations"],
}
SENIORITIES = ["owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager", "senior"]


@dataclass
class Person:
    apollo_id: str
    first_name: str
    name: str
    title: str
    has_email: bool
    linkedin_url: str = ""
    email: str = ""
    email_status: str = ""


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z&]+", (s or "").lower())) - {"of", "and", "the", "a", "for", "to", "in", "i", "ii"}


def rank_people(people: list[Person], job: dict) -> list[Person]:
    """Prefer people in the job's own function and seniority band; recruiters are a fallback."""
    track = job.get("track") or "business"
    job_words = _words(job.get("title", "")) | _words(job.get("department", ""))
    preferred = TITLES.get(track, [])

    def score(p: Person) -> float:
        t = p.title.lower()
        s = 0.0
        if any(k in t for k in preferred):
            s += 3
        s += 1.5 * len(job_words & _words(t))
        if any(k in t for k in TITLES["recruiting"]):
            s += 1                                  # a real fallback, but below a hiring-side contact
        if re.search(r"\b(ceo|chief executive|founder)\b", t):
            s += 0.5                                # founders reply at small companies
        if not p.has_email:
            s -= 10
        return s

    return sorted(people, key=score, reverse=True)


class ApolloClient:
    def __init__(self, http: HttpClient, api_key: str):
        self.http = http
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json", "Cache-Control": "no-cache"}
        self.reveals = 0

    async def find_domain(self, company: str) -> str | None:
        res = await self.http.request_json("POST", f"{BASE}/mixed_companies/search", headers=self.headers,
                                           json_body={"q_organization_name": company, "per_page": 5}, retry_post=True)
        if not res.ok:
            log.warning("Apollo organization search failed for %s: %s", company, res.error)
            return None
        for org in (res.data or {}).get("organizations") or (res.data or {}).get("accounts") or []:
            if names_match(company, org.get("name") or ""):
                domain = org.get("primary_domain") or re.sub(r"^https?://(www\.)?|/.*$", "", org.get("website_url") or "")
                if domain:
                    return domain.lower()
        return None

    async def search_people(self, domain: str, titles: list[str], per_page: int = 25) -> list[Person]:
        params: dict[str, Any] = {"q_organization_domains_list[]": [domain], "person_titles[]": titles,
                                  "person_seniorities[]": SENIORITIES, "include_similar_titles": "true",
                                  "per_page": per_page, "page": 1}
        res = await self.http.request_json("POST", f"{BASE}/mixed_people/api_search", headers=self.headers,
                                           params=params, retry_post=True)
        if not res.ok:
            log.warning("Apollo people search failed for %s: %s", domain, res.error)
            return []
        out = []
        for p in (res.data or {}).get("people") or (res.data or {}).get("contacts") or []:
            first = p.get("first_name") or ""
            last = p.get("last_name") or p.get("last_name_obfuscated") or ""
            out.append(Person(apollo_id=str(p.get("id") or ""), first_name=first, name=f"{first} {last}".strip(),
                              title=p.get("title") or "", linkedin_url=p.get("linkedin_url") or "",
                              has_email=bool(p.get("has_email", True))))
        return [p for p in out if p.apollo_id]

    async def reveal(self, person: Person) -> Person:
        """Uses Apollo credits. Returns the person with email/email_status filled in when available."""
        res = await self.http.request_json("POST", f"{BASE}/people/match", headers=self.headers,
                                           params={"id": person.apollo_id, "reveal_personal_emails": "false",
                                                   "reveal_phone_number": "false"})
        self.reveals += 1
        if not res.ok:
            log.warning("Apollo enrichment failed for %s: %s", person.apollo_id, res.error)
            return person
        p = (res.data or {}).get("person") or {}
        person.email = (p.get("email") or "").strip()
        person.email_status = (p.get("email_status") or "").strip().lower()
        if p.get("last_name"):
            person.name = f"{p.get('first_name') or person.first_name} {p['last_name']}".strip()
        person.linkedin_url = p.get("linkedin_url") or person.linkedin_url
        return person

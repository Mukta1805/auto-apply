"""Shared data types."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Company:
    name: str
    industry: str = ""
    ats: str = "unknown"          # greenhouse | lever | ashby | workable | smartrecruiters | unknown | unsupported
    slug: str = ""
    careers_url: str = ""
    priority: int = 2             # 1 = dream company
    active: bool = True
    notes: str = ""
    id: int | None = None


@dataclass(slots=True)
class Job:
    """A job posting normalized across job boards."""

    ats: str
    company: str
    external_id: str
    title: str
    url: str
    location: str = ""
    country: str | None = None     # ISO-2 when known ("US")
    is_remote: bool = False
    department: str = ""
    employment_type: str = ""
    salary: str = ""
    description: str | None = None  # plain text; None = needs hydration
    posted_at: str | None = None    # ISO date
    apply_url: str = ""
    company_slug: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.ats}:{self.company_slug or self.company}:{self.external_id}".lower()

    def content_hash(self) -> str:
        """Hash of what the job board listing returned. Used to detect changed postings,
        so only new or changed jobs are filtered and scored again."""
        return content_hash(self.title, self.location, self.description or "", self.salary)


def content_hash(title: str, location: str, description: str, salary: str) -> str:
    basis = "\x1f".join([title or "", location or "", description or "", salary or ""])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

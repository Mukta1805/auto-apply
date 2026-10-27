"""The target company list (config/companies.csv) and automatic job-board detection."""

from __future__ import annotations

import asyncio
import csv
import logging
import re
from pathlib import Path

from .fetchers import FETCHERS
from .http import HttpClient
from .models import Company

log = logging.getLogger("jobagent.companies")
FIELDS = ["name", "industry", "ats", "slug", "careers_url", "priority", "active", "notes"]
SUPPORTED = list(FETCHERS)
DETECT_ORDER = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]
_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "company", "the", "therapeutics", "pharmaceuticals", "pharma",
             "biosciences", "bioscience", "biotechnology", "biotech", "health", "healthcare", "technologies",
             "technology", "labs", "laboratory", "laboratories", "institute", "consulting", "partners", "group",
             "associates", "advisors", "medicine", "diagnostics", "genomics", "center", "cancer", "research"}


def load_companies(path: Path) -> list[Company]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        out = []
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            out.append(Company(
                name=name, industry=(row.get("industry") or "").strip(),
                ats=(row.get("ats") or "unknown").strip().lower() or "unknown",
                slug=(row.get("slug") or "").strip(), careers_url=(row.get("careers_url") or "").strip(),
                priority=int(row.get("priority") or 2), active=str(row.get("active", "1")).strip() not in ("0", "false", "no"),
                notes=(row.get("notes") or "").strip()))
        return out


def save_companies(path: Path, companies: list[Company]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(FIELDS)
        for c in companies:
            w.writerow([c.name, c.industry, c.ats, c.slug, c.careers_url, c.priority, int(c.active), c.notes])
    tmp.replace(path)


def slug_candidates(name: str) -> list[tuple[str, bool]]:
    """Likely board slugs for a company name, as (slug, is_strong). Weak = first word only."""
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and ").replace("'", ""))
    core = [w for w in words if w not in _SUFFIXES] or words
    seen: dict[str, bool] = {}
    for slug, strong in (("".join(words), True), ("-".join(words), True), ("".join(core), True),
                         ("-".join(core), True), (core[0], len(core) == 1 or len(core[0]) >= 7)):
        if slug and slug not in seen:
            seen[slug] = strong
    return list(seen.items())


def _name_matches(company: str, board_name: str | None) -> bool:
    if not board_name:
        return True
    first = re.findall(r"[a-z0-9]+", company.lower())[0]
    return first in board_name.lower()


async def _probe(http: HttpClient, ats: str, company: str) -> tuple[str, int] | None:
    fetcher = FETCHERS[ats]
    for slug, strong in slug_candidates(company):
        if not strong and ats in ("lever", "ashby"):
            continue   # these boards don't return a company name, so weak guesses can't be verified
        res = await http.get_json(fetcher.board_url(slug), params=fetcher.board_params(), expect_404=True)
        if not res.ok or not fetcher.looks_valid(res.data):
            continue
        board_name = None
        if ats == "greenhouse":
            meta = await http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}", expect_404=True)
            board_name = (meta.data or {}).get("name") if meta.ok else None
        elif ats == "workable":
            board_name = res.data.get("name")
        elif ats == "smartrecruiters":
            content = res.data.get("content") or [{}]
            board_name = (content[0].get("company") or {}).get("name")
        if not _name_matches(company, board_name):
            log.debug("%s: %s/%s belongs to %r, skipping", company, ats, slug, board_name)
            continue
        count = len(res.data.get("jobs", res.data.get("content", []))) if isinstance(res.data, dict) else len(res.data)
        return slug, count
    return None


async def detect_boards(http: HttpClient, companies: list[Company], redetect: bool = False) -> dict[str, int]:
    """Find the job board for companies marked ats=unknown. Updates the Company objects in place."""
    targets = [c for c in companies if c.active and (c.ats == "unknown" or (redetect and c.ats == "unsupported"))]
    stats = {"checked": len(targets), "found": 0, "unsupported": 0}

    async def one(c: Company) -> None:
        results = await asyncio.gather(*(_probe(http, ats, c.name) for ats in DETECT_ORDER))
        hits = [(ats, r) for ats, r in zip(DETECT_ORDER, results) if r]
        if hits:
            # Prefer the board with the most postings (companies sometimes keep an old board around).
            ats, (slug, count) = max(hits, key=lambda h: h[1][1])
            c.ats, c.slug = ats, slug
            c.notes = (c.notes + f" auto-detected {ats}/{slug} ({count} jobs)").strip()
            stats["found"] += 1
            log.info("%s -> %s/%s (%d jobs)", c.name, ats, slug, count)
        else:
            c.ats = "unsupported"
            stats["unsupported"] += 1
            log.info("%s -> no public board found (likely Workday/iCIMS/Taleo; check careers page)", c.name)

    await asyncio.gather(*(one(c) for c in targets))
    return stats

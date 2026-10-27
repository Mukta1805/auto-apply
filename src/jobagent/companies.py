"""The target company list (config/companies.csv) and automatic job-board detection.

Detection rules were tuned on live probes of all 211 seed companies (tests/fixtures/real_detection_hits.json):
  * Slugs are tried in three tiers: the full name ("beamtherapeutics"), the name without generic words
    ("beam" for "Beam Therapeutics" counts as core), and the first word alone (weak).
  * Boards that report a company name (Greenhouse, Workable, SmartRecruiters) must match it both ways,
    which rejects look-alikes such as "Polychain Genesis", "Parallel Systems" or "Nkarta Testing Job Board".
  * Boards without a name (Lever, Ashby) accept a non-full slug only when the postings mention the full
    company name; this rejected live collisions like "generate", "scribe", "latent" and "watershed".
  * The best board wins on: has jobs, then slug tier, then job count.
  * Greenhouse, Lever and Ashby are probed first; Workable and SmartRecruiters (slower, rate-limited) only
    for companies not found there.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fetchers import FETCHERS
from .http import HttpClient
from .models import Company

log = logging.getLogger("jobagent.companies")
FIELDS = ["name", "industry", "ats", "slug", "careers_url", "domain", "priority", "active", "notes"]
SUPPORTED = list(FETCHERS)
PRIMARY = ["greenhouse", "lever", "ashby"]
SECONDARY = ["workable", "smartrecruiters"]
NAMED_BOARDS = {"greenhouse", "workable", "smartrecruiters"}
TIER_RANK = {"full": 2, "core": 1, "weak": 0}
_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "company", "the", "therapeutics", "pharmaceuticals", "pharma",
             "biosciences", "bioscience", "biotechnology", "biotech", "health", "healthcare", "technologies",
             "technology", "labs", "laboratory", "laboratories", "institute", "consulting", "partners", "group",
             "associates", "advisors", "medicine", "diagnostics", "genomics", "center", "cancer", "research"}
# Extra words ignored when comparing a board's display name with the company name.
_NAME_NOISE = _SUFFIXES | {"bio", "ai", "and", "of", "for"}


def _words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", name.lower().replace("&", " and ").replace("'", ""))


def normalize_phrase(text: str) -> str:
    return " " + " ".join(_words(text)) + " "


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
                domain=(row.get("domain") or "").strip().lower(),
                priority=int(row.get("priority") or 2),
                active=str(row.get("active", "1")).strip() not in ("0", "false", "no"),
                notes=(row.get("notes") or "").strip()))
        return out


def save_companies(path: Path, companies: list[Company]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(FIELDS)
        for c in companies:
            w.writerow([c.name, c.industry, c.ats, c.slug, c.careers_url, c.domain, c.priority, int(c.active), c.notes])
    tmp.replace(path)


def slug_candidates(name: str) -> list[tuple[str, str]]:
    """Likely board slugs for a company name, as (slug, tier) with tier full | core | weak."""
    words = _words(name)
    core = [w for w in words if w not in _SUFFIXES] or words
    out: dict[str, str] = {}
    for slug, tier in (("".join(words), "full"), ("-".join(words), "full"),
                       ("".join(core), "core"), ("-".join(core), "core"), (core[0], "weak")):
        if slug and slug not in out:
            out[slug] = tier
    if len(core) == 1 and out.get(core[0]) == "weak":
        out[core[0]] = "core"                     # e.g. "beam" for "Beam Therapeutics"
    return list(out.items())


def names_match(company: str, board_name: str) -> bool:
    """Both directions must hold: the board's distinctive words are in the company name and vice versa."""
    board = set(_words(board_name)) - _NAME_NOISE
    comp_all = set(_words(company))
    comp_core = comp_all - _NAME_NOISE
    return bool(board) and board <= comp_all and comp_core <= set(_words(board_name))


@dataclass
class BoardHit:
    company: str
    ats: str
    slug: str
    jobs: int
    board_name: str | None = None
    mentions: bool | None = None       # postings mention the full company name (name-less boards)
    tier: str = ""

    def __post_init__(self) -> None:
        if not self.tier:
            self.tier = dict(slug_candidates(self.company)).get(self.slug, "weak")


def accept_reason(hit: BoardHit) -> tuple[bool, str]:
    if hit.board_name is not None:
        if not names_match(hit.company, hit.board_name):
            return False, f"board belongs to {hit.board_name.strip()!r}"
    elif hit.tier != "full" and hit.mentions is not True:
        return False, "ambiguous slug and postings don't mention the company"
    if hit.jobs == 0 and hit.tier == "weak":
        return False, "empty board on an ambiguous slug"
    return True, "ok"


def choose_board(hits: list[BoardHit]) -> tuple[BoardHit | None, list[tuple[BoardHit, str]]]:
    accepted, rejected = [], []
    for h in hits:
        ok, why = accept_reason(h)
        (accepted.append(h) if ok else rejected.append((h, why)))
    if not accepted:
        return None, rejected
    order = {a: i for i, a in enumerate(PRIMARY + SECONDARY)}
    best = max(accepted, key=lambda h: (h.jobs > 0, TIER_RANK[h.tier], h.jobs, -order.get(h.ats, 9)))
    return best, rejected


def _mentions(data: Any, company: str) -> bool:
    texts: list[str] = []
    if isinstance(data, list):                                  # Lever
        for p in data[:30]:
            texts += [p.get("descriptionPlain") or p.get("description") or "", p.get("additionalPlain") or "",
                      p.get("openingPlain") or ""]
    elif isinstance(data, dict):                                # Ashby / others
        for j in (data.get("jobs") or [])[:30]:
            texts.append(j.get("descriptionPlain") or j.get("descriptionHtml") or j.get("description") or "")
    return normalize_phrase(company) in normalize_phrase(" ".join(texts))


async def _probe(http: HttpClient, ats: str, company: str) -> list[BoardHit]:
    fetcher = FETCHERS[ats]
    hits = []
    for slug, tier in slug_candidates(company):
        res = await http.get_json(fetcher.board_url(slug), params=fetcher.board_params(), expect_404=True)
        if not res.ok or not fetcher.looks_valid(res.data):
            continue
        data = res.data
        board_name = None
        if ats == "greenhouse":
            meta = await http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}", expect_404=True)
            board_name = (meta.data or {}).get("name", "") if meta.ok else ""
        elif ats == "workable":
            board_name = data.get("name") or ""
        elif ats == "smartrecruiters":
            board_name = ((data.get("content") or [{}])[0].get("company") or {}).get("name") or ""
        count = (data.get("totalFound") if ats == "smartrecruiters" else
                 len(data.get("jobs", [])) if isinstance(data, dict) else len(data))
        hits.append(BoardHit(company, ats, slug, int(count or 0), board_name,
                             None if ats in NAMED_BOARDS else _mentions(data, company), tier))
    return hits


async def detect_board(http: HttpClient, company: str) -> tuple[BoardHit | None, list[tuple[BoardHit, str]]]:
    hits: list[BoardHit] = []
    for group in (PRIMARY, SECONDARY):
        for result in await asyncio.gather(*(_probe(http, ats, company) for ats in group)):
            hits.extend(result)
        best, rejected = choose_board(hits)
        if best is not None:
            return best, rejected
    return choose_board(hits)


async def detect_boards(http: HttpClient, companies: list[Company], redetect: bool = False,
                        concurrency: int = 6) -> dict[str, int]:
    """Find the job board for companies marked ats=unknown (and unsupported, with redetect). In place."""
    targets = [c for c in companies if c.active and (c.ats == "unknown" or (redetect and c.ats == "unsupported"))]
    stats = {"checked": len(targets), "found": 0, "unsupported": 0}
    sem = asyncio.Semaphore(concurrency)

    async def one(c: Company) -> None:
        async with sem:
            best, rejected = await detect_board(http, c.name)
        for h, why in rejected:
            log.debug("%s: rejected %s/%s (%d jobs): %s", c.name, h.ats, h.slug, h.jobs, why)
        if best:
            c.ats, c.slug = best.ats, best.slug
            c.notes = f"auto-detected {best.ats}/{best.slug} ({best.jobs} jobs)"
            stats["found"] += 1
            log.info("%s -> %s/%s (%d jobs, %s slug)", c.name, best.ats, best.slug, best.jobs, best.tier)
        else:
            c.ats = "unsupported"
            c.notes = "no public board found (likely Workday/iCIMS/Taleo); check the careers page"
            stats["unsupported"] += 1
            log.info("%s -> no public board found", c.name)

    await asyncio.gather(*(one(c) for c in targets))
    return stats

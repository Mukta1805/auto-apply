"""Regression tests built from live data captured on 2026-10-27 (job-board APIs and board detection)."""

import asyncio
import json
from collections import defaultdict
from pathlib import Path

import httpx
import pytest
import respx

from jobagent.companies import BoardHit, choose_board, names_match, slug_candidates
from jobagent.config import load_config
from jobagent.fetchers import FETCHERS
from jobagent.filters import JobFilter
from jobagent.http import HttpClient
from jobagent.location import classify, normalize_country

FIX = Path(__file__).parent / "fixtures"
REAL = json.loads((FIX / "real_samples.json").read_text())


def test_greenhouse_country_from_office_and_workplace_metadata():
    jobs = {j.external_id: j for j in FETCHERS["greenhouse"].parse(REAL["greenhouse_alnylam"], "Alnylam", "alnylam")}
    uk = jobs["4400012009"]
    assert uk.location == "Maidenhead" and uk.country == "XX"          # country only in the office ("Masthead, UK")
    assert uk.is_remote is False                                        # "Hybrid" is not remote
    us = jobs["4400156009"]
    assert us.country == "US" and us.is_remote is True                 # "Virtual" workplace type


def test_greenhouse_multi_city_and_india():
    jobs = {j.external_id: j for j in FETCHERS["greenhouse"].parse(REAL["greenhouse_clearview"], "ClearView", "cv")}
    assert jobs["5213990008"].country == "XX"
    multi = jobs["5070509008"]
    assert multi.country == "US" and not multi.location.endswith(";")


def test_ashby_country_spellings_and_hybrid_is_not_remote():
    jobs = {j.external_id: j for j in FETCHERS["ashby"].parse(REAL["ashby_mixed"], "X", "x")}
    assert jobs["g1"].country == "US" and jobs["g1"].is_remote is False     # isRemote=true but workplaceType=Hybrid
    assert jobs["i1"].country == "US"                                       # addressCountry "U.S.A"
    assert jobs["i1"].description == "Analyze payer data."                  # empty plain text -> HTML fallback
    assert jobs["i1"].apply_url.endswith("/application")                    # derived when applyUrl is blank
    assert jobs["o1"].country == "XX" and jobs["o1"].is_remote is True
    assert jobs["r1"].country == "US" and jobs["r1"].is_remote is True


def test_lever_empty_plain_description_falls_back_to_html():
    (j,) = FETCHERS["lever"].parse(REAL["lever_synthego"], "Synthego", "synthego")
    assert "associate product marketer" in j.description and "Synthego is a pioneering" in j.description
    assert j.salary.startswith("USD 80,000-105,000") and j.is_remote and j.country == "US"


@pytest.mark.parametrize("hint,expected", [("U.S.A", "US"), ("United States of America", "US"), ("us", "US"),
                                           ("USA", "US"), ("France", "XX"), ("gb", "XX"), ("", None), (None, None)])
def test_normalize_country(hint, expected):
    assert normalize_country(hint) == expected


@pytest.mark.parametrize("loc,expected", [("SF Office", "US"), ("San Francisco-HQ", "US"), ("Bay Area", "US"),
                                          ("New York City Office", "US"), ("Anywhere in the United States", "US"),
                                          ("Remote (USA)", "US"), ("Maidenhead", None), ("Paris ", "XX")])
def test_real_location_strings(loc, expected):
    assert classify(loc)[0] == expected


def test_title_rules_on_real_titles():
    jf = JobFilter(load_config().data["filters"])
    assert jf.title_check("Scientific Associate Product Marketing Manager")[0]
    assert not jf.title_check("Senior Research Associate")[0]              # exception doesn't cover "senior"
    assert not jf.title_check("Associate Director, Biostatistics")[0]
    assert jf.title_check("Chief of Staff - Commercial Strategy")[0]
    assert not jf.title_check("Chief Business Officer")[0]


# ---- board detection ---------------------------------------------------------
def _hits():
    data = json.loads((FIX / "real_detection_hits.json").read_text())
    by = defaultdict(list)
    for h in data["hits"]:
        by[h["company"]].append(BoardHit(**h))
    return by, data["expected"]


def test_board_choice_matches_live_verification():
    by, expected = _hits()
    for company, exp in expected.items():
        best, _ = choose_board(by[company])
        assert ([best.ats, best.slug] if best else None) == exp, company


def test_lookalike_boards_are_rejected_with_reasons():
    by, _ = _hits()
    _, rejected = choose_board(by["Genesis Therapeutics"])
    reasons = {(h.ats, h.slug): why for h, why in rejected}
    assert "Polychain Genesis" in reasons[("greenhouse", "genesis")]
    assert "mention" in reasons[("ashby", "genesis")]


@pytest.mark.parametrize("company,board,ok", [
    ("Sword Health", "Sword", True), ("Seer", "Seer Inc.", True), ("Ginkgo Bioworks", "Ginkgo Bioworks Inc.", True),
    ("Formation Bio", "Formation", True), ("Genesis Therapeutics", "Polychain Genesis", False),
    ("Parallel Bio", "Parallel Systems", False), ("Nkarta", "Nkarta Testing Job Board", False),
    ("Charles River Associates", "charles", False), ("Beam Therapeutics", "Bridge to Enter Advanced Mathematics (BEAM)", False),
])
def test_names_match(company, board, ok):
    assert names_match(company, board) is ok


def test_slug_tiers():
    assert dict(slug_candidates("Beam Therapeutics")) == {"beamtherapeutics": "full", "beam-therapeutics": "full",
                                                          "beam": "core"}
    assert dict(slug_candidates("Atomic AI"))["atomic"] == "weak"
    assert dict(slug_candidates("Hims & Hers"))["hims-and-hers"] == "full"


def test_companies_csv_reflects_live_detection():
    import csv
    rows = list(csv.DictReader(open(Path(__file__).parents[1] / "config" / "companies.csv")))
    by = {r["name"]: r for r in rows}
    assert by["Lyra Health"]["ats"] == "lever" and by["Benchling"]["ats"] == "ashby"
    assert by["Genesis Therapeutics"]["ats"] == "unsupported"
    assert sum(r["ats"] in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters") for r in rows) >= 100


# ---- HTTP behaviour seen live ------------------------------------------------
@respx.mock
async def test_retry_after_zero_is_not_trusted():
    url = "https://apply.workable.com/api/v1/widget/accounts/x"
    respx.get(url).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "0"}),
                                     httpx.Response(200, json={"jobs": []})])
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    async with HttpClient(max_retries=2) as http:
        orig = asyncio.sleep
        try:
            asyncio.sleep = fake_sleep           # type: ignore[assignment]
            res = await http.get_json(url)
        finally:
            asyncio.sleep = orig                 # type: ignore[assignment]
    assert res.ok and slept and slept[0] >= 2.0


@respx.mock
async def test_host_min_interval_paces_requests():
    url = "https://apply.workable.com/api/v1/widget/accounts/y"
    respx.get(url).mock(return_value=httpx.Response(200, json={"jobs": []}))
    async with HttpClient(host_limits={"apply.workable.com": {"concurrency": 1, "min_interval": 0.2}}) as http:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.gather(*(http.get_json(url) for _ in range(3)))
        assert loop.time() - t0 >= 0.38

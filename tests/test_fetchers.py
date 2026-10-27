import httpx
import respx

from jobagent.fetchers import FETCHERS
from jobagent.http import HttpClient
from jobagent.text import html_to_text

from .conftest import load_fixture


def test_greenhouse_parse():
    jobs = FETCHERS["greenhouse"].parse(load_fixture("greenhouse_jobs.json"), "Acme Bio", "acmebio")
    assert len(jobs) == 5
    j = jobs[0]
    assert j.uid == "greenhouse:acmebio:4001"
    assert j.country == "US" and j.posted_at == "2026-10-18"
    assert "BD team" in j.description and "<" not in j.description and "- 0-2 years" in j.description
    assert jobs[3].country == "XX"


def test_lever_parse():
    jobs = FETCHERS["lever"].parse(load_fixture("lever_postings.json"), "ChipCo", "chipco")
    a, b = jobs
    assert a.country == "US" and a.salary.startswith("USD 85,000-100,000")
    assert "Requirements:" in a.description and "Microfluidics" in a.description
    assert b.is_remote and b.country is None


def test_ashby_parse_skips_unlisted():
    jobs = FETCHERS["ashby"].parse(load_fixture("ashby_board.json"), "AI Bio", "aibio")
    assert [j.title for j in jobs] == ["Founder's Associate"]
    assert jobs[0].country == "US" and jobs[0].salary == "$90K - $110K"


def test_workable_parse():
    (j,) = FETCHERS["workable"].parse(load_fixture("workable_account.json"), "Medtech Co", "medtechco")
    assert j.external_id == "ABC123" and j.country == "US" and "CAD required" in j.description


@respx.mock
async def test_smartrecruiters_fetch_and_hydrate():
    base = "https://api.smartrecruiters.com/v1/companies/ConsultCo/postings"
    respx.get(base).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters_postings.json")))
    respx.get(f"{base}/744000011").mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters_detail.json")))
    f = FETCHERS["smartrecruiters"]
    async with HttpClient(max_retries=0) as http:
        res = await f.fetch(http, "ConsultCo", "ConsultCo")
        assert res.status == "ok" and len(res.jobs) == 2
        assert res.jobs[0].description is None and res.jobs[1].country == "XX"
        desc, apply_url = await f.hydrate(http, "ConsultCo", "744000011")
    assert "Payer research" in desc and "U.S. citizen" in desc and apply_url.endswith("oga=true")


@respx.mock
async def test_http_retries_then_succeeds_and_handles_404():
    url = "https://boards-api.greenhouse.io/v1/boards/acmebio/jobs"
    route = respx.get(url).mock(side_effect=[httpx.Response(503), httpx.Response(429, headers={"Retry-After": "0"}),
                                             httpx.Response(200, json={"jobs": []})])
    respx.get("https://boards-api.greenhouse.io/v1/boards/gone/jobs").mock(return_value=httpx.Response(404))
    async with HttpClient(max_retries=3) as http:
        http._retry_delay = staticmethod(lambda resp, attempt: 0)   # no waiting in tests
        res = await FETCHERS["greenhouse"].fetch(http, "Acme Bio", "acmebio")
        missing = await FETCHERS["greenhouse"].fetch(http, "Gone", "gone")
        assert http.stats["retries"] == 2
    assert res.status == "ok" and route.call_count == 3
    assert missing.status == "not_found"


@respx.mock
async def test_etag_not_modified():
    url = "https://boards-api.greenhouse.io/v1/boards/acmebio/jobs"
    respx.get(url, headers={"If-None-Match": '"abc"'}).mock(return_value=httpx.Response(304))
    async with HttpClient(max_retries=0) as http:
        res = await FETCHERS["greenhouse"].fetch(http, "Acme Bio", "acmebio", etag='"abc"')
    assert res.status == "not_modified"


def test_html_to_text():
    assert html_to_text("<p>A&amp;B</p><ul><li>one</li><li>two</li></ul><script>x()</script>") == "A&B\n\n- one\n- two"

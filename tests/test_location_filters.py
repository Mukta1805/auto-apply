from datetime import date

import pytest

from jobagent.config import load_config
from jobagent.filters import JobFilter
from jobagent.location import classify


@pytest.mark.parametrize("loc,country_hint,remote,expected", [
    ("South San Francisco, CA", None, None, ("US", False)),
    ("Remote - US", None, None, ("US", True)),
    ("Remote", None, None, (None, True)),
    ("London, UK", None, None, ("XX", False)),
    ("Cambridge, UK", None, None, ("XX", False)),
    ("Cambridge, MA", None, None, ("US", False)),
    ("Toronto, ON, Canada", "ca", False, ("XX", False)),
    ("London, UK; Boston, MA", None, None, ("US", False)),
    ("New York", None, None, ("US", False)),
    ("Anywhere", "US", None, ("US", True)),
    ("Bengaluru", None, None, ("XX", False)),
])
def test_classify(loc, country_hint, remote, expected):
    assert classify(loc, country_hint, remote) == expected


@pytest.fixture
def jf():
    cfg = load_config()
    return JobFilter(cfg.data["filters"], avoid_companies=["Nope Bio"], applied_keys=["acme bio|strategy analyst"],
                     detail_ats=["smartrecruiters"], today=date(2026, 10, 28))


def job(**kw):
    base = {"company": "Acme Bio", "title": "Business Development Associate", "location": "Boston, MA",
            "country": None, "is_remote": 0, "posted_at": "2026-10-20", "description": "Great role.", "ats": "greenhouse"}
    base.update(kw)
    return base


@pytest.mark.parametrize("title,ok,track", [
    ("Business Development Associate", True, "business"),
    ("Research Associate I", True, "both"),            # "associate" is a business keyword too; Claude decides
    ("Associate Scientist, Process Development", True, "both"),
    ("Senior Scientist", False, ""),
    ("Director, BD", False, ""),
    ("Associate Product Manager", True, "business"),
    ("Product Manager", False, ""),
    ("Software Engineer, Data Platform", False, ""),
    ("Summer Intern, R&D", False, ""),
    ("Internal Communications Associate", True, "business"),   # "intern" must not match "internal"
    ("Field Application Scientist", True, "research"),
    ("Nurse Practitioner", False, ""),
    ("Collaboration Specialist", False, ""),            # "lab" must not match inside "collaboration"
])
def test_title_rules(jf, title, ok, track):
    passed, _, hint = jf.title_check(title)
    assert passed is ok
    if ok:
        assert hint == track


def test_sponsorship_blockers(jf):
    for text in ("No visa sponsorship is available for this role.",
                 "Applicants must be authorized to work without sponsorship now or in the future.",
                 "We are unable to sponsor visas.", "Must be a U.S. citizen.", "Requires an active security clearance."):
        d = jf.decide(job(description=text))
        assert d.status == "excluded" and d.reason.startswith("no sponsorship"), text
    assert jf.decide(job(description="Visa sponsorship is available.")).sponsorship == "positive"
    assert jf.decide(job(description="We value sponsorship of community events.")).status == "pass"


def test_decide_paths(jf):
    assert jf.decide(job()).status == "pass"
    assert jf.decide(job(company="Nope Bio")).reason == "company on avoid list"
    assert jf.decide(job(title="Strategy Analyst")).reason.startswith("already applied")
    assert jf.decide(job(location="Berlin, Germany")).reason == "outside the US"
    assert jf.decide(job(posted_at="2026-08-01")).reason.startswith("posted over")
    assert jf.decide(job(description=None, ats="smartrecruiters")).status == "needs_detail"
    remote = jf.decide(job(location="Remote"))
    assert remote.status == "pass" and remote.reason == "location not confirmed as US"

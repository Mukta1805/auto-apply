"""Apply engine with a fake browser serving the real captured forms."""

import json
from pathlib import Path

import pytest
import yaml

from jobagent.apply.answers import Answers
from jobagent.apply.engine import ApplyEngine, select_jobs
from jobagent.config import load_config
from jobagent.db import Store
from jobagent.models import Company, Job

FORMS = Path(__file__).parent / "fixtures" / "forms"
REPO = Path(__file__).resolve().parents[1]


class FakeDriver:
    """Records every action; returns a captured real form for every URL."""

    def __init__(self, forms: dict[str, dict], outcome: str = "confirmed", failing: set[str] = frozenset()):
        self.forms = forms
        self.outcome = outcome
        self.failing = set(failing)
        self.calls: list[tuple] = []
        self.url = ""
        self.submitted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def goto(self, url):
        self.url = url
        self.submitted = False
        self.calls.append(("goto", url))

    async def extract(self):
        return self.forms[self.url]

    async def fill(self, sel, value):
        self._maybe_fail(sel)
        self.calls.append(("fill", sel, value))

    async def select(self, sel, value):
        self.calls.append(("select", sel, value))

    async def check(self, sel):
        self.calls.append(("check", sel))

    async def upload(self, sel, path):
        assert Path(path).exists(), path
        self.calls.append(("upload", sel, path))

    async def combobox(self, sel, text):
        self.calls.append(("combobox", sel, text))
        return None if sel in self.failing else text

    async def click(self, sel):
        self.calls.append(("click", sel))
        self.submitted = True

    async def screenshot(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        self.calls.append(("screenshot", path.name))

    async def post_submit_state(self):
        if not self.submitted:
            return {"challenge": False, "errors": [], "url": self.url, "text": ""}
        if self.outcome == "confirmed":
            return {"challenge": False, "errors": [], "url": self.url + "/thanks", "text": "Thank you for applying!"}
        if self.outcome == "challenge":
            return {"challenge": True, "errors": [], "url": self.url, "text": ""}
        if self.outcome == "error":
            return {"challenge": False, "errors": ["Phone is required"], "url": self.url, "text": "Phone is required"}
        return {"challenge": False, "errors": [], "url": self.url, "text": "Some page"}

    async def pause(self, seconds):
        self.calls.append(("pause", round(seconds, 1)))

    def _maybe_fail(self, sel):
        if sel in self.failing:
            raise RuntimeError("element not interactable")


class FakeWriter:
    def __init__(self, confident=True):
        self.confident = confident
        self.usage_rows = []
        self.cost = 0.0
        self.questions = []

    async def answer_questions(self, job, fields):
        self.questions.extend(f["label"] for f in fields)
        return {f["selector"]: {"field_id": f["selector"], "answer": "A specific, truthful answer.",
                                "option": "", "confident": self.confident} for f in fields}

    async def cover_letter(self, job):
        return "Dear Hiring Team,\n\nI am applying...\n\nRiya Sancheti" if self.confident else None


def full_answers() -> Answers:
    data = yaml.safe_load((REPO / "kb" / "answers.example.yaml").read_text())
    data["identity"].update(legal_first_name="Riya", legal_last_name="Sancheti", email="riya@example.com",
                            phone="+1 555 0100", linkedin_url="https://linkedin.com/in/example")
    return Answers(data)


@pytest.fixture
def setup(tmp_path):
    kb = tmp_path / "kb"
    (kb / "resumes").mkdir(parents=True)
    (kb / "resumes" / "resume_business.pdf").write_bytes(b"%PDF-1.4\n")
    (kb / "resumes" / "resume_research.pdf").write_bytes(b"%PDF-1.4\n")
    store = Store(tmp_path / "t.db")
    store.sync_companies([Company(name="Xaira", ats="greenhouse", slug="x"), Company(name="Synthego", ats="lever", slug="s"),
                          Company(name="Benchling", ats="ashby", slug="b"), Company(name="ConsultCo", ats="smartrecruiters", slug="c")])
    ids = {r["name"]: r["id"] for r in store.query("SELECT id, name FROM companies")}
    forms = {}
    now = "2026-10-28T10:00:00+00:00"
    for i, (co, fname, track, score) in enumerate([("Xaira", "greenhouse_xaira", "research", 90),
                                                  ("Synthego", "lever_synthego", "business", 85),
                                                  ("Benchling", "ashby_benchling", "business", 70),
                                                  ("ConsultCo", None, "business", 80)]):
        url = f"https://example.test/{co}/apply"
        ats = {"Xaira": "greenhouse", "Synthego": "lever", "Benchling": "ashby", "ConsultCo": "smartrecruiters"}[co]
        store.upsert_jobs(ids[co], [Job(ats=ats, company=co, company_slug=co.lower(), external_id=str(i),
                                        title=f"Role {i}", url=url, apply_url=url, description="desc")], now)
        if fname:
            forms[url] = json.loads((FORMS / f"{fname}.json").read_text())
        store.conn.execute("UPDATE jobs SET filter_status='pass', score=?, band=?, track=? WHERE company=?",
                           (score, "High" if score >= 75 else "Medium", track, co))
    cfg = load_config()
    acfg = dict(cfg.data["apply"], dry_run=True)
    yield store, acfg, dict(cfg.data["run"]), kb, tmp_path / "out", forms
    store.close()


def engine(setup, driver, writer=None, answers=None, **over):
    store, acfg, run_cfg, kb, out, _ = setup
    return ApplyEngine(store, {**acfg, **over}, run_cfg, answers or full_answers(), kb, out, lambda: driver, writer, "run1")


async def test_dry_run_fills_without_submitting(setup):
    store, *_, forms = setup
    driver = FakeDriver(forms)
    s = await engine(setup, driver, FakeWriter()).run({})
    assert s["selected"] == 3 and s["mode"] == "dry run" and s["dry_run"] == 3
    rows = {r["company"]: r for r in store.query("SELECT * FROM jobs")}
    assert rows["Synthego"]["apply_status"] == "dry_run" and rows["Synthego"]["apply_screenshot"].endswith("_filled.png")
    assert not any(c[0] == "click" for c in driver.calls)
    assert rows["ConsultCo"]["apply_status"] == "manual" and "not auto-filled" in rows["ConsultCo"]["apply_error"]
    details = json.loads(rows["Xaira"]["apply_details"])
    assert any(a["label"] == "Cover letter" and a["value"].endswith(".pdf") for a in details["answers"])
    assert any(c[0] == "pause" for c in driver.calls)                    # human-like pacing between jobs


async def test_submit_confirmed_and_dry_run_jobs_become_eligible_again(setup):
    store, *_, forms = setup
    await engine(setup, FakeDriver(forms), FakeWriter()).run({})          # dry run first
    driver = FakeDriver(forms, "confirmed")
    s = await engine(setup, driver, FakeWriter(), dry_run=False).run({})
    assert s.get("applied") == 3
    row = store.query("SELECT * FROM jobs WHERE company='Synthego'")[0]
    assert row["apply_status"] == "applied" and row["applied_at"] and row["apply_screenshot"].endswith("after_submit.png")
    assert ("click", "#btn-submit") in driver.calls


@pytest.mark.parametrize("outcome,status,reason", [("challenge", "manual", "CAPTCHA"),
                                                   ("error", "manual", "Phone is required"),
                                                   ("unknown", "applied", "verify")])
async def test_submit_outcomes(setup, outcome, status, reason, monkeypatch):
    store, *_, forms = setup
    import jobagent.apply.engine as eng

    async def fast_outcome(driver, timeout_s=0, step_s=0):
        from jobagent.apply.browser import wait_for_outcome
        return await wait_for_outcome(driver, timeout_s=0.0, step_s=1.0)

    monkeypatch.setattr(eng, "wait_for_outcome", fast_outcome)
    await engine(setup, FakeDriver(forms, outcome), FakeWriter(), dry_run=False).run({})
    row = store.query("SELECT * FROM jobs WHERE company='Synthego'")[0]
    assert row["apply_status"] == status and reason in row["apply_error"]


async def test_visible_captcha_goes_manual(setup):
    store, *_, forms = setup
    for f in forms.values():
        f["captcha"] = [{"src": "https://www.recaptcha.net/recaptcha/api2/anchor", "invisible": False}]
    await engine(setup, FakeDriver(forms), FakeWriter()).run({})
    assert {r["apply_status"] for r in store.query("SELECT * FROM jobs WHERE ats<>'smartrecruiters'")} == {"manual"}


async def test_required_dropdown_failure_goes_manual(setup):
    store, *_, forms = setup
    await engine(setup, FakeDriver(forms, failing={"#question_11642284007"}), FakeWriter()).run({})
    row = store.query("SELECT * FROM jobs WHERE company='Xaira'")[0]
    assert row["apply_status"] == "manual" and "could not fill" in row["apply_error"]


async def test_unconfident_writer_sends_job_to_manual_with_reason(setup):
    store, *_, forms = setup
    await engine(setup, FakeDriver(forms), FakeWriter(confident=False)).run({})
    row = store.query("SELECT * FROM jobs WHERE company='Benchling'")[0]
    assert row["apply_status"] == "manual" and "confidently" in row["apply_error"]


async def test_incomplete_answers_block_auto_apply(setup):
    store, *_, forms = setup
    a = full_answers()
    a.data["identity"]["email"] = ""
    driver = FakeDriver(forms)
    s = await engine(setup, driver, FakeWriter(), answers=a).run({})
    assert "answers.yaml incomplete" in s["skipped"] and not driver.calls


def test_selection_quota_split_company_cap_and_user_status(setup):
    store, acfg, run_cfg, *_ = setup
    run_cfg = {**run_cfg, "daily_application_target": {"min": 1, "max": 2}, "track_split": {"business": 0.5, "research": 0.5}}
    auto, manual = select_jobs(store, acfg, run_cfg, dry_run=True)
    assert [j["company"] for j in auto] == ["Xaira", "Synthego"]          # one per track, best scores
    assert [j["company"] for j, _ in manual] == ["ConsultCo"]
    store.conn.execute("UPDATE jobs SET user_status='skip' WHERE company='Synthego'")
    auto, _ = select_jobs(store, acfg, run_cfg, dry_run=True)
    assert [j["company"] for j in auto] == ["Xaira", "Benchling"]
    store.conn.execute("UPDATE jobs SET apply_status='applied', applied_at=datetime('now') WHERE company='Xaira'")
    acfg = {**acfg, "max_per_company_30d": 1}
    from jobagent.models import Job
    xid = store.scalar("SELECT company_id FROM jobs WHERE company='Xaira'")
    store.upsert_jobs(xid, [Job(ats="greenhouse", company="Xaira", company_slug="xaira", external_id="99", title="Another",
                                url="u", apply_url="u")], "2026-10-28T11:00:00+00:00")
    store.conn.execute("UPDATE jobs SET filter_status='pass', score=99, band='High', track='research' WHERE external_id='99'")
    auto, _ = select_jobs(store, acfg, {**run_cfg, "daily_application_target": {"min": 1, "max": 5}}, dry_run=True)
    assert "Another" not in [j["title"] for j in auto]                     # company already applied to this month

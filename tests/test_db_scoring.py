import json

from jobagent.config import load_config
from jobagent.db import Store
from jobagent.kb import load_kb
from jobagent.models import Company, Job
from jobagent.scoring import Scorer, parse_result
from jobagent.scoring.costs import compute_usage

from .conftest import FakeLLM, fake_message


def mk(ext, title="Research Associate", desc="HPLC work"):
    return Job(ats="greenhouse", company="Acme Bio", company_slug="acmebio", external_id=ext, title=title,
               url=f"https://x/{ext}", location="Boston, MA", country="US", description=desc)


def test_upsert_new_changed_unchanged_closed(tmp_path):
    s = Store(tmp_path / "t.db")
    s.sync_companies([Company(name="Acme Bio", ats="greenhouse", slug="acmebio")])
    cid = s.scalar("SELECT id FROM companies")
    assert s.upsert_jobs(cid, [mk("1"), mk("2"), mk("3")], "2026-10-27T10:00:00+00:00")["new"] == 3
    s.conn.execute("UPDATE jobs SET filter_status='pass', score=80, scored_hash=content_hash")
    r = s.upsert_jobs(cid, [mk("1"), mk("2", desc="changed text")], "2026-10-28T10:00:00+00:00")
    assert r == {"new": 0, "changed": 1, "unchanged": 1, "closed": 1}
    rows = {x["external_id"]: x for x in s.query("SELECT * FROM jobs")}
    assert rows["1"]["score"] == 80 and rows["1"]["filter_status"] == "pass"     # untouched
    assert rows["2"]["score"] is None and rows["2"]["filter_status"] is None     # re-filter + re-score
    assert rows["3"]["is_active"] == 0
    s.close()


def test_hydrated_job_is_not_rescored_on_next_fetch(tmp_path):
    s = Store(tmp_path / "t.db")
    s.sync_companies([Company(name="Acme Bio", ats="smartrecruiters", slug="acmebio")])
    cid = s.scalar("SELECT id FROM companies")
    s.upsert_jobs(cid, [mk("1", desc=None)], "2026-10-27T10:00:00+00:00")
    jid = s.scalar("SELECT id FROM jobs")
    s.save_descriptions([("full description", "", "newhash", jid)])
    s.conn.execute("UPDATE jobs SET filter_status='pass', scored_hash=content_hash, score=70")
    r = s.upsert_jobs(cid, [mk("1", desc=None)], "2026-10-28T10:00:00+00:00")
    assert r["unchanged"] == 1
    row = s.query("SELECT * FROM jobs")[0]
    assert row["description"] == "full description" and row["score"] == 70
    assert len(s.jobs_to_score(10)) == 0
    s.close()


def test_compute_usage_costs():
    cfg = load_config()
    u = compute_usage({"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
                       "cache_creation_input_tokens": 0}, "claude-haiku-5-5", cfg.data["pricing"])
    assert abs(u.cost_usd - (1000 * 0.10 + 200 * 0.50 + 5000 * 0.01) / 1e6) < 1e-12
    b = compute_usage({"input_tokens": 1000, "output_tokens": 200}, "claude-haiku-5-5", cfg.data["pricing"], batch=True)
    assert abs(b.cost_usd - (1000 * 0.10 + 200 * 0.50) / 2e6) < 1e-12


def test_parse_result_caps_blocked_and_bands():
    th = {"high": 75, "medium": 50}
    base = {"track": "business", "score": 92, "reason": "r", "matched_skills": ["a"] * 9, "gaps": [],
            "seniority_fit": "entry", "sponsorship": "blocked", "location_ok": True}
    row = parse_result(1, fake_message(base), "m", th)
    assert row["score"] == 20 and row["band"] == "Low" and len(json.loads(row["matched_skills"])) == 5
    row = parse_result(1, fake_message({**base, "sponsorship": "positive", "score": 140}), "m", th)
    assert row["score"] == 100 and row["band"] == "High"
    try:
        parse_result(1, fake_message(base, stop_reason="max_tokens"), "m", th)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


async def test_scorer_realtime_warms_cache_and_respects_budget():
    cfg = load_config()
    kb = load_kb(cfg.path("kb_dir"))
    llm = FakeLLM()
    sc = Scorer(llm, kb, cfg.data["scoring"], cfg.data["pricing"], "run1", budget_left_usd=10.0)
    jobs = [{"id": i, "title": "Research Associate", "company": "X", "description": "HPLC"} for i in range(1, 13)]
    out = await sc.score_realtime(jobs)
    assert len(out.scored) == 12 and not out.errors
    first = llm.messages.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert first["output_config"]["format"]["type"] == "json_schema" and first["output_config"]["effort"] == "low"
    assert "Riya" in first["system"][0]["text"] and "GLP-1" in first["system"][0]["text"]
    assert out.totals.cache_write_tokens == 5000 and out.totals.cache_read_tokens == 11 * 5000
    tight = Scorer(FakeLLM(), kb, cfg.data["scoring"], cfg.data["pricing"], "run2", budget_left_usd=0.0)
    out2 = await tight.score_realtime(jobs)
    assert out2.skipped_budget == 12 and not out2.scored


async def test_scorer_batch_roundtrip():
    cfg = load_config()
    llm = FakeLLM()
    sc = Scorer(llm, load_kb(cfg.path("kb_dir")), cfg.data["scoring"], cfg.data["pricing"], "r", 10.0)
    jobs = [{"id": 7, "title": "Business Development Associate", "company": "X", "description": "BD"}]
    bid = await sc.submit_batch(jobs)
    assert await sc.wait_for_batch(bid, 0, 0)
    out = await sc.collect_batch(bid)
    assert out.scored[0]["job_id"] == 7 and out.scored[0]["band"] == "High" and out.usage_rows[0][4] == 1

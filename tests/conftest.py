import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def fake_message(payload: dict, input_tokens=900, output_tokens=150, cache_read=5000, cache_write=0,
                 stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write),
    )


class FakeMessages:
    """Scores by keyword so tests are deterministic; records every request."""

    def __init__(self):
        self.calls = []
        self.batches = FakeBatches(self)

    def decide(self, params):
        text = params["messages"][0]["content"].lower()
        if "research associate" in text or "scientist" in text:
            return {"track": "research", "score": 88, "reason": "Downstream purification match.",
                    "matched_skills": ["HPLC", "IEX"], "gaps": [], "seniority_fit": "entry",
                    "sponsorship": "not_mentioned", "location_ok": True}
        if "business development" in text or "founder" in text:
            return {"track": "business", "score": 81, "reason": "BD role in biotech.",
                    "matched_skills": ["GTM"], "gaps": ["Pipeline mgmt"], "seniority_fit": "entry",
                    "sponsorship": "positive", "location_ok": True}
        return {"track": "business", "score": 55, "reason": "Adjacent role.", "matched_skills": [],
                "gaps": ["3 years"], "seniority_fit": "mid", "sponsorship": "not_mentioned", "location_ok": True}

    async def create(self, **params):
        self.calls.append(params)
        props = params.get("output_config", {}).get("format", {}).get("schema", {}).get("properties", {})
        if "answers" in props:                       # application answer writer
            text = params["messages"][0]["content"]
            ids = [line.split('"field_id": ')[1].split('"')[1] for line in text.splitlines() if '"field_id": ' in line]
            return fake_message({"answers": [{"field_id": i, "answer": "I use Claude to draft and Excel to model.",
                                              "option": "", "confident": True} for i in ids]})
        if "letter" in props:
            return fake_message({"letter": "Dear Hiring Team,\n\nI am applying.\n\nRiya Sancheti", "confident": True})
        if "subject" in props:                       # outreach email writer
            return fake_message({"subject": "Applied: a role", "body": "Hi there,\n\nI just applied.", "confident": True})
        first = len([c for c in self.calls if "track" in json.dumps(c.get("output_config", {}))]) == 1
        return fake_message(self.decide(params), cache_read=0 if first else 5000, cache_write=5000 if first else 0)


class FakeBatches:
    def __init__(self, messages):
        self.messages = messages
        self.created = {}
        self.retrieve_calls = 0

    async def create(self, requests):
        bid = f"msgbatch_{len(self.created) + 1}"
        self.created[bid] = list(requests)
        return SimpleNamespace(id=bid)

    async def retrieve(self, batch_id):
        self.retrieve_calls += 1
        return SimpleNamespace(processing_status="ended")

    async def results(self, batch_id):
        async def gen():
            for r in self.created[batch_id]:
                yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(
                    type="succeeded", message=fake_message(self.messages.decide(r["params"]))))
        return gen()


class FakeLLM:
    def __init__(self):
        self.messages = FakeMessages()


@pytest.fixture
def project(tmp_path):
    """A throwaway copy of the repo's config and knowledge base."""
    root = tmp_path / "agent"
    (root / "config").mkdir(parents=True)
    shutil.copy(REPO / "config" / "config.yaml", root / "config" / "config.yaml")
    shutil.copytree(REPO / "kb", root / "kb", ignore=shutil.ignore_patterns("*.pdf"))
    (root / "config" / "companies.csv").write_text(
        "name,industry,ats,slug,careers_url,priority,active,notes\n"
        "Acme Bio,biotech,greenhouse,acmebio,,1,1,\n"
        "ChipCo,organ_on_chip,lever,chipco,,2,1,\n"
        "AI Bio,ai_bio,ashby,aibio,,2,1,\n"
        "Medtech Co,diagnostics_medtech,workable,medtechco,,2,1,\n"
        "ConsultCo,consulting,smartrecruiters,ConsultCo,,2,1,\n"
        "Big Pharma,biotech,unsupported,,,2,1,\n", encoding="utf-8")
    return root

"""Field mapping on application forms captured live (tests/fixtures/forms)."""

import json
from pathlib import Path

import pytest
import yaml

from jobagent.apply.answers import Answers, load_answers
from jobagent.apply.mapper import FieldMapper, choose_option

FORMS = Path(__file__).parent / "fixtures" / "forms"
REPO = Path(__file__).resolve().parents[1]


def answers(**identity) -> Answers:
    data = yaml.safe_load((REPO / "kb" / "answers.example.yaml").read_text())
    data["identity"].update({"legal_first_name": "Riya", "legal_last_name": "Sancheti", "email": "riya@example.com",
                             "phone": "+1 555 0100", "linkedin_url": "https://linkedin.com/in/example"}, **identity)
    return Answers(data)


def form(name):
    return json.loads((FORMS / f"{name}.json").read_text())


def plan(name, writer=True, ans=None, resume=REPO / "kb" / "resumes" / "resume_business.pdf"):
    return FieldMapper(ans or answers(), resume, writer).plan(form(name))


def by_cat(p):
    return {a.category: a for a in p.actions}


@pytest.fixture(autouse=True)
def resume_file(tmp_path, monkeypatch):
    # Resumes are git-ignored; make sure a file exists for the upload rule.
    path = REPO / "kb" / "resumes" / "resume_business.pdf"
    if not path.exists():
        path.write_bytes(b"%PDF-1.4\n")
    yield


def test_greenhouse_real_form():
    p = plan("greenhouse_xaira")
    c = by_cat(p)
    assert p.ok
    assert c["first_name"].value == "Riya" and c["last_name"].value == "Sancheti"
    assert c["resume_file"].kind == "upload" and c["resume_file"].value.endswith("resume_business.pdf")
    assert c["auth_without_sponsorship"].kind == "combobox" and c["auth_without_sponsorship"].value == "No"
    assert c["requires_sponsorship"].value == "Yes"
    assert c["sms"].value == "No"
    assert c["eeo_gender"].value.startswith("Decline") and c["eeo_veteran"].value.startswith("Decline")
    assert c["school"].value == "Columbia University" and c["discipline"].value == "Biomedical Engineering"
    labels = [f["label"] for f in p.writer_fields]
    assert any("links to work" in l for l in labels)            # not mistaken for a GitHub-URL field
    assert any("five days per week" in l for l in labels)       # essay box, not a bare "Yes"
    assert p.cover_letter_field and p.cover_letter_field["label"] == "Cover Letter"


def test_lever_real_form():
    p = plan("lever_synthego")
    c = by_cat(p)
    assert p.ok and not p.writer_fields
    assert c["full_name"].value == "Riya Sancheti"
    assert c["authorized_to_work"].kind == "radio" and c["authorized_to_work"].value == "Yes"
    assert c["requires_sponsorship"].kind == "fill" and c["requires_sponsorship"].value.startswith("Yes. I will be on F-1 OPT")
    assert c["start_date"].value.startswith("I can start in early January 2027")
    assert c["how_heard"].value == "Company careers page"
    race = [a for a in p.actions if a.label == "Race"][0]
    assert race.kind == "select" and race.value == "Decline to self-identify"
    disability = [a for a in p.actions if a.label == "Do you have a Disability?"][0]
    assert disability.value == "Decline to specify"
    assert not any(a.category == "pronouns" for a in p.actions)  # optional and empty: left alone


def test_ashby_real_form():
    p = plan("ashby_benchling")
    c = by_cat(p)
    assert p.ok
    assert not any("Autofill" in a.label for a in p.actions)     # never upload into Ashby's autofill box
    assert c["work_auth_status"].value == "Can work for any employer"
    assert c["acknowledge"].value == "I acknowledge"
    assert c["eeo_race"].label == "Ethnicity"                    # not the "city" rule (ethniCITY)
    assert c["city_location"].label == "Location"
    assert [f["label"] for f in p.writer_fields] == ["What AI tools are you currently using today and how are you using them?"]


def test_without_writer_custom_questions_go_manual():
    p = plan("ashby_benchling", writer=False)
    assert not p.ok and any("custom question" in r for r in p.manual_reasons)


def test_missing_answer_goes_manual():
    a = answers()
    a.data["identity"]["phone"] = ""
    p = plan("lever_synthego", ans=a)
    assert any("identity.phone" in r for r in p.manual_reasons)


def test_missing_resume_goes_manual(tmp_path):
    p = plan("lever_synthego", resume=tmp_path / "nope.pdf")
    assert any("resume file missing" in r for r in p.manual_reasons)


def test_sensitive_required_question_goes_manual_never_to_ai():
    f = {"fields": [{"selector": "#q", "type": "radio", "label": "Have you ever been convicted of a felony?",
                     "required": True, "options": [{"value": "y", "text": "Yes"}, {"value": "n", "text": "No"}]}]}
    p = FieldMapper(answers(), REPO / "kb" / "resumes" / "resume_business.pdf", True).plan(f)
    assert not p.writer_fields and any("sensitive" in r for r in p.manual_reasons)


@pytest.mark.parametrize("opts,answer,expected", [
    (["Yes", "No"], "Yes", "Yes"),
    (["Yes, I am authorized", "No"], "Yes", "Yes, I am authorized"),
    (["Select ...", "Male", "Female", "Decline to self-identify"], "Decline to self-identify", "Decline to self-identify"),
    (["Man", "Woman", "Decline to Self Identify"], "Decline to self-identify", "Decline to Self Identify"),
    (["I am not a protected Veteran", "I don't wish to answer"], "Decline to self-identify", "I don't wish to answer"),
    (["Can work for any employer", "Can work for current employer", "Seeking work authorization"],
     "Can work for any employer", "Can work for any employer"),
    (["Bachelor's Degree", "Master's Degree", "Doctorate"], "Master's Degree", "Master's Degree"),
    (["Yes", "No"], "Maybe", None),
])
def test_choose_option(opts, answer, expected):
    got = choose_option([{"value": o, "text": o} for o in opts], answer)
    assert (got["text"] if got else None) == expected


def test_answers_missing_lists_required_fields():
    a = load_answers(REPO / "kb" / "answers.example.yaml")
    assert "identity.email" in a.missing() and "work_authorization.authorized_to_work_in_us" not in a.missing()

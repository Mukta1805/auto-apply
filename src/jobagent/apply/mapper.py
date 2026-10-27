"""Deterministic field mapping: form field -> value from kb/answers.yaml, resume, or (custom questions) the writer.

Input is the output of extract_fields.js. Rules were built from live Greenhouse, Lever and Ashby forms
(tests/fixtures/forms/*.json). Legal, work-authorization and demographic answers only ever come from
answers.yaml; anything sensitive that isn't covered sends the job to the Manual tab.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .answers import Answers

DECLINE = re.compile(r"decline|don'?t wish|do not wish|prefer not|not to (answer|say|disclose|specify)|"
                     r"don'?t want|choose not|rather not|not specified", re.I)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$%+ ]", " ", (s or "").lower())).strip()


@dataclass
class Rule:
    category: str
    pattern: re.Pattern
    source: str                      # dotted key in answers.yaml, or a special source
    types: tuple[str, ...] = ()      # restrict to these field types (empty = any)
    max_label: int = 0               # identity-style rules only match short labels (0 = no limit)


def R(category, pattern, source, types=(), max_label=0):
    return Rule(category, re.compile(pattern, re.I), source, tuple(types), max_label)


SHORT = 60   # "Please share links to your work (GitHub, papers...)" is a question, not a GitHub-URL field
# Categories whose stored answer is a short Yes/No: in an essay box the writer answers instead.
YES_NO_CATEGORIES = {"onsite", "relocate", "authorized_to_work", "auth_without_sponsorship", "over_18",
                     "previously_employed", "background_check"}


# Order matters: the first matching rule wins.
RULES: list[Rule] = [
    R("autofill_upload", r"autofill|auto-fill|parse (your )?resume", "skip", ("file",)),
    R("cover_letter_file", r"cover letter", "cover_letter", ("file",)),
    R("resume_file", r"resume|r[ée]sum[ée]|\bcv\b|curriculum", "resume", ("file",)),
    R("other_file", r".", "skip_optional", ("file",)),
    R("cover_letter_text", r"cover letter", "writer", ("textarea", "text")),
    R("preferred_name", r"preferred (first )?name|nickname", "identity.preferred_name", max_label=SHORT),
    R("first_name", r"^(legal )?first name|^given name|first name$", "identity.legal_first_name", max_label=SHORT),
    R("last_name", r"^(legal )?last name|^surname|family name|last name$", "identity.legal_last_name", max_label=SHORT),
    R("full_name", r"^(full |legal |your )?name$|^full name|^legal name", "full_name", max_label=SHORT),
    R("email", r"e-?mail", "identity.email", ("text", "email"), max_label=SHORT),
    R("phone", r"phone|mobile|cell", "identity.phone", ("text", "tel"), max_label=SHORT),
    R("linkedin", r"linkedin", "identity.linkedin_url", max_label=SHORT),
    R("github", r"github", "identity.github_url", max_label=SHORT),
    R("twitter", r"twitter|\bx\.com", "skip_optional", max_label=SHORT),
    R("website", r"website|portfolio|personal (site|url)|blog", "identity.website_url", ("text", "url"), max_label=SHORT),
    R("pronouns", r"pronoun", "identity.pronouns", max_label=SHORT),
    R("current_company", r"current (company|employer|organi[sz]ation)|^company$|most recent (company|employer)",
      "identity.current_company", max_label=SHORT),
    R("country", r"^country( of residence)?$", "identity.country", max_label=SHORT),
    R("city_location", r"^(current )?location|\bcity\b|where are you (currently )?(located|based)",
      "location_text", max_label=SHORT),
    R("state", r"^state|province", "identity.state", max_label=SHORT),
    R("postal_code", r"zip|postal", "identity.postal_code", max_label=SHORT),
    R("address", r"street|^address", "identity.address_line1", max_label=SHORT),
    # Work authorization: three different questions seen live; order matters.
    R("auth_without_sponsorship", r"authori[sz]ed to work.*without (requiring |the need (for|of) )?"
      r"(employer|company|visa)?[- ]?(sponsor|sponsored)", "work_authorization.authorized_without_sponsorship"),
    R("requires_sponsorship", r"(require|need).{0,40}sponsor|sponsor.{0,40}(require|need)|h-?1b|"
      r"visa sponsorship|immigration sponsorship", "work_authorization.requires_sponsorship_now_or_future"),
    R("work_auth_status", r"work authori[sz]ation status|authori[sz]ation status|visa status|immigration status",
      "work_authorization.work_authorization_status"),
    R("authorized_to_work", r"(legally )?(authori[sz]ed|eligible|permitted) to work", "work_authorization.authorized_to_work_in_us"),
    R("start_date", r"start date|when (can|could|would) you start|how soon|available to start|earliest.*start|notice period",
      "availability.start_date_text"),
    R("relocate", r"relocat", "availability.willing_to_relocate"),
    R("onsite", r"(work|able|willing).{0,60}(office|on-?site|in person|hybrid)|commut|days (a|per) week",
      "availability.can_work_onsite"),
    R("salary", r"salary|compensation|desired pay|pay (range|expectation)|expected (base|pay|comp)", "salary"),
    R("how_heard", r"how did you (hear|find|learn|come across)|where did you (hear|find)|^source$|referral source",
      "defaults.how_did_you_hear"),
    R("referred_by", r"referred by|referr(er|al) name|who referred", "defaults.referred_by"),
    R("over_18", r"18 years|at least 18|over (the age of )?18", "defaults.over_18"),
    R("previously_employed", r"(previously|ever|before).{0,30}(employed|worked).{0,30}(by|for|at)|former employee",
      "defaults.previously_employed_here"),
    R("background_check", r"background check", "defaults.background_check_consent"),
    R("sms", r"\bsms\b|text messag", "defaults.sms_consent"),
    R("acknowledge", r"acknowledge|privacy (policy|notice)|i (agree|consent|certify|confirm) .*(policy|terms)",
      "acknowledge"),
    R("school", r"school|university|college|institution", "education.0.school", max_label=SHORT),
    R("degree", r"^degree|degree (type|level)|highest degree", "education.0.degree", max_label=SHORT),
    R("discipline", r"discipline|major|field of study|area of study", "education.0.discipline", max_label=SHORT),
    R("gpa", r"\bgpa\b|grade point", "education.0.gpa", max_label=SHORT),
    R("graduation", r"graduat", "graduation", max_label=SHORT),
    # Demographic questions: always from answers.yaml (default "decline").
    R("eeo_hispanic", r"hispanic|latin[oa]", "eeo.hispanic_latino"),
    R("eeo_gender", r"gender|^sex\b|sex assigned", "eeo.gender"),
    R("eeo_race", r"\brace\b|ethnicit", "eeo.race_ethnicity"),
    R("eeo_veteran", r"veteran", "eeo.veteran_status"),
    R("eeo_disability", r"disabilit", "eeo.disability_status"),
    R("eeo_orientation", r"sexual orientation", "eeo.sexual_orientation"),
    R("eeo_transgender", r"transgender", "eeo.transgender"),
    R("eeo_age", r"age range|your age|date of birth", "eeo.age_range"),
]

# Questions that must never be answered by AI. If no rule above covered them, the job goes to Manual.
SENSITIVE = re.compile(r"criminal|convict|felony|misdemeanor|clearance|citizen|social security|\bssn\b|"
                       r"salary history|current (salary|compensation)|non-?compete|signature|sign here|"
                       r"religio|pregnan|marital|date of birth|passport", re.I)


@dataclass
class Action:
    selector: str
    kind: str                         # fill | select | radio | checkbox | combobox | upload
    value: str = ""
    option_selector: str = ""         # radio/checkbox option to click
    label: str = ""
    category: str = ""
    source: str = ""                  # answers key, resume, writer, ...
    required: bool = False


@dataclass
class FormPlan:
    actions: list[Action] = field(default_factory=list)
    writer_fields: list[dict] = field(default_factory=list)     # custom questions for the writer
    manual_reasons: list[str] = field(default_factory=list)
    skipped_optional: list[str] = field(default_factory=list)
    cover_letter_field: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.manual_reasons


def choose_option(options: list[dict], answer: str) -> dict | None:
    """Best option for an answer. Yes/No and 'decline' are matched by meaning, the rest by text."""
    if not options or answer in (None, ""):
        return None
    opts = [o for o in options if norm(o.get("text", "")) and not re.match(r"^(select|choose|please select)", norm(o["text"]))]
    a = norm(str(answer))
    if DECLINE.search(str(answer)):
        return next((o for o in opts if DECLINE.search(o["text"])), None)
    if a in ("yes", "no"):
        return next((o for o in opts if norm(o["text"]).split(" ")[0] == a), None)
    for test in (lambda t: t == a, lambda t: t.startswith(a) or a.startswith(t),
                 lambda t: a in t or t in a):
        hit = [o for o in opts if test(norm(o["text"]))]
        if hit:
            return min(hit, key=lambda o: abs(len(norm(o["text"])) - len(a)))
    words = set(a.split())
    scored = [(len(words & set(norm(o["text"]).split())) / max(1, len(words)), o) for o in opts]
    best = max(scored, key=lambda x: x[0], default=(0, None))
    return best[1] if best[0] >= 0.6 else None


class FieldMapper:
    def __init__(self, answers: Answers, resume_path: Path, writer_available: bool, cover_letter_auto: bool = True):
        self.answers = answers
        self.resume_path = resume_path
        self.writer_available = writer_available
        self.cover_letter_auto = cover_letter_auto

    def classify(self, f: dict) -> Rule | None:
        label = (f.get("label") or "").strip()
        hay = label or f.get("placeholder", "") or f.get("name", "")
        ftype = f.get("type", "")
        for rule in RULES:
            if rule.types and ftype not in rule.types:
                continue
            if rule.max_label and len(label) > rule.max_label:
                continue
            if rule.pattern.search(hay) or (rule.category in ("full_name", "email", "phone", "resume_file")
                                            and rule.pattern.search(f.get("name", "") or "")):
                return rule
        if f.get("name") in ("_systemfield_name",):
            return R("full_name", r".", "full_name")
        return None

    def value_for(self, rule: Rule) -> str:
        a = self.answers
        special = {
            "full_name": a.full_name, "location_text": a.location_text,
            "salary": a.get("compensation.expected_salary_text"),
            "graduation": self._graduation(),
        }
        if rule.source in special:
            return str(special[rule.source] or "")
        return str(a.get(rule.source, "") or "")

    def _graduation(self) -> str:
        end = str(self.answers.get("education.0.end", ""))
        m = re.match(r"(\d{4})-(\d{2})", end)
        if not m:
            return end
        months = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                  "October", "November", "December"]
        return f"{months[int(m.group(2)) - 1]} {m.group(1)}"

    def plan(self, form: dict) -> FormPlan:
        plan = FormPlan()
        for f in form.get("fields", []):
            label = f.get("label") or f.get("placeholder") or f.get("name") or f.get("selector")
            required = bool(f.get("required"))
            ftype = f.get("type", "text")
            rule = self.classify(f)

            if rule is None:
                if SENSITIVE.search(label):
                    if required:
                        plan.manual_reasons.append(f"sensitive question needs Riya: {label[:90]}")
                    else:
                        plan.skipped_optional.append(label)
                    continue
                if required:
                    if self.writer_available:
                        plan.writer_fields.append(f)
                    else:
                        plan.manual_reasons.append(f"custom question needs an answer (no API key): {label[:90]}")
                else:
                    plan.skipped_optional.append(label)
                continue

            src = rule.source
            if src == "skip":
                continue
            if src == "skip_optional":
                if required:
                    plan.manual_reasons.append(f"required upload the tool can't provide: {label[:90]}")
                continue
            if src == "resume":
                if not self.resume_path.exists():
                    plan.manual_reasons.append(f"resume file missing: {self.resume_path.name}")
                    continue
                plan.actions.append(Action(f["selector"], "upload", str(self.resume_path), label=label,
                                           category=rule.category, source="resume", required=required))
                continue
            if src == "cover_letter":
                if required:
                    if self.writer_available and self.cover_letter_auto:
                        plan.cover_letter_field = f
                    else:
                        plan.manual_reasons.append("cover letter required")
                continue
            if src == "writer":
                if required or rule.category == "cover_letter_text":
                    if self.writer_available and (self.cover_letter_auto or rule.category != "cover_letter_text"):
                        plan.writer_fields.append(f)
                    elif required:
                        plan.manual_reasons.append(f"needs written answer: {label[:90]}")
                continue
            if src == "acknowledge":
                if self.answers.get("defaults.accept_privacy_acknowledgement") is True:
                    self._add_choice(plan, f, "yes", rule, label, required, prefer_any=True)
                elif required:
                    plan.manual_reasons.append(f"acknowledgement needs Riya: {label[:90]}")
                continue

            if ftype == "textarea" and rule.category in YES_NO_CATEGORIES:
                # An essay box asking e.g. how 5 days in-office fits: a bare "Yes" reads badly.
                if self.writer_available:
                    plan.writer_fields.append(f)
                elif required:
                    plan.manual_reasons.append(f"needs written answer: {label[:90]}")
                continue

            value = self.value_for(rule)
            if rule.category == "requires_sponsorship" and ftype in ("text", "textarea") and value.lower() == "yes":
                explanation = self.answers.get("work_authorization.sponsorship_explanation")
                value = f"Yes. {explanation}" if explanation else value
            if not value:
                if required:
                    plan.manual_reasons.append(f"missing in answers.yaml ({rule.source}): {label[:90]}")
                else:
                    plan.skipped_optional.append(label)
                continue
            if ftype in ("select", "radio", "checkbox", "combobox"):
                self._add_choice(plan, f, value, rule, label, required)
            else:
                maxlen = f.get("maxlength") or 0
                if maxlen and len(value) > maxlen:
                    value = value[:maxlen]
                plan.actions.append(Action(f["selector"], "fill", value, label=label, category=rule.category,
                                           source=rule.source, required=required))
        return plan

    def _add_choice(self, plan: FormPlan, f: dict, value: str, rule: Rule, label: str, required: bool,
                    prefer_any: bool = False) -> None:
        ftype = f.get("type")
        options = f.get("options")
        if ftype == "combobox" and not options:
            # Options load only after typing (Greenhouse react-select); the browser picks the best match.
            plan.actions.append(Action(f["selector"], "combobox", value, label=label, category=rule.category,
                                       source=rule.source, required=required))
            return
        opt = choose_option(options or [], value)
        if opt is None and prefer_any and options and len(options) == 1:
            opt = options[0]                       # single "I acknowledge" option
        if opt is None:
            if required:
                plan.manual_reasons.append(f"no option matches {value!r} for: {label[:80]}")
            else:
                plan.skipped_optional.append(label)
            return
        kind = {"select": "select", "radio": "radio", "checkbox": "checkbox", "combobox": "combobox"}[ftype]
        plan.actions.append(Action(f["selector"], kind, opt.get("value") or opt.get("text"),
                                   option_selector=opt.get("selector", ""), label=label, category=rule.category,
                                   source=rule.source, required=required))

    def add_writer_answers(self, plan: FormPlan, answers: dict[str, dict]) -> None:
        """Merge writer output ({selector: {answer, option, confident}}) into the plan."""
        for f in plan.writer_fields:
            res = answers.get(f["selector"])
            label = f.get("label", "")[:90]
            if not res or not res.get("confident") or not (res.get("answer") or res.get("option")):
                if f.get("required"):
                    plan.manual_reasons.append(f"couldn't answer confidently: {label}")
                continue
            ftype = f.get("type")
            if ftype in ("select", "radio", "checkbox") or (ftype == "combobox" and f.get("options")):
                opt = choose_option(f.get("options") or [], res.get("option") or res.get("answer"))
                if opt is None:
                    if f.get("required"):
                        plan.manual_reasons.append(f"writer's choice isn't an option: {label}")
                    continue
                plan.actions.append(Action(f["selector"], ftype if ftype != "checkbox" else "checkbox",
                                           opt.get("value") or opt.get("text"), opt.get("selector", ""),
                                           f.get("label", ""), "custom", "writer", bool(f.get("required"))))
            elif ftype == "combobox":
                plan.actions.append(Action(f["selector"], "combobox", res.get("option") or res["answer"],
                                           label=f.get("label", ""), category="custom", source="writer",
                                           required=bool(f.get("required"))))
            else:
                text = res["answer"]
                if f.get("maxlength") and len(text) > f["maxlength"]:
                    text = text[: f["maxlength"]]
                plan.actions.append(Action(f["selector"], "fill", text, label=f.get("label", ""),
                                           category="custom", source="writer", required=bool(f.get("required"))))


def summarize(plan: FormPlan) -> dict[str, Any]:
    return {"actions": len(plan.actions), "writer_fields": len(plan.writer_fields),
            "manual_reasons": plan.manual_reasons, "skipped_optional": len(plan.skipped_optional)}

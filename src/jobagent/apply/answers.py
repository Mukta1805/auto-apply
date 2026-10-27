"""Riya's fixed application answers (kb/answers.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Values the tool cannot apply without. Missing ones are listed by `doctor` and block auto-apply.
REQUIRED_KEYS = [
    "identity.legal_first_name", "identity.legal_last_name", "identity.email", "identity.phone",
    "identity.city", "identity.state", "identity.country", "identity.linkedin_url",
    "work_authorization.authorized_to_work_in_us", "work_authorization.authorized_without_sponsorship",
    "work_authorization.requires_sponsorship_now_or_future", "availability.earliest_start_date",
]
# Useful but optional: the matching form fields are simply left for Riya if empty.
RECOMMENDED_KEYS = ["identity.postal_code", "identity.address_line1", "work_authorization.opt_ead_expected_start"]


@dataclass
class Answers:
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, dotted: str, default: Any = "") -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    return default
            elif isinstance(node, dict):
                node = node.get(part)
            else:
                return default
            if node is None:
                return default
        return node

    def missing(self, keys: list[str] = REQUIRED_KEYS) -> list[str]:
        return [k for k in keys if str(self.get(k, "")).strip() == ""]

    @property
    def full_name(self) -> str:
        return f"{self.get('identity.legal_first_name')} {self.get('identity.legal_last_name')}".strip()

    @property
    def location_text(self) -> str:
        city, state = self.get("identity.city"), self.get("identity.state")
        return ", ".join(x for x in (city, state) if x)

    def summary_for_writer(self) -> str:
        """Non-sensitive facts the answer writer may use (no EEO data, no contact details)."""
        keys = ["work_authorization.visa_status", "work_authorization.sponsorship_explanation",
                "availability.start_date_text", "availability.willing_to_relocate", "availability.work_arrangement",
                "compensation.expected_salary_text"]
        return "\n".join(f"- {k.split('.')[-1]}: {self.get(k)}" for k in keys if self.get(k))


def load_answers(path: Path) -> Answers:
    if not path.exists():
        return Answers({})
    with open(path, encoding="utf-8") as fh:
        return Answers(yaml.safe_load(fh) or {})

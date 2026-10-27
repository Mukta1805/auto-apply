"""Riya's knowledge base: profile markdown files and steering lists in kb/."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class KnowledgeBase:
    candidate: str
    business: str
    research: str
    lists: dict = field(default_factory=dict)

    @property
    def avoid_companies(self) -> list[str]:
        return list(self.lists.get("avoid_companies") or [])

    @property
    def dream_companies(self) -> list[str]:
        return list(self.lists.get("dream_companies") or [])

    @property
    def already_applied(self) -> list[str]:
        return list(self.lists.get("already_applied") or [])


def load_kb(kb_dir: Path) -> KnowledgeBase:
    def read(name: str) -> str:
        p = kb_dir / name
        if not p.exists():
            raise FileNotFoundError(f"Missing knowledge-base file: {p}")
        return p.read_text(encoding="utf-8").strip()

    lists_path = kb_dir / "lists.yaml"
    lists = yaml.safe_load(lists_path.read_text(encoding="utf-8")) if lists_path.exists() else {}
    return KnowledgeBase(candidate=read("candidate.md"), business=read("profile_business.md"),
                         research=read("profile_research.md"), lists=lists or {})

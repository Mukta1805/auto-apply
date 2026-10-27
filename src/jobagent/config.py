"""Configuration loading: config/config.yaml + .env, with paths resolved from the repo root."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.yaml"


class Section(dict):
    """A dict that also allows attribute access (cfg.scoring.model)."""

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError as exc:  # pragma: no cover - surfaced as AttributeError
            raise AttributeError(name) from exc
        return Section(value) if isinstance(value, dict) and not isinstance(value, Section) else value


@dataclass
class Config:
    data: Section
    root: Path = REPO_ROOT
    env: dict[str, str] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.data, name)

    def path(self, key: str) -> Path:
        """Absolute path for a key under `paths:`."""
        p = Path(self.data["paths"][key])
        return p if p.is_absolute() else self.root / p

    def secret(self, name: str) -> str | None:
        value = self.env.get(name) or os.environ.get(name)
        return value.strip() if value else None


def load_config(config_path: Path | None = None, root: Path | None = None) -> Config:
    root = root or REPO_ROOT
    config_path = config_path or root / "config" / "config.yaml"
    load_dotenv(root / ".env", override=False)
    with open(config_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    env = {k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "APOLLO_", "CONTACT_", "JOBAGENT_"))}
    if env.get("JOBAGENT_LOG_LEVEL"):
        raw.setdefault("logging", {})["level"] = env["JOBAGENT_LOG_LEVEL"]
    return Config(data=Section(raw), root=root, env=env)

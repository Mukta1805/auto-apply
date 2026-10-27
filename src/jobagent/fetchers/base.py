"""Fetcher interface. One fetcher per job-board type (ATS)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..http import HttpClient
from ..models import Job


@dataclass(slots=True)
class BoardResult:
    status: str                      # ok | not_modified | not_found | error
    jobs: list[Job] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    elapsed_ms: int = 0


class Fetcher:
    ats: ClassVar[str] = ""
    host: ClassVar[str] = ""
    needs_detail: ClassVar[bool] = False   # listing has no description; call hydrate() for candidates

    def board_url(self, slug: str) -> str:
        raise NotImplementedError

    def board_params(self) -> dict[str, Any] | None:
        return None

    def parse(self, data: Any, company: str, slug: str) -> list[Job]:
        raise NotImplementedError

    def looks_valid(self, data: Any) -> bool:
        """Used by ATS detection: does this response look like a real job board?"""
        return data is not None

    async def fetch(self, http: HttpClient, company: str, slug: str,
                    etag: str | None = None, last_modified: str | None = None) -> BoardResult:
        res = await http.get_json(self.board_url(slug), params=self.board_params(), etag=etag,
                                  last_modified=last_modified, expect_404=True)
        if res.not_modified:
            return BoardResult("not_modified", etag=etag, last_modified=last_modified, elapsed_ms=res.elapsed_ms)
        if res.status == 404:
            return BoardResult("not_found", error="board not found (404)", elapsed_ms=res.elapsed_ms)
        if not res.ok:
            return BoardResult("error", error=res.error, elapsed_ms=res.elapsed_ms)
        try:
            jobs = self.parse(res.data, company, slug)
        except (KeyError, TypeError, ValueError) as exc:
            return BoardResult("error", error=f"parse error: {exc!r}", elapsed_ms=res.elapsed_ms)
        return BoardResult("ok", jobs=jobs, etag=res.etag, last_modified=res.last_modified, elapsed_ms=res.elapsed_ms)

    async def hydrate(self, http: HttpClient, slug: str, external_id: str) -> tuple[str, str] | None:
        """Return (description_text, apply_url) for boards whose listing lacks descriptions."""
        return None

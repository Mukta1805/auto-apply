"""Job-board fetchers, keyed by ATS name."""

from .ashby import AshbyFetcher
from .base import BoardResult, Fetcher
from .greenhouse import GreenhouseFetcher
from .lever import LeverFetcher
from .smartrecruiters import SmartRecruitersFetcher
from .workable import WorkableFetcher

FETCHERS: dict[str, Fetcher] = {f.ats: f for f in (
    GreenhouseFetcher(), LeverFetcher(), AshbyFetcher(), WorkableFetcher(), SmartRecruitersFetcher())}

__all__ = ["FETCHERS", "Fetcher", "BoardResult"]

"""Token accounting and cost calculation from API usage objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UsageTotals:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)

    def add(self, model: str, u: "TokenUsage") -> None:
        self.requests += 1
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_read_tokens += u.cache_read_tokens
        self.cache_write_tokens += u.cache_write_tokens
        self.cost_usd += u.cost_usd
        self.by_model[model] = self.by_model.get(model, 0.0) + u.cost_usd

    def as_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens,
                "cost_usd": round(self.cost_usd, 4)}


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float


def compute_usage(usage: Any, model: str, pricing: dict, batch: bool = False, cache_ttl: str = "5m") -> TokenUsage:
    """`usage` is the SDK Usage object (or a dict with the same keys)."""
    get = (lambda k: usage.get(k)) if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    inp = int(get("input_tokens") or 0)
    out = int(get("output_tokens") or 0)
    cr = int(get("cache_read_input_tokens") or 0)
    cw = int(get("cache_creation_input_tokens") or 0)
    prices = pricing["models"].get(model)
    if not prices:
        return TokenUsage(inp, out, cr, cw, 0.0)
    write_rate = prices.get("cache_write_1h", prices["cache_write"]) if cache_ttl == "1h" else prices["cache_write"]
    cost = (inp * prices["input"] + out * prices["output"] + cr * prices["cache_read"] + cw * write_rate) / 1_000_000
    if batch:
        cost *= float(pricing.get("batch_discount", 0.5))
    return TokenUsage(inp, out, cr, cw, round(cost, 6))

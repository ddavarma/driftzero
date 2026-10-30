"""Token -> USD accounting for Meta models.

Muse's `run.usage` reports, per model: input_tokens (cached_tokens is a subset of it),
output_tokens (reasoning_tokens is a subset of it). So:
    usd = (input - cached) * in_rate + cached * cached_rate + output * out_rate
"""
from __future__ import annotations

from dataclasses import dataclass

# USD per 1M tokens: (input, cached input, output).
# Rates as of 2026-09, verify at dev.meta.ai/docs/models
RATES: dict[str, tuple[float, float, float]] = {
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.3-contributor": (0.10, 0.002, 0.20),
    "muse-wonder-1.0": (4.00, 0.20, 20.00),
    # Muse Spark Open (open weights), served by Baseten / Fireworks.
    # Rates as of 2026-09, verify at dev.meta.ai/docs/models
    "meta-models/muse-spark-open-1026": (1.25, 0.15, 4.25),
    "accounts/fireworks/models/muse-spark-open": (1.25, 0.15, 4.25),
}


@dataclass
class Cost:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    usd: float | None = 0.0

    def __add__(self, other: Cost) -> Cost:
        usd = None if self.usd is None or other.usd is None else self.usd + other.usd
        return Cost(self.input_tokens + other.input_tokens, self.cached_tokens + other.cached_tokens,
                    self.output_tokens + other.output_tokens, self.reasoning_tokens + other.reasoning_tokens, usd)

    @property
    def cached_pct(self) -> float:
        return 100.0 * self.cached_tokens / self.input_tokens if self.input_tokens else 0.0

    def as_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "cached_tokens": self.cached_tokens,
                "output_tokens": self.output_tokens, "reasoning_tokens": self.reasoning_tokens,
                "cached_pct": round(self.cached_pct, 1), "usd": None if self.usd is None else round(self.usd, 4)}


def rate_for(model: str | None, contributor: bool = False) -> tuple[float, float, float] | None:
    if not model:
        return None
    if contributor and not model.endswith("-contributor"):
        model = f"{model}-contributor"
    return RATES.get(model)


def usd(model: str | None, input_tokens: int, cached_tokens: int, output_tokens: int,
        contributor: bool = False) -> float | None:
    rate = rate_for(model, contributor)
    if rate is None:
        return None
    r_in, r_cached, r_out = rate
    uncached = max(0, input_tokens - cached_tokens)
    return (uncached * r_in + cached_tokens * r_cached + output_tokens * r_out) / 1_000_000


def cost_from_usage(usage: dict | None, *, default_model: str | None = None, contributor: bool = False,
                    free: bool = False) -> Cost:
    """Cost of one Muse run from its `run.usage` payload. `free` for the echo provider."""
    total = Cost()
    for m in (usage or {}).get("models") or []:
        model = m.get("model") or default_model
        c = Cost(m.get("input_tokens") or 0, m.get("cached_tokens") or 0, m.get("output_tokens") or 0,
                 m.get("reasoning_tokens") or 0)
        c.usd = 0.0 if free else usd(model, c.input_tokens, c.cached_tokens, c.output_tokens, contributor)
        total = total + c
    return total

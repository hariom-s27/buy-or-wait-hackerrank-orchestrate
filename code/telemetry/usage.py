"""
Telemetry logging for LLM/VLM calls (Step 10).

Appends one JSONL record per extraction call or cache hit to:
- evaluation/usage_report.jsonl
- usage.jsonl
Schema:
{ts, model, provider, purpose, source_ids, input_tokens, cached_tokens,
 output_tokens, cost, latency_ms, cache_hit, ok, validation_result}
"""
from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import Any, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
USAGE_JSONL_PATHS = [
    _REPO_ROOT / "usage.jsonl",
    _REPO_ROOT / "evaluation" / "usage_report.jsonl",
]

_LOCK = threading.Lock()

# Anthropic first-party API rates, USD per 1M tokens (cached: claude-api skill, 2026-06-24).
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model: (input_per_million, output_per_million)
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "deterministic_regex": (0.0, 0.0),
}


def compute_cost(model: str, input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    """Compute USD cost from real token counts. Returns None (never 0) when a
    token count is unavailable — an unknown cost must never be fabricated as
    free, per Step 10 section 13."""
    if input_tokens is None or output_tokens is None:
        return None
    rates = MODEL_PRICING.get(model)
    if rates is None:
        return None
    return round((input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000, 6)


def log_usage(
    *,
    model: str,
    provider: str,
    purpose: str,
    source_ids: List[str],
    input_tokens: Optional[int] = 0,
    cached_tokens: Optional[int] = 0,
    output_tokens: Optional[int] = 0,
    latency_ms: float = 0.0,
    cache_hit: bool = False,
    ok: bool = True,
    validation_result: str = "passed",
    cost: Optional[float] = None,
) -> dict[str, Any]:
    """Append one structured usage record to telemetry logs.

    Token counts and cost are Optional[int]/Optional[float]: when the
    provider's usage metadata is unavailable (e.g. a cache hit that never
    called the API, or the regex fallback path), pass None rather than 0 so
    the record honestly reflects "not applicable" instead of fabricating a
    zero cost for a call that was never measured.
    """
    if cost is None and not cache_hit:
        cost = compute_cost(model, input_tokens, output_tokens)
    elif cache_hit:
        cost = 0.0  # a cache hit performs no API call: zero cost is measured fact, not a guess

    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "model": model,
        "provider": provider,
        "purpose": purpose,
        "source_ids": source_ids,
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
        "latency_ms": round(latency_ms, 2),
        "cache_hit": cache_hit,
        "ok": ok,
        "validation_result": validation_result,
    }

    line = json.dumps(record) + "\n"
    with _LOCK:
        for path in USAGE_JSONL_PATHS:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass

    return record


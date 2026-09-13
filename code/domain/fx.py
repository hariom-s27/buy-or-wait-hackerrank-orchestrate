"""
FX conversion helper (Step 2).

Looks up exchange_rates.csv for the requested currency pair and the
nearest rate_date to ``on_date``. Falls back to the reverse pair
(1 / rate) when the direct pair is unavailable.

Every conversion is logged so later steps can audit FX usage.
"""
from __future__ import annotations

import datetime
import logging
from typing import Dict, List, Optional, Tuple

from code.domain.models import ExchangeRate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level rate table (populated by init_rates)
# ---------------------------------------------------------------------------

# Keyed by (from_ccy, to_ccy) -> sorted list of (rate_date, rate)
_rate_table: Dict[Tuple[str, str], List[Tuple[datetime.date, float]]] = {}

_conversion_log: List[dict] = []


def init_rates(exchange_rates: List[ExchangeRate]) -> None:
    """Build the rate lookup table from loaded exchange rate objects."""
    _rate_table.clear()
    for er in exchange_rates:
        key = (er.from_currency, er.to_currency)
        _rate_table.setdefault(key, []).append((er.rate_date, er.rate))
    # Sort each list by date for nearest-date lookup
    for key in _rate_table:
        _rate_table[key].sort(key=lambda x: x[0])


def _find_nearest(rates: List[Tuple[datetime.date, float]], on_date: datetime.date) -> float:
    """Return the rate whose date is nearest to ``on_date``."""
    best_rate = rates[0][1]
    best_dist = abs((rates[0][0] - on_date).days)
    for rd, rate in rates[1:]:
        dist = abs((rd - on_date).days)
        if dist < best_dist:
            best_dist = dist
            best_rate = rate
    return best_rate


def convert(
    amount: float,
    from_ccy: str,
    to_ccy: str,
    on_date: datetime.date,
) -> float:
    """Convert ``amount`` from ``from_ccy`` to ``to_ccy`` on ``on_date``.

    Rules (BUILD-PLAN.md Step 2):
    - If from_ccy == to_ccy, return amount unchanged.
    - Look up the direct (from_ccy, to_ccy) pair first.
    - If unavailable, use the reverse pair with 1/rate.
    - Select the rate_date nearest to on_date.
    - Log every conversion for auditability.
    - Deterministic: same inputs always give same output.
    """
    if from_ccy == to_ccy:
        return amount

    direct_key = (from_ccy, to_ccy)
    reverse_key = (to_ccy, from_ccy)

    rate: Optional[float] = None
    direction = "direct"

    if direct_key in _rate_table:
        rate = _find_nearest(_rate_table[direct_key], on_date)
        direction = "direct"
    elif reverse_key in _rate_table:
        reverse_rate = _find_nearest(_rate_table[reverse_key], on_date)
        rate = 1.0 / reverse_rate
        direction = "reverse"
    else:
        raise ValueError(
            f"No exchange rate found for {from_ccy} -> {to_ccy} "
            f"(or reverse). Available pairs: {sorted(_rate_table.keys())}"
        )

    result = amount * rate

    # Log the conversion
    entry = {
        "amount": amount,
        "from_ccy": from_ccy,
        "to_ccy": to_ccy,
        "on_date": str(on_date),
        "rate": rate,
        "direction": direction,
        "result": result,
    }
    _conversion_log.append(entry)
    logger.debug(
        "FX: %.2f %s -> %.2f %s @ rate=%.6f (%s, date=%s)",
        amount, from_ccy, result, to_ccy, rate, direction, on_date,
    )

    return result


def get_conversion_log() -> List[dict]:
    """Return the full list of FX conversions performed (for auditing)."""
    return list(_conversion_log)


def clear_conversion_log() -> None:
    """Clear the conversion log."""
    _conversion_log.clear()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Quick self-test: USD -> INR at the supplied rate of 83.33."""
    import sys

    # Build a minimal rate table for the test
    test_rates = [
        ExchangeRate(
            rate_date=datetime.date(2024, 1, 15),
            from_currency="USD",
            to_currency="INR",
            rate=83.33,
        ),
    ]
    init_rates(test_rates)

    # Test direct conversion
    result = convert(100.0, "USD", "INR", datetime.date(2024, 1, 15))
    expected = 100.0 * 83.33
    assert abs(result - expected) < 0.01, f"Expected {expected}, got {result}"
    print(f"PASS: 100 USD -> {result:.2f} INR (expected {expected:.2f})")

    # Test same currency
    result_same = convert(500.0, "INR", "INR", datetime.date(2024, 1, 15))
    assert result_same == 500.0, f"Same-currency conversion failed: {result_same}"
    print(f"PASS: 500 INR -> {result_same:.2f} INR (same currency, unchanged)")

    # Test reverse lookup
    result_rev = convert(8333.0, "INR", "USD", datetime.date(2024, 1, 15))
    expected_rev = 8333.0 / 83.33
    assert abs(result_rev - expected_rev) < 0.01, f"Expected {expected_rev}, got {result_rev}"
    print(f"PASS: 8333 INR -> {result_rev:.2f} USD (reverse lookup, expected {expected_rev:.2f})")

    print("FX self-test: ALL PASSED")


if __name__ == "__main__":
    _self_test()


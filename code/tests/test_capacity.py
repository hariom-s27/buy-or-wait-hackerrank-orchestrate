"""Synthetic tests for Step 5 (capacity): amount_safe_to_pay and
earliest_date_for_full_payment. No real user/request IDs; the separate
real-data sanity check (BUILD-PLAN.md Step 5 checklist) is run ad hoc and
never writes output.csv.
"""
from __future__ import annotations

import inspect
import math
import random
from datetime import date, timedelta

import pytest

from code.forecast import capacity
from code.forecast.capacity import (
    amount_safe_to_pay,
    amount_safe_to_pay_bisect,
    earliest_date_for_full_payment,
)
from code.forecast.ledger import trough


def d(value):
    return date.fromisoformat(value)


REQUEST_DATE = d("2025-02-01")
HORIZON = 90


def make_ledger(movements: dict, start: date = REQUEST_DATE, horizon: int = HORIZON) -> dict:
    """A complete [start, start+horizon] ledger, 0 net movement unless overridden."""
    return {start + timedelta(days=i): movements.get(start + timedelta(days=i), 0.0)
            for i in range(horizon + 1)}


# ---------------------------------------------------------------------------
# 1. CLOSED FORM VS BISECTION -- explicit synthetic ledgers
# ---------------------------------------------------------------------------

def test_closed_form_matches_bisection_on_synthetic_ledgers():
    cases = [
        (make_ledger({REQUEST_DATE: -200, REQUEST_DATE + timedelta(days=30): 1000}), 500, 100, 300),
        (make_ledger({REQUEST_DATE: 50}), 1000, 200, 900),
        (make_ledger({}), 100, 50, 1000),
        (make_ledger({REQUEST_DATE + timedelta(days=10): -5000}), 6000, 0, 6000),
        (make_ledger({REQUEST_DATE + timedelta(days=60): 2000, REQUEST_DATE + timedelta(days=5): -1500}),
         2000, 300, 4000),
    ]
    for ledger, balance, minimum, requested in cases:
        closed = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, requested)
        bisected = amount_safe_to_pay_bisect(ledger, balance, minimum, REQUEST_DATE, requested)
        assert math.isclose(closed, bisected, abs_tol=1e-6)
        assert 0 <= closed <= requested


def test_closed_form_calls_trough_exactly_once(monkeypatch):
    calls = {"n": 0}
    original_trough = capacity.trough

    def counting_trough(*args, **kwargs):
        calls["n"] += 1
        return original_trough(*args, **kwargs)

    monkeypatch.setattr(capacity, "trough", counting_trough)
    ledger = make_ledger({REQUEST_DATE + timedelta(days=10): -300})
    amount_safe_to_pay(ledger, 1000, 100, REQUEST_DATE, 500)
    assert calls["n"] == 1


def test_bisection_reference_calls_trough_many_times(monkeypatch):
    calls = {"n": 0}
    original_trough = capacity.trough

    def counting_trough(*args, **kwargs):
        calls["n"] += 1
        return original_trough(*args, **kwargs)

    monkeypatch.setattr(capacity, "trough", counting_trough)
    ledger = make_ledger({REQUEST_DATE + timedelta(days=10): -300})
    # requested_amount=1000 lands strictly between the 0/full-amount early
    # exits (safe amount is 600), forcing genuine bisection iterations.
    amount_safe_to_pay_bisect(ledger, 1000, 100, REQUEST_DATE, 1000)
    assert calls["n"] > 5


def test_bisection_reference_is_documented_as_test_only():
    doc = amount_safe_to_pay_bisect.__doc__.lower()
    assert "test" in doc
    assert "production" in doc


# ---------------------------------------------------------------------------
# 2 & 3. LOWER / UPPER CLAMP
# ---------------------------------------------------------------------------

def test_lower_clamp_when_trough_below_minimum():
    ledger = make_ledger({REQUEST_DATE: -900})
    assert amount_safe_to_pay(ledger, 1000, 500, REQUEST_DATE, 1000) == 0


def test_upper_clamp_when_headroom_exceeds_requested_amount():
    ledger = make_ledger({})
    assert amount_safe_to_pay(ledger, 10_000, 100, REQUEST_DATE, 500) == 500


# ---------------------------------------------------------------------------
# 4. EXACT REQUEST AMOUNT -- boundary equality
# ---------------------------------------------------------------------------

def test_safe_amount_equals_requested_when_headroom_exactly_matches():
    ledger = make_ledger({})
    balance, minimum, requested = 1000, 300, 700  # trough(1000) - minimum(300) == requested exactly
    assert amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, requested) == 700
    assert amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, requested - 0.01) == \
        pytest.approx(699.99)


# ---------------------------------------------------------------------------
# 5. MINIMUM MONOTONICITY -- explicit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("minimum_low,minimum_high", [(100, 500), (0, 1), (-50, 50)])
def test_increasing_minimum_never_increases_safe_amount(minimum_low, minimum_high):
    ledger = make_ledger({REQUEST_DATE + timedelta(days=5): -200})
    low = amount_safe_to_pay(ledger, 1000, minimum_low, REQUEST_DATE, 1000)
    high = amount_safe_to_pay(ledger, 1000, minimum_high, REQUEST_DATE, 1000)
    assert high <= low


# ---------------------------------------------------------------------------
# 6. INCOME-DELAY MONOTONICITY -- explicit
# ---------------------------------------------------------------------------

def test_delaying_the_income_candidate_moves_earliest_date_later_not_earlier():
    day20 = REQUEST_DATE + timedelta(days=20)
    day25 = REQUEST_DATE + timedelta(days=25)
    day40 = REQUEST_DATE + timedelta(days=40)
    ledger = make_ledger({day20: 200, day40: 900})
    balance, minimum, amount = 1000, 100, 950

    earliest_on_time = earliest_date_for_full_payment(
        ledger, balance, minimum, REQUEST_DATE, amount, [day20, day40],
    )
    assert earliest_on_time == day20

    earliest_delayed = earliest_date_for_full_payment(
        ledger, balance, minimum, REQUEST_DATE, amount, [day25, day40],
    )
    assert earliest_delayed == day25
    assert earliest_delayed > earliest_on_time


# ---------------------------------------------------------------------------
# 7. FUTURE-EXPENSE MONOTONICITY -- explicit
# ---------------------------------------------------------------------------

def test_adding_future_expense_never_increases_safe_amount():
    base = make_ledger({})
    with_expense = make_ledger({REQUEST_DATE + timedelta(days=45): -300})
    balance, minimum, requested = 1000, 100, 1000
    base_safe = amount_safe_to_pay(base, balance, minimum, REQUEST_DATE, requested)
    reduced_safe = amount_safe_to_pay(with_expense, balance, minimum, REQUEST_DATE, requested)
    assert reduced_safe <= base_safe


# ---------------------------------------------------------------------------
# 8. EQUALITY IS SAFE
# ---------------------------------------------------------------------------

def test_equality_with_minimum_is_safe_not_a_breach():
    ledger = make_ledger({})
    balance, minimum = 1000, 400
    safe = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, 600)
    assert safe == 600
    assert trough(ledger, balance, REQUEST_DATE, {REQUEST_DATE: safe}) == minimum
    assert earliest_date_for_full_payment(ledger, balance, minimum, REQUEST_DATE, 600, []) == REQUEST_DATE


# ---------------------------------------------------------------------------
# 9. REQUEST-DATE FEASIBILITY
# ---------------------------------------------------------------------------

def test_earliest_date_is_request_date_when_full_payment_already_safe():
    ledger = make_ledger({})
    balance, minimum, amount = 5000, 100, 2000
    result = earliest_date_for_full_payment(
        ledger, balance, minimum, REQUEST_DATE, amount,
        [REQUEST_DATE + timedelta(days=20), REQUEST_DATE + timedelta(days=50)],
    )
    assert result == REQUEST_DATE


# ---------------------------------------------------------------------------
# 10. FUTURE INCOME DATE
# ---------------------------------------------------------------------------

def test_earliest_date_falls_on_first_feasible_income_date():
    day20 = REQUEST_DATE + timedelta(days=20)
    day40 = REQUEST_DATE + timedelta(days=40)
    ledger = make_ledger({day20: 200, day40: 900})
    balance, minimum, amount = 1000, 100, 950
    result = earliest_date_for_full_payment(
        ledger, balance, minimum, REQUEST_DATE, amount, [day20, day40],
    )
    assert result == day20


# ---------------------------------------------------------------------------
# 11. NO FEASIBLE DATE
# ---------------------------------------------------------------------------

def test_earliest_date_is_none_when_never_feasible():
    ledger = make_ledger({})
    balance, minimum, amount = 100, 50, 10_000
    income_dates = [REQUEST_DATE + timedelta(days=30), REQUEST_DATE + timedelta(days=60)]
    result = earliest_date_for_full_payment(ledger, balance, minimum, REQUEST_DATE, amount, income_dates)
    assert result is None


# ---------------------------------------------------------------------------
# 12. SPENDING CHANGES IGNORED
# ---------------------------------------------------------------------------

def test_capacity_functions_have_no_spending_change_parameter():
    forbidden = {"changes", "spending_changes", "overlays", "stop", "reduce_to"}
    safe_params = set(inspect.signature(amount_safe_to_pay).parameters)
    bisect_params = set(inspect.signature(amount_safe_to_pay_bisect).parameters)
    earliest_params = set(inspect.signature(earliest_date_for_full_payment).parameters)
    assert not (safe_params & forbidden)
    assert not (bisect_params & forbidden)
    assert not (earliest_params & forbidden)


def test_capacity_only_reflects_changes_already_baked_into_the_ledger():
    # A "changed" ledger (as build_ledger(..., changes=...) would produce) only
    # affects capacity through the ledger dict itself -- this module has no
    # side channel of its own that applies, ignores, or reverses a change.
    baseline = make_ledger({REQUEST_DATE + timedelta(days=10): -400})
    as_if_stopped = make_ledger({})
    balance, minimum, requested = 1000, 100, 1000
    assert amount_safe_to_pay(as_if_stopped, balance, minimum, REQUEST_DATE, requested) >= \
        amount_safe_to_pay(baseline, balance, minimum, REQUEST_DATE, requested)
    # Fully deterministic -- no hidden state carried between calls.
    assert amount_safe_to_pay(baseline, balance, minimum, REQUEST_DATE, requested) == \
        amount_safe_to_pay(baseline, balance, minimum, REQUEST_DATE, requested)


# ---------------------------------------------------------------------------
# 13. FULL-WINDOW CHECK
# ---------------------------------------------------------------------------

def test_later_expense_can_make_a_locally_safe_looking_date_unsafe():
    day20 = REQUEST_DATE + timedelta(days=20)
    day60 = REQUEST_DATE + timedelta(days=60)
    ledger = make_ledger({day60: -5000})
    balance, minimum, amount = 6000, 600, 500
    # Immediately after paying on day20 the balance looks fine (5500 >> 600);
    # it is the day-60 bill, well after day20, that later breaches minimum.
    assert trough(ledger, balance, REQUEST_DATE, {day20: amount}) < minimum
    result = earliest_date_for_full_payment(ledger, balance, minimum, REQUEST_DATE, amount, [day20])
    assert result is None


# ---------------------------------------------------------------------------
# 14. ORIGINAL-HORIZON SEMANTICS
# ---------------------------------------------------------------------------

def test_safety_window_is_anchored_to_request_date_not_the_candidate_date():
    day5 = REQUEST_DATE + timedelta(days=5)
    day20 = REQUEST_DATE + timedelta(days=20)
    # A dip strictly BEFORE the candidate day20 that a payment dated day20
    # cannot possibly fix (it hasn't happened yet).
    ledger = make_ledger({day5: -950, day20: 200})
    balance, minimum, amount = 1000, 100, 100

    # A [d, d+90] re-anchoring would look only from day20 onward and miss the
    # day-5 dip entirely -- demonstrated directly against trough() here.
    assert trough(ledger, balance, day20, {day20: amount}) >= minimum
    # The correct [request_date, request_date+90] anchoring sees the day-5
    # breach (1000 - 950 = 50 < 100) and must reject day20 too.
    assert trough(ledger, balance, REQUEST_DATE, {day20: amount}) < minimum

    result = earliest_date_for_full_payment(ledger, balance, minimum, REQUEST_DATE, amount, [day20])
    assert result is None


# ---------------------------------------------------------------------------
# 15. SEARCH MODE EQUIVALENCE
# ---------------------------------------------------------------------------

def test_search_all_days_flag_defaults_to_false():
    assert capacity.SEARCH_ALL_DAYS is False


def test_search_all_days_agrees_with_income_dates_when_sufficient():
    day15, day20, day40 = (REQUEST_DATE + timedelta(days=n) for n in (15, 20, 40))
    ledger = make_ledger({day15: -300, day20: 200, day40: 900})
    balance, minimum, amount = 1000, 100, 950
    income_dates = [day20, day40]

    by_income_dates = earliest_date_for_full_payment(
        ledger, balance, minimum, REQUEST_DATE, amount, income_dates, search_all_days=False,
    )
    by_full_search = earliest_date_for_full_payment(
        ledger, balance, minimum, REQUEST_DATE, amount, income_dates, search_all_days=True,
    )
    assert by_income_dates == by_full_search is not None


def test_search_all_days_agrees_across_several_income_only_ledgers():
    rng = random.Random(777)
    for _ in range(20):
        day_a = rng.randint(5, 40)
        day_b = rng.randint(day_a + 5, HORIZON)
        income = [REQUEST_DATE + timedelta(days=day_a), REQUEST_DATE + timedelta(days=day_b)]
        movements = {income[0]: rng.uniform(200, 2000), income[1]: rng.uniform(200, 4000)}
        ledger = make_ledger(movements)
        balance = rng.uniform(200, 3000)
        minimum = rng.uniform(0, 800)
        amount = rng.uniform(200, 5000)
        by_income = earliest_date_for_full_payment(
            ledger, balance, minimum, REQUEST_DATE, amount, income, search_all_days=False,
        )
        by_full = earliest_date_for_full_payment(
            ledger, balance, minimum, REQUEST_DATE, amount, income, search_all_days=True,
        )
        assert by_income == by_full


# ---------------------------------------------------------------------------
# NO MUTATION
# ---------------------------------------------------------------------------

def test_capacity_functions_do_not_mutate_their_inputs():
    ledger = make_ledger({REQUEST_DATE + timedelta(days=10): -500, REQUEST_DATE + timedelta(days=40): 900})
    snapshot = dict(ledger)
    income_dates = [REQUEST_DATE + timedelta(days=40)]
    income_snapshot = list(income_dates)

    amount_safe_to_pay(ledger, 1000, 100, REQUEST_DATE, 500)
    amount_safe_to_pay_bisect(ledger, 1000, 100, REQUEST_DATE, 500)
    earliest_date_for_full_payment(ledger, 1000, 100, REQUEST_DATE, 500, income_dates)

    assert ledger == snapshot
    assert income_dates == income_snapshot


# ---------------------------------------------------------------------------
# Random synthetic case generator, shared by the differential test (section 7)
# and the property tests (section 8). Fixed seeds throughout for determinism.
# ---------------------------------------------------------------------------

SCENARIO_KINDS = (
    "mixed",
    "positive_income",
    "expense_heavy",
    "same_day_net",
    "trough_on_request_date",
    "trough_later_in_horizon",
    "upper_clamp_forced",
    "lower_clamp_forced",
)


def _random_case(rng: random.Random, kind: str):
    movements: dict[date, float] = {}

    def bump(offset: int, amount: float) -> None:
        on_date = REQUEST_DATE + timedelta(days=offset)
        movements[on_date] = round(movements.get(on_date, 0.0) + amount, 2)

    if kind == "mixed":
        for _ in range(rng.randint(4, 10)):
            bump(rng.randint(0, HORIZON), rng.choice([1, -1]) * rng.uniform(10, 1500))
        balance, minimum, requested = rng.uniform(500, 8000), rng.uniform(0, 2000), rng.uniform(0, 5000)
    elif kind == "positive_income":
        bump(rng.randint(10, 80), rng.uniform(1000, 6000))
        bump(rng.randint(0, 90), -rng.uniform(100, 1000))
        balance, minimum, requested = rng.uniform(100, 3000), rng.uniform(0, 1500), rng.uniform(0, 6000)
    elif kind == "expense_heavy":
        for _ in range(rng.randint(2, 5)):
            bump(rng.randint(0, HORIZON), -rng.uniform(200, 2000))
        balance, minimum, requested = rng.uniform(2000, 9000), rng.uniform(0, 2000), rng.uniform(0, 4000)
    elif kind == "same_day_net":
        bump(0, rng.uniform(500, 2000))
        bump(0, -rng.uniform(200, 1800))
        balance, minimum, requested = rng.uniform(500, 4000), rng.uniform(0, 1500), rng.uniform(0, 3000)
    elif kind == "trough_on_request_date":
        bump(0, -rng.uniform(500, 3000))
        for _ in range(rng.randint(1, 4)):
            bump(rng.randint(1, HORIZON), rng.uniform(50, 500))
        balance, minimum, requested = rng.uniform(2000, 9000), rng.uniform(0, 1500), rng.uniform(0, 4000)
    elif kind == "trough_later_in_horizon":
        bump(rng.randint(30, HORIZON), -rng.uniform(1000, 4000))
        balance, minimum, requested = rng.uniform(2000, 9000), rng.uniform(0, 1500), rng.uniform(0, 4000)
    elif kind == "upper_clamp_forced":
        balance, minimum, requested = rng.uniform(8000, 20000), rng.uniform(0, 200), rng.uniform(10, 300)
    elif kind == "lower_clamp_forced":
        bump(0, -rng.uniform(5000, 9000))
        balance, minimum, requested = rng.uniform(100, 2000), rng.uniform(500, 3000), rng.uniform(100, 5000)
    else:
        raise ValueError(kind)

    return make_ledger(movements), round(balance, 2), round(minimum, 2), round(requested, 2)


# ---------------------------------------------------------------------------
# Section 7: 25+ deterministic random differential cases, closed form vs bisect
# ---------------------------------------------------------------------------

def test_random_differential_closed_form_matches_bisection():
    rng = random.Random(20250913)
    trials = 32
    hit_upper_clamp = False
    hit_lower_clamp = False
    for i in range(trials):
        kind = SCENARIO_KINDS[i % len(SCENARIO_KINDS)]
        ledger, balance, minimum, requested = _random_case(rng, kind)
        closed = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, requested)
        bisected = amount_safe_to_pay_bisect(ledger, balance, minimum, REQUEST_DATE, requested)
        assert math.isclose(closed, bisected, abs_tol=1e-4), f"case {i} ({kind}): {closed} vs {bisected}"
        assert 0 <= closed <= requested
        hit_upper_clamp = hit_upper_clamp or closed == requested
        hit_lower_clamp = hit_lower_clamp or closed == 0
    assert hit_upper_clamp, "generator never exercised the upper clamp"
    assert hit_lower_clamp, "generator never exercised the lower clamp"


# ---------------------------------------------------------------------------
# Section 8: label-free property tests
# ---------------------------------------------------------------------------

def test_property_increasing_minimum_never_increases_safe_amount():
    rng = random.Random(4242)
    for i in range(40):
        kind = SCENARIO_KINDS[i % len(SCENARIO_KINDS)]
        ledger, balance, minimum, requested = _random_case(rng, kind)
        higher_minimum = minimum + rng.uniform(0, 2000)
        safe_low = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, requested)
        safe_high = amount_safe_to_pay(ledger, balance, higher_minimum, REQUEST_DATE, requested)
        assert safe_high <= safe_low + 1e-9


def test_property_future_expense_never_increases_safe_amount():
    rng = random.Random(4343)
    for i in range(40):
        kind = SCENARIO_KINDS[i % len(SCENARIO_KINDS)]
        ledger, balance, minimum, requested = _random_case(rng, kind)
        snapshot = dict(ledger)
        worsened = dict(ledger)
        offset = rng.randint(0, HORIZON)
        extra_expense_date = REQUEST_DATE + timedelta(days=offset)
        worsened[extra_expense_date] -= rng.uniform(1, 3000)

        safe_before = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, requested)
        safe_after = amount_safe_to_pay(worsened, balance, minimum, REQUEST_DATE, requested)
        assert safe_after <= safe_before + 1e-9
        assert ledger == snapshot


def test_property_increasing_requested_amount_never_decreases_safe_amount():
    rng = random.Random(4444)
    for i in range(40):
        kind = SCENARIO_KINDS[i % len(SCENARIO_KINDS)]
        ledger, balance, minimum, requested = _random_case(rng, kind)
        bigger_requested = requested + rng.uniform(0, 3000)
        safe_small = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, requested)
        safe_big = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, bigger_requested)
        assert safe_big >= safe_small - 1e-9


def test_property_improving_cash_flow_never_decreases_safe_amount():
    rng = random.Random(4545)
    for i in range(40):
        kind = SCENARIO_KINDS[i % len(SCENARIO_KINDS)]
        ledger, balance, minimum, requested = _random_case(rng, kind)
        improved = {on_date: value + rng.uniform(0, 500) for on_date, value in ledger.items()}
        safe_before = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, requested)
        safe_after = amount_safe_to_pay(improved, balance, minimum, REQUEST_DATE, requested)
        assert safe_after >= safe_before - 1e-9


def test_property_delaying_income_candidate_never_moves_earliest_date_earlier():
    rng = random.Random(4646)
    trials = 0
    for _ in range(60):
        day_a = rng.randint(5, 40)
        day_b = rng.randint(day_a + 5, HORIZON)
        income_dates = [REQUEST_DATE + timedelta(days=day_a), REQUEST_DATE + timedelta(days=day_b)]
        movements = {income_dates[0]: rng.uniform(200, 1500), income_dates[1]: rng.uniform(200, 3000)}
        if rng.random() < 0.5:
            noisy = REQUEST_DATE + timedelta(days=rng.randint(0, HORIZON))
            movements[noisy] = movements.get(noisy, 0.0) - rng.uniform(50, 500)
        ledger = make_ledger(movements)
        balance = rng.uniform(500, 5000)
        minimum = rng.uniform(0, 1000)
        amount = rng.uniform(200, 4000)

        earliest_before = earliest_date_for_full_payment(
            ledger, balance, minimum, REQUEST_DATE, amount, income_dates,
        )

        pick = rng.randrange(len(income_dates))
        delayed = list(income_dates)
        delayed[pick] = delayed[pick] + timedelta(days=rng.randint(1, 10))
        if delayed[pick] not in ledger:
            continue  # delayed past the ledger's own horizon; not comparable

        earliest_after = earliest_date_for_full_payment(
            ledger, balance, minimum, REQUEST_DATE, amount, delayed,
        )
        trials += 1
        if earliest_before is not None and earliest_after is not None:
            assert earliest_after >= earliest_before
    assert trials > 30, "too many trials skipped; delay pushed past the ledger horizon"


def test_property_equality_at_minimum_is_always_safe():
    rng = random.Random(4747)
    checked = 0
    for i in range(40):
        kind = SCENARIO_KINDS[i % len(SCENARIO_KINDS)]
        ledger, balance, minimum, _ = _random_case(rng, kind)
        raw_trough = trough(ledger, balance, REQUEST_DATE)
        if raw_trough < minimum:
            continue  # nothing payable; the equality case does not apply
        headroom = raw_trough - minimum
        safe = amount_safe_to_pay(ledger, balance, minimum, REQUEST_DATE, headroom)
        assert math.isclose(safe, headroom, abs_tol=1e-6)
        assert trough(ledger, balance, REQUEST_DATE, {REQUEST_DATE: safe}) >= minimum - 1e-9
        checked += 1
    assert checked > 0

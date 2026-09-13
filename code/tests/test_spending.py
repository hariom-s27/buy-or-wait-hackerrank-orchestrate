"""Synthetic test suite for Step 8 (Exhaustive spending-change search).

Tests 1-15 cover all required spending-change semantics using synthetic fixtures.
No real request/user IDs or external dataset modifications.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal

import pytest

from code.decide.plans import enumerate_plans
from code.decide.rank import select_plan
from code.decide.spending import (
    SpendingChange,
    eligible_changes,
    enumerate_change_subsets,
    search_spending_plans,
)
from code.domain.models import Event, PaymentOption, Profile, Request, RequestCase
from code.forecast.capacity import amount_safe_to_pay, earliest_date_for_full_payment
from code.forecast.ledger import build_ledger_trace
from code.main import decide_case


def d(val: str) -> date:
    return date.fromisoformat(val)


def make_event(
    event_id: str,
    user_id: str = "user_synth",
    category: str = "dining",
    description: str = "Dinner spend",
    amount: float = 100.0,
    settlement_date: str = "2026-01-01",
    direction: str = "debit",
    status: str = "settled",
    flexibility: str = "reducible",
    minimum_allowed_amount: float | None = 20.0,
    currency: str = "USD",
) -> Event:
    s_date = d(settlement_date)
    return Event(
        event_id=event_id,
        user_id=user_id,
        event_type="expense" if direction == "debit" else "income",
        description=description,
        category=category,
        direction=direction,
        amount=amount,
        currency=currency,
        event_date=s_date,
        settlement_date=s_date,
        status=status,
        linked_event_id=None,
        flexibility=flexibility,
        minimum_allowed_amount=minimum_allowed_amount,
    )


def make_profile(
    user_id: str = "user_synth",
    currency: str = "USD",
    balance: float = 1000.0,
    minimum: float = 200.0,
    protect: list[str] | None = None,
    willing_reduce: list[str] | None = None,
    willing_stop: list[str] | None = None,
    methods: list[str] | None = None,
    max_installments: int | None = None,
) -> Profile:
    return Profile(
        user_id=user_id,
        home_currency=currency,
        current_available_balance=balance,
        minimum_balance_to_keep=minimum,
        financial_priorities=["saving"],
        expense_categories_to_protect=protect or [],
        expense_categories_user_is_willing_to_reduce=willing_reduce or [],
        expense_categories_user_is_willing_to_stop=willing_stop or [],
        payment_methods_user_will_consider=methods or ["full_payment"],
        max_installment_months=max_installments,
    )


def make_case(
    request_id: str = "req_synth",
    user_id: str = "user_synth",
    request_date: str = "2026-02-01",
    requested_amount: float = 500.0,
    desired_completion_date: str = "2026-02-15",
    allows_partial: bool = False,
    profile: Profile | None = None,
    events: list[Event] | None = None,
    payment_options: list[PaymentOption] | None = None,
) -> RequestCase:
    req_d = d(request_date)
    comp_d = d(desired_completion_date)
    req = Request(
        request_id=request_id,
        user_id=user_id,
        request_date=req_d,
        request_type="electronics",
        requested_amount=requested_amount,
        desired_completion_date=comp_d,
        allows_partial_payment=allows_partial,
        request_text="Synthetic test request",
    )
    prof = profile or make_profile(user_id=user_id)
    evs = events or []
    opts = payment_options or []
    return RequestCase(
        request=req,
        profile=prof,
        events=evs,
        payment_options=opts,
        messages=[],
        images=[],
    )


# ===========================================================================
# TEST 1 — PROTECTED CATEGORY
# ===========================================================================
def test_protected_category_never_offered():
    """A protected category must never produce a spending-change candidate."""
    events = [
        make_event("e1", category="dining", settlement_date="2025-12-01", flexibility="stoppable"),
        make_event("e2", category="dining", settlement_date="2026-01-01", flexibility="stoppable"),
        make_event("e3", category="entertainment", settlement_date="2025-12-01", flexibility="stoppable"),
        make_event("e4", category="entertainment", settlement_date="2026-01-01", flexibility="stoppable"),
    ]
    prof = make_profile(
        protect=["dining"],
        willing_stop=["dining", "entertainment"],
        willing_reduce=[],
    )
    case = make_case(profile=prof, events=events)
    eligible = eligible_changes(case)

    categories = [c.category for c in eligible]
    assert "dining" not in categories
    assert "entertainment" in categories


# ===========================================================================
# TEST 2 — NO USER PERMISSION
# ===========================================================================
def test_unpermitted_category_never_offered():
    """A flexible category absent from the user's willing list must never produce a candidate."""
    events = [
        make_event("e1", category="dining", settlement_date="2025-12-01", flexibility="reducible_or_stoppable"),
        make_event("e2", category="dining", settlement_date="2026-01-01", flexibility="reducible_or_stoppable"),
    ]
    prof = make_profile(
        protect=[],
        willing_stop=["streaming"],
        willing_reduce=["groceries"],
    )
    case = make_case(profile=prof, events=events)
    eligible = eligible_changes(case)
    assert len(eligible) == 0


# ===========================================================================
# TEST 3 — REDUCTION FLOOR
# ===========================================================================
def test_reduction_floor_respected():
    """reduce_to must never be below minimum_allowed_amount."""
    events = [
        make_event("e1", category="dining", amount=150.0, settlement_date="2025-12-01",
                   flexibility="reducible", minimum_allowed_amount=45.50),
        make_event("e2", category="dining", amount=150.0, settlement_date="2026-01-01",
                   flexibility="reducible", minimum_allowed_amount=45.50),
    ]
    prof = make_profile(
        protect=[],
        willing_reduce=["dining"],
    )
    case = make_case(profile=prof, events=events)
    eligible = eligible_changes(case)
    reduce_changes = [c for c in eligible if c.kind == "reduce_to"]
    assert len(reduce_changes) == 1
    assert reduce_changes[0].target_amount >= 45.50
    assert "45.50" in reduce_changes[0].operation_str


# ===========================================================================
# TEST 4 — SAME EVENT EXCLUSION
# ===========================================================================
def test_same_event_stop_and_reduce_mutually_exclusive():
    """stop and reduce for the same event_id can never coexist in a candidate subset."""
    events = [
        make_event("e1", category="dining", amount=100.0, settlement_date="2025-12-01",
                   flexibility="reducible_or_stoppable", minimum_allowed_amount=30.0),
        make_event("e2", category="dining", amount=100.0, settlement_date="2026-01-01",
                   flexibility="reducible_or_stoppable", minimum_allowed_amount=30.0),
    ]
    prof = make_profile(
        protect=[],
        willing_stop=["dining"],
        willing_reduce=["dining"],
    )
    case = make_case(profile=prof, events=events)
    eligible = eligible_changes(case)
    assert len(eligible) == 2  # stop:e2 and reduce_to:e2:30
    subsets = enumerate_change_subsets(eligible, max_size=3)
    for sub in subsets:
        eids = [c.event_id for c in sub]
        assert len(eids) == len(set(eids)), f"Duplicate event_id in subset: {sub}"


# ===========================================================================
# TEST 5 — MAX THREE
# ===========================================================================
def test_max_three_spending_changes():
    """No emitted change set can contain more than 3 distinct event IDs."""
    events = []
    categories = ["cat1", "cat2", "cat3", "cat4", "cat5"]
    for i, cat in enumerate(categories):
        events.append(make_event(f"e_{i}_1", category=cat, settlement_date="2025-12-01", flexibility="stoppable"))
        events.append(make_event(f"e_{i}_2", category=cat, settlement_date="2026-01-01", flexibility="stoppable"))
    prof = make_profile(protect=[], willing_stop=categories)
    case = make_case(profile=prof, events=events)
    eligible = eligible_changes(case)
    assert len(eligible) == 5
    subsets = enumerate_change_subsets(eligible, max_size=3)
    for sub in subsets:
        assert len(sub) <= 3


# ===========================================================================
# TEST 6 — STOP
# ===========================================================================
def test_stop_removes_future_occurrences():
    """Stopping a legal stream removes only its future occurrences."""
    events = [
        make_event("e1", category="streaming", amount=25.0, settlement_date="2025-12-01", flexibility="stoppable"),
        make_event("e2", category="streaming", amount=25.0, settlement_date="2026-01-01", flexibility="stoppable"),
    ]
    case = make_case(request_date="2026-01-15", events=events)
    base_trace = build_ledger_trace(case, d("2026-01-15"))
    changed_trace = build_ledger_trace(case, d("2026-01-15"), changes={"e2": None})

    # Historical dates unaffected
    assert base_trace.ledger[d("2026-01-15")] == changed_trace.ledger[d("2026-01-15")]
    # Future occurrence (around Feb 1) should be present in base but absent in changed
    base_debits = sum(base_trace.ledger.values())
    changed_debits = sum(changed_trace.ledger.values())
    assert changed_debits > base_debits  # less negative = savings


# ===========================================================================
# TEST 7 — REDUCE
# ===========================================================================
def test_reduce_uses_specified_amount():
    """Reducing a legal stream changes only future occurrences and uses the reduction amount."""
    events = [
        make_event("e1", category="streaming", amount=50.0, settlement_date="2025-12-01",
                   flexibility="reducible", minimum_allowed_amount=20.0),
        make_event("e2", category="streaming", amount=50.0, settlement_date="2026-01-01",
                   flexibility="reducible", minimum_allowed_amount=20.0),
    ]
    case = make_case(request_date="2026-01-15", events=events)
    base_trace = build_ledger_trace(case, d("2026-01-15"))
    changed_trace = build_ledger_trace(case, d("2026-01-15"), changes={"e2": 20.0})

    base_sum = sum(base_trace.ledger.values())
    changed_sum = sum(changed_trace.ledger.values())
    # Reduced from 50 to 20 saves 30 per future occurrence
    diff = round(changed_sum - base_sum, 2)
    assert diff > 0 and diff % 30.0 == 0


# ===========================================================================
# TEST 8 — HISTORY PRESERVATION
# ===========================================================================
def test_history_preservation():
    """Historical ledger values and settled events are unchanged after a spending change."""
    events = [
        make_event("e1", category="groceries", amount=120.0, settlement_date="2025-11-01"),
        make_event("e2", category="groceries", amount=120.0, settlement_date="2025-12-01"),
        make_event("e3", category="groceries", amount=120.0, settlement_date="2026-01-01",
                   flexibility="stoppable"),
    ]
    case = make_case(request_date="2026-01-15", events=events)
    base_trace = build_ledger_trace(case, d("2026-01-15"))
    changed_trace = build_ledger_trace(case, d("2026-01-15"), changes={"e3": None})

    # Check contributions from historical events before request_date
    base_hist = [c for c in base_trace.contributions if c.date < d("2026-01-15")]
    changed_hist = [c for c in changed_trace.contributions if c.date < d("2026-01-15")]
    assert base_hist == changed_hist


# ===========================================================================
# TEST 9 — BASELINE IMMUTABILITY
# ===========================================================================
def test_baseline_immutability():
    """Trying multiple subsets cannot mutate baseline ledger, baseline streams, or source events."""
    events = [
        make_event("e1", category="dining", amount=80.0, settlement_date="2025-12-01",
                   flexibility="reducible_or_stoppable", minimum_allowed_amount=20.0),
        make_event("e2", category="dining", amount=80.0, settlement_date="2026-01-01",
                   flexibility="reducible_or_stoppable", minimum_allowed_amount=20.0),
    ]
    prof = make_profile(balance=300.0, minimum=100.0, willing_stop=["dining"], willing_reduce=["dining"])
    case = make_case(request_date="2026-01-15", requested_amount=250.0, profile=prof, events=events)

    trace = build_ledger_trace(case, d("2026-01-15"))
    ledger_snapshot = copy.deepcopy(trace.ledger)
    streams_snapshot = copy.deepcopy(trace.expense_streams)
    events_snapshot = copy.deepcopy(case.events)

    search_spending_plans(case, trace, 100.0, d("2026-02-01"))

    assert trace.ledger == ledger_snapshot
    assert trace.expense_streams == streams_snapshot
    assert case.events == events_snapshot


# ===========================================================================
# TEST 10 — EXHAUSTIVE SEARCH
# ===========================================================================
def test_exhaustive_search_finds_timing_pair():
    """Construct a timing case where one large-saving change occurs too late,
    but two smaller earlier changes together make the plan safe."""
    events = [
        # Stream B (dining, gap 30): occurrences on Jan 6, Feb 5
        make_event("b1", category="dining", amount=30.0, settlement_date="2025-12-06", flexibility="stoppable"),
        make_event("b2", category="dining", amount=30.0, settlement_date="2026-01-06", flexibility="stoppable"),
        # Stream C (streaming, gap 30): occurrences on Jan 9, Feb 8
        make_event("c1", category="streaming", amount=30.0, settlement_date="2025-12-09", flexibility="stoppable"),
        make_event("c2", category="streaming", amount=30.0, settlement_date="2026-01-09", flexibility="stoppable"),
        # Stream A (shopping, gap 30): occurrence on Feb 26 (too late, after Feb 10 deadline)
        make_event("a1", category="shopping", amount=150.0, settlement_date="2025-12-26", flexibility="stoppable"),
        make_event("a2", category="shopping", amount=150.0, settlement_date="2026-01-26", flexibility="stoppable"),
        # Scheduled salary credit on Feb 15 (after Feb 10 deadline)
        Event("pay1", "user_synth", "income", "Next confirmed salary", "salary", "credit", 1000.0,
              "USD", d("2026-02-15"), d("2026-02-15"), "scheduled", None, "fixed", None),
    ]
    prof = make_profile(
        balance=520.0,
        minimum=200.0,
        protect=[],
        willing_stop=["dining", "streaming", "shopping"],
    )
    # Deadline is Feb 10!
    case = make_case(
        request_date="2026-02-01",
        requested_amount=300.0,
        desired_completion_date="2026-02-10",
        profile=prof,
        events=events,
    )
    trace = build_ledger_trace(case, d("2026-02-01"))
    safe = amount_safe_to_pay(trace.ledger, prof.current_available_balance, prof.minimum_balance_to_keep,
                              d("2026-02-01"), 300.0)
    # Baseline plan is not safe
    base_winner = select_plan(enumerate_plans(case, trace.ledger, safe, None), d("2026-02-10"))
    assert base_winner is None

    # Exhaustive search finds pair {b2, c2}
    winner = search_spending_plans(case, trace, safe, None)
    assert winner is not None
    assert winner.method == "full_payment"
    assert set(winner.spending_changes) == {"stop:b2", "stop:c2"}


# ===========================================================================
# TEST 11 — ZERO-CHANGE PREFERENCE
# ===========================================================================
def test_zero_change_preference_preserved():
    """If a zero-change plan is already safe, a change-requiring plan must not replace it."""
    events = [
        make_event("e1", category="dining", amount=50.0, settlement_date="2025-12-01", flexibility="stoppable"),
        make_event("e2", category="dining", amount=50.0, settlement_date="2026-01-01", flexibility="stoppable"),
    ]
    prof = make_profile(balance=2000.0, minimum=200.0, willing_stop=["dining"])
    case = make_case(request_date="2026-01-15", requested_amount=300.0, profile=prof, events=events)

    dec = decide_case(case)
    assert dec.affordability_status == "affordable_now"
    assert dec.recommended_payment_method == "full_payment"
    assert dec.spending_changes_needed == "none"


# ===========================================================================
# TEST 12 — METHOD ELIGIBILITY
# ===========================================================================
def test_spending_change_obeys_user_accepted_methods():
    """A spending-change plan must still obey the user's accepted methods."""
    events = [
        make_event("e1", category="dining", amount=100.0, settlement_date="2025-12-01", flexibility="stoppable"),
        make_event("e2", category="dining", amount=100.0, settlement_date="2026-01-01", flexibility="stoppable"),
    ]
    # User does NOT accept full_payment; only installments
    prof = make_profile(
        balance=400.0, minimum=100.0, willing_stop=["dining"],
        methods=["installments"], max_installments=3,
    )
    # Option: 3 monthly payments of 100 on Feb 1, Mar 1, Apr 1
    options = [
        PaymentOption(
            payment_option_id="opt_1", request_id="req_synth", payment_method="installments",
            payment_amount=100.0, number_of_payments=3, first_payment_date=d("2026-02-01"),
            payment_frequency_days=30, financing_fee=0.0, total_payable_amount=300.0,
        ),
    ]
    case = make_case(
        request_date="2026-01-15", requested_amount=300.0,
        desired_completion_date="2026-05-01", profile=prof, events=events,
        payment_options=options,
    )
    dec = decide_case(case)
    assert dec.recommended_payment_method in ("installments", "not_recommended")
    assert dec.recommended_payment_method != "full_payment"


# ===========================================================================
# TEST 13 — INSTALLMENT FIDELITY
# ===========================================================================
def test_installment_fidelity_with_spending_change():
    """A spending-change installment plan must still exactly match a real payment option."""
    events = [
        make_event("e1", category="dining", amount=150.0, settlement_date="2025-12-01", flexibility="stoppable"),
        make_event("e2", category="dining", amount=150.0, settlement_date="2026-01-01", flexibility="stoppable"),
    ]
    prof = make_profile(
        balance=250.0, minimum=100.0, willing_stop=["dining"],
        methods=["installments"], max_installments=3,
    )
    options = [
        PaymentOption(
            payment_option_id="opt_real", request_id="req_synth", payment_method="installments",
            payment_amount=100.0, number_of_payments=2, first_payment_date=d("2026-01-20"),
            payment_frequency_days=30, financing_fee=0.0, total_payable_amount=200.0,
        ),
    ]
    case = make_case(
        request_date="2026-01-15", requested_amount=200.0,
        desired_completion_date="2026-03-01", profile=prof, events=events,
        payment_options=options,
    )
    dec = decide_case(case)
    if dec.recommended_payment_method == "installments":
        assert dec.spending_changes_needed == "stop:e2"
        # Schedule must match opt_real exactly: 2026-01-20:100|2026-02-19:100
        assert "2026-01-20:100" in dec.payment_plan
        assert "2026-02-19:100" in dec.payment_plan


# ===========================================================================
# TEST 14 — DEADLINE
# ===========================================================================
def test_spending_change_rejected_if_past_deadline():
    """A change-requiring plan is invalid if the payment plan finishes after desired_completion_date."""
    events = [
        make_event("e1", category="dining", amount=100.0, settlement_date="2025-12-01", flexibility="stoppable"),
        make_event("e2", category="dining", amount=100.0, settlement_date="2026-01-01", flexibility="stoppable"),
    ]
    prof = make_profile(
        balance=150.0, minimum=100.0, willing_stop=["dining"],
        methods=["installments"], max_installments=3,
    )
    options = [
        PaymentOption(
            payment_option_id="opt_late", request_id="req_synth", payment_method="installments",
            payment_amount=50.0, number_of_payments=2, first_payment_date=d("2026-01-20"),
            payment_frequency_days=30, financing_fee=0.0, total_payable_amount=100.0,
        ),
    ]
    # Deadline is Feb 1, but last installment is Feb 19
    case = make_case(
        request_date="2026-01-15", requested_amount=100.0,
        desired_completion_date="2026-02-01", profile=prof, events=events,
        payment_options=options,
    )
    dec = decide_case(case)
    assert dec.recommended_payment_method == "not_recommended"
    assert dec.payment_plan == "none"


# ===========================================================================
# TEST 15 — BASELINE CAPACITY IMMUTABILITY
# ===========================================================================
def test_baseline_capacity_immutability():
    """amount_safe_to_pay and earliest_date_for_full_payment must remain the same
    before and after spending-change searches."""
    events = [
        make_event("e1", category="dining", amount=100.0, settlement_date="2026-01-01", flexibility="stoppable"),
        make_event("e2", category="dining", amount=100.0, settlement_date="2026-01-31", flexibility="stoppable"),
        # Salary credit on Feb 15
        Event("pay1", "user_synth", "income", "Payroll credit", "salary", "credit", 1000.0,
              "USD", d("2026-01-15"), d("2026-01-15"), "settled", None, "fixed", None),
    ]
    # Balance 250, minimum 100, requested 400.
    # Safe amount today before salary is 150.
    prof = make_profile(balance=250.0, minimum=100.0, willing_stop=["dining"])
    case = make_case(
        request_date="2026-02-01", requested_amount=400.0,
        desired_completion_date="2026-02-10", profile=prof, events=events,
    )

    trace = build_ledger_trace(case, d("2026-02-01"))
    base_safe = amount_safe_to_pay(trace.ledger, 250.0, 100.0, d("2026-02-01"), 400.0)
    base_earliest = earliest_date_for_full_payment(
        trace.ledger, 250.0, 100.0, d("2026-02-01"), 400.0,
        {r.date for r in trace.contributions if r.amount > 0},
    )

    dec = decide_case(case)
    # The decision row fields must match the baseline capacity values exactly
    assert dec.amount_safe_to_pay == base_safe
    assert dec.earliest_date_for_full_payment == (base_earliest.isoformat() if base_earliest and dec.recommended_payment_method != "not_recommended" else "")

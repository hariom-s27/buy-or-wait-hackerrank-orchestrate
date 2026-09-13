"""
Step 13 — Label-free property, regression, permutation and determinism tests.

Covers BUILD-PLAN.md Step 13 sections 6-21 (properties 1-68 plus the output
contract sweep and deterministic-replay verification). All fixtures are
deterministic synthetic constructions; this module never depends on the
labelled ground truth in dataset/sample_requests.csv to DEFINE a property.
Two tests exercise the real dataset (permutation invariance over one real
request's events, and the output-contract sweep over the real 250-row
output.csv) because those are explicitly about the real pipeline's
determinism/contract, not about matching a ground-truth label.

No test in this module makes a live model/API call: an autouse fixture
strips API keys, and the deterministic-replay test additionally redirects
the on-disk evidence cache to an isolated temporary directory so it never
reads or writes the repository's real `.cache/evidence`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from code.domain import fx
from code.domain.models import (
    Event,
    ExchangeRate,
    PaymentOption,
    Profile,
    Request,
    RequestCase,
)
from code.evidence import cache as cache_mod
from code.evidence.apply import apply_deltas
from code.evidence.images import (
    VERIFIED_IMAGE_DATA,
    apply_image_evidence,
    extract_all_images,
    get_image_event_mappings,
    validate_image_evidence,
)
from code.evidence.messages import extract_all_deltas
from code.evidence.schema import EvidenceDelta, EvidenceIntent
from code.decide.plans import enumerate_plans
from code.decide.rank import select_plan
from code.decide.spending import eligible_changes, enumerate_change_subsets, search_spending_plans
from code.forecast.capacity import amount_safe_to_pay, earliest_date_for_full_payment
from code.forecast.ledger import UnresolvedCashEvents, build_ledger, build_ledger_trace, occurrences, trough
from code.io.indexes import build_request_case, load_and_index
from code.io.loaders import _row_to_request, load_all
from code.main import decide_case, decision_row
from code.output.validator import ValidationError, validate_rows
from code.output.writer import write_output
from code.reconstruct.salary import project_salary_streams

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def d(value: str) -> date:
    return date.fromisoformat(value)


# ---------------------------------------------------------------------------
# No live model/API calls from this module, regardless of host environment.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_live_model_calls(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    yield


# ---------------------------------------------------------------------------
# Shared local fixture builders (one coherent set for the whole file; every
# other test module in code/tests defines its own local copies too).
# ---------------------------------------------------------------------------

def event(event_id, description, on_date, amount=100.0, **changes) -> Event:
    on_date = d(on_date) if isinstance(on_date, str) else on_date
    return replace(Event(
        event_id=event_id, user_id="prop-user", event_type="expense",
        description=description, category="shopping", direction="debit", amount=amount,
        currency="USD", event_date=on_date, settlement_date=on_date, status="settled",
        linked_event_id=None, flexibility="reducible", minimum_allowed_amount=10.0,
    ), **changes)


def payroll(event_id="pay", description="Payroll credit", on_date="2025-01-15", amount=1000.0, **changes) -> Event:
    return event(event_id, description, on_date, amount,
                 category="salary", direction="credit", event_type="income", **changes)


def case(events=(), request="2025-02-01", currency="USD", balance=2000.0, minimum=0.0,
         requested=100.0, deadline_days=90, methods=("full_payment",), partial=False,
         options=(), messages=(), user_id="prop-user", request_id="prop-request",
         max_installments=None) -> RequestCase:
    request_date = d(request)
    return RequestCase(
        request=Request(request_id, user_id, request_date, "purchase", requested,
                        request_date + timedelta(days=deadline_days), partial, "Property fixture"),
        profile=Profile(user_id, currency, balance, minimum, [], [], [], [], list(methods), max_installments),
        events=list(events), payment_options=list(options), messages=list(messages), images=[],
    )


def ledger_of(rows, request="2025-02-01", horizon=90, changes=None, currency="USD") -> dict:
    return build_ledger(case(rows, request, currency), d(request), horizon, changes)


REQ_DATE = d("2025-03-01")
HORIZON = 90


def mk_ledger(movements: dict, start: date = REQ_DATE, horizon: int = HORIZON) -> dict:
    return {start + timedelta(days=i): movements.get(start + timedelta(days=i), 0.0)
            for i in range(horizon + 1)}


# ===========================================================================
# SECTION 6 — CAPACITY MONOTONICITY (PROPERTIES 1-5)
# ===========================================================================

def test_property_01_increasing_minimum_balance_never_increases_safe_amount():
    ledger = mk_ledger({REQ_DATE + timedelta(days=10): -300, REQ_DATE + timedelta(days=40): 900})
    low = amount_safe_to_pay(ledger, 2000, 100, REQ_DATE, 2000)
    high = amount_safe_to_pay(ledger, 2000, 900, REQ_DATE, 2000)
    assert high <= low


def test_property_02_future_expense_never_increases_safe_amount():
    base = mk_ledger({})
    worse = mk_ledger({REQ_DATE + timedelta(days=45): -500})
    assert amount_safe_to_pay(worse, 2000, 100, REQ_DATE, 2000) <= \
        amount_safe_to_pay(base, 2000, 100, REQ_DATE, 2000)


def test_property_03_future_confirmed_credit_never_decreases_safe_amount():
    base = mk_ledger({})
    better = mk_ledger({REQ_DATE + timedelta(days=45): 500})
    assert amount_safe_to_pay(better, 500, 100, REQ_DATE, 2000) >= \
        amount_safe_to_pay(base, 500, 100, REQ_DATE, 2000)


def test_property_04_increasing_requested_amount_never_decreases_safe_amount():
    ledger = mk_ledger({})
    assert amount_safe_to_pay(ledger, 2000, 100, REQ_DATE, 5000) >= \
        amount_safe_to_pay(ledger, 2000, 100, REQ_DATE, 500)


def test_property_05_equality_with_minimum_is_safe_not_a_breach():
    ledger = mk_ledger({})
    safe = amount_safe_to_pay(ledger, 1000, 400, REQ_DATE, 600)
    assert safe == 600
    assert trough(ledger, 1000, REQ_DATE, {REQ_DATE: safe}) == 400


# ===========================================================================
# SECTION 7 — EARLIEST-DATE MONOTONICITY (PROPERTY 6)
# ===========================================================================

def test_property_06_delaying_confirmed_income_never_moves_earliest_date_earlier():
    day20, day25 = REQ_DATE + timedelta(days=20), REQ_DATE + timedelta(days=25)
    ledger = mk_ledger({day20: 500, day25: 500})
    on_time = earliest_date_for_full_payment(ledger, 100, 50, REQ_DATE, 400, [day20])
    delayed = earliest_date_for_full_payment(ledger, 100, 50, REQ_DATE, 400, [day25])
    assert on_time == day20 and delayed == day25 and delayed >= on_time


# ===========================================================================
# SECTION 8 — TROUGH PROPERTIES (PROPERTIES 7-9)
# ===========================================================================

def test_property_07_removing_optional_future_expense_cannot_lower_trough():
    with_expense = mk_ledger({REQ_DATE + timedelta(days=30): -400})
    without = mk_ledger({})
    assert trough(without, 1000, REQ_DATE) >= trough(with_expense, 1000, REQ_DATE)


def test_property_08_adding_an_expense_cannot_improve_trough():
    base = mk_ledger({})
    added = mk_ledger({REQ_DATE + timedelta(days=15): -250})
    assert trough(added, 1000, REQ_DATE) <= trough(base, 1000, REQ_DATE)


def test_property_09_same_day_income_and_expense_are_netted_before_trough():
    salary = payroll(on_date="2025-01-15", amount=1000.0)
    rent = event("rent", "Rent", "2025-01-15", 400.0, category="rent")
    c = case([salary, rent], request="2025-01-15", balance=100.0)
    trace = build_ledger_trace(c, d("2025-01-15"))
    assert trace.ledger[d("2025-01-15")] == 600.0  # +1000 -400 netted, not a phantom dip
    assert trough(trace.ledger, 100.0, d("2025-01-15")) == 700.0  # 100 + 600, no intraday dip below 100


# ===========================================================================
# SECTION 9 — AFFORDABILITY TERMINATION (PROPERTY 10)
# ===========================================================================

def test_property_10_terminating_income_stream_never_improves_affordability():
    active_rows = [payroll(on_date="2025-01-15")]
    terminated_rows = [payroll(on_date="2024-12-15"), payroll("final", "Final employer payroll", "2025-01-15")]
    kwargs = dict(request="2025-02-01", balance=100.0, minimum=50.0, requested=800.0)
    trace_active = build_ledger_trace(case(active_rows, **kwargs), d("2025-02-01"))
    trace_terminated = build_ledger_trace(case(terminated_rows, **kwargs), d("2025-02-01"))
    safe_active = amount_safe_to_pay(trace_active.ledger, 100.0, 50.0, d("2025-02-01"), 800.0)
    safe_terminated = amount_safe_to_pay(trace_terminated.ledger, 100.0, 50.0, d("2025-02-01"), 800.0)
    assert safe_terminated <= safe_active


# ===========================================================================
# SECTION 10 — PAYMENT PLAN CONSERVATION (PROPERTIES 11-12)
# ===========================================================================

def test_property_11_partial_payment_conservation():
    c = case([payroll(on_date="2025-01-15", amount=1000.0)], request="2025-02-01", balance=200.0,
             minimum=50.0, requested=1000.0, methods=("full_payment", "partial_payment"),
             partial=True, deadline_days=30)
    dec = decide_case(c)
    assert dec.recommended_payment_method == "partial_payment"
    parts = dec.payment_plan.split("|")
    assert len(parts) == 2
    (d1, a1), (d2, a2) = (p.split(":") for p in parts)
    assert d1 == c.request.request_date.isoformat()
    assert d2 == dec.earliest_date_for_full_payment
    assert abs(float(a1) + float(a2) - 1000.0) <= 0.01


def test_property_12_installments_exactly_reproduce_a_supplied_option():
    opt = PaymentOption("opt_prop12", "prop-request", "installments", 400.0, 3,
                         d("2025-02-10"), 30, 20.0, 1200.0)
    c = case([], request="2025-02-01", balance=2000.0, minimum=0.0, requested=1200.0,
             methods=("installments",), options=[opt], deadline_days=90, max_installments=3)
    dec = decide_case(c)
    assert dec.recommended_payment_method == "installments"
    assert dec.affordability_status == "affordable_with_plan"
    parts = [p.split(":") for p in dec.payment_plan.split("|")]
    assert len(parts) == 3
    expected_dates = [d("2025-02-10") + timedelta(days=30 * k) for k in range(3)]
    for (ds, amt), ed in zip(parts, expected_dates):
        assert ds == ed.isoformat()
        assert float(amt) == 400.0


# ===========================================================================
# SECTION 11 — METHOD / DEADLINE PROPERTIES (PROPERTIES 13-15)
# ===========================================================================

def test_property_13_selected_method_is_always_user_accepted():
    opt = PaymentOption("opt_p13", "prop-request", "installments", 300.0, 2, d("2025-02-05"), 30, 0.0, 600.0)
    c = case([], request="2025-02-01", balance=2000.0, requested=600.0, methods=("full_payment",),
             options=[opt], max_installments=6)
    dec = decide_case(c)
    assert dec.recommended_payment_method != "installments"
    assert dec.recommended_payment_method in {"full_payment", "wait", "not_recommended"}


def test_property_14_selected_plan_meets_deadline():
    opt = PaymentOption("opt_p14", "prop-request", "installments", 300.0, 5, d("2025-02-05"), 30, 0.0, 1500.0)
    c = case([], request="2025-02-01", balance=2000.0, requested=1500.0, methods=("installments",),
             options=[opt], deadline_days=60, max_installments=6)
    dec = decide_case(c)
    if dec.recommended_payment_method != "not_recommended":
        last_date = max(date.fromisoformat(p.split(":")[0]) for p in dec.payment_plan.split("|"))
        assert last_date <= c.request.desired_completion_date
    else:
        assert dec.payment_plan == "none" and dec.earliest_date_for_full_payment == ""


def test_property_15_not_affordable_contract_is_internally_consistent():
    c = case([], request="2025-02-01", balance=10.0, minimum=1000.0, requested=500.0, methods=())
    dec = decide_case(c)
    assert dec.affordability_status == "not_affordable"
    assert dec.recommended_payment_method == "not_recommended"
    assert dec.payment_plan == "none"
    assert dec.earliest_date_for_full_payment == ""
    assert dec.amount_safe_to_pay >= 0.0


# ===========================================================================
# SECTION 12 — SPENDING-CHANGE PROPERTIES (PROPERTIES 16-23)
# ===========================================================================

def _spend_profile_case(events, protect=(), willing_stop=(), willing_reduce=(), **kwargs):
    c = case(events, **kwargs)
    c.profile.expense_categories_to_protect = list(protect)
    c.profile.expense_categories_user_is_willing_to_stop = list(willing_stop)
    c.profile.expense_categories_user_is_willing_to_reduce = list(willing_reduce)
    return c


def test_property_16_at_most_three_spending_changes():
    events = []
    cats = ["cat1", "cat2", "cat3", "cat4", "cat5"]
    for cat in cats:
        events.append(event(f"{cat}_a", "spend", "2025-12-01", category=cat, flexibility="stoppable"))
        events.append(event(f"{cat}_b", "spend", "2026-01-01", category=cat, flexibility="stoppable"))
    c = _spend_profile_case(events, willing_stop=cats, request="2026-02-01")
    eligible = eligible_changes(c)
    assert len(eligible) == 5
    for subset in enumerate_change_subsets(eligible, max_size=3):
        assert len(subset) <= 3


def test_property_17_no_duplicate_event_id_within_a_subset():
    events = [event("dup_a", "Dining", "2025-12-01", category="dining", flexibility="reducible_or_stoppable", minimum_allowed_amount=5.0),
              event("dup_b", "Dining", "2026-01-01", category="dining", flexibility="reducible_or_stoppable", minimum_allowed_amount=5.0)]
    c = _spend_profile_case(events, willing_stop=["dining"], willing_reduce=["dining"], request="2026-02-01")
    eligible = eligible_changes(c)
    assert eligible  # sanity: the fixture actually produces candidates
    for subset in enumerate_change_subsets(eligible, max_size=3):
        ids = [chg.event_id for chg in subset]
        assert len(ids) == len(set(ids))


def test_property_18_stop_and_reduce_cannot_target_the_same_event_together():
    events = [event("both_a", "Dining", "2025-12-01", category="dining", flexibility="reducible_or_stoppable", minimum_allowed_amount=5.0),
              event("both_b", "Dining", "2026-01-01", category="dining", flexibility="reducible_or_stoppable", minimum_allowed_amount=5.0)]
    c = _spend_profile_case(events, willing_stop=["dining"], willing_reduce=["dining"], request="2026-02-01")
    eligible = eligible_changes(c)
    kinds_by_event = {chg.event_id: {c2.kind for c2 in eligible if c2.event_id == chg.event_id} for chg in eligible}
    assert kinds_by_event["both_b"] == {"stop", "reduce_to"}  # both offered individually
    for subset in enumerate_change_subsets(eligible, max_size=3):
        touched = [chg.event_id for chg in subset]
        assert len(touched) == len(set(touched))  # never co-occur in one subset


def test_property_19_protected_categories_are_never_offered():
    events = [event("prot_a", "Dining", "2025-12-01", category="dining", flexibility="stoppable"),
              event("prot_b", "Dining", "2026-01-01", category="dining", flexibility="stoppable"),
              event("free_a", "Fun", "2025-12-01", category="entertainment", flexibility="stoppable"),
              event("free_b", "Fun", "2026-01-01", category="entertainment", flexibility="stoppable")]
    c = _spend_profile_case(events, protect=["dining"], willing_stop=["dining", "entertainment"],
                             request="2026-02-01")
    categories = {chg.category for chg in eligible_changes(c)}
    assert "dining" not in categories and "entertainment" in categories


def test_property_20_fixed_streams_are_never_offered():
    events = [event("fix_a", "Rent", "2025-12-01", category="rent", flexibility="fixed"),
              event("fix_b", "Rent", "2026-01-01", category="rent", flexibility="fixed")]
    c = _spend_profile_case(events, willing_stop=["rent"], willing_reduce=["rent"], request="2026-02-01")
    assert eligible_changes(c) == []


def test_property_21_reduce_to_never_falls_below_minimum_allowed_amount():
    events = [event("red_a", "Dining", "2025-12-01", amount=150.0, category="dining", flexibility="reducible", minimum_allowed_amount=45.5),
              event("red_b", "Dining", "2026-01-01", amount=150.0, category="dining", flexibility="reducible", minimum_allowed_amount=45.5)]
    c = _spend_profile_case(events, willing_reduce=["dining"], request="2026-02-01")
    reduce_changes = [chg for chg in eligible_changes(c) if chg.kind == "reduce_to"]
    assert len(reduce_changes) == 1
    assert reduce_changes[0].target_amount >= 45.5


def test_property_22_spending_changes_apply_only_to_future_occurrences():
    events = [event("hist_a", "rent", "2024-12-15", 400.0, category="rent", flexibility="stoppable"),
              event("hist_b", "rent", "2025-01-15", 400.0, category="rent", flexibility="stoppable")]
    c = case(events, request="2025-02-01")
    baseline = build_ledger_trace(c, d("2025-02-01"))
    changed = build_ledger_trace(c, d("2025-02-01"), changes={"hist_b": None})
    before_request_date = [row for row in baseline.contributions if row.date < d("2025-02-01")]
    after_change = [row for row in changed.contributions if row.date < d("2025-02-01")]
    assert before_request_date == after_change  # historical state is untouched
    assert sum(changed.ledger.values()) > sum(baseline.ledger.values())  # only future occurrences dropped


def test_property_23_spending_search_never_redefines_baseline_capacity():
    events = [event("cap_a", "dining", "2025-12-01", amount=80.0, flexibility="stoppable"),
              event("cap_b", "dining", "2026-01-01", amount=80.0, flexibility="stoppable")]
    c = _spend_profile_case(events, willing_stop=["dining"], request="2026-01-15", requested=250.0,
                             deadline_days=27, balance=300.0, minimum=100.0)
    trace = build_ledger_trace(c, d("2026-01-15"))
    baseline_safe = amount_safe_to_pay(trace.ledger, 300.0, 100.0, d("2026-01-15"), 250.0)
    baseline_earliest = earliest_date_for_full_payment(
        trace.ledger, 300.0, 100.0, d("2026-01-15"), 250.0,
        {row.date for row in trace.contributions if row.amount > 0},
    )
    search_spending_plans(c, trace, baseline_safe, baseline_earliest)  # may explore spending changes
    # Baseline quantities, recomputed the same way, must be untouched by the search.
    assert amount_safe_to_pay(trace.ledger, 300.0, 100.0, d("2026-01-15"), 250.0) == baseline_safe
    assert earliest_date_for_full_payment(
        trace.ledger, 300.0, 100.0, d("2026-01-15"), 250.0,
        {row.date for row in trace.contributions if row.amount > 0},
    ) == baseline_earliest


# ===========================================================================
# SECTION 13 — PERMUTATION INVARIANCE (PROPERTY 24)
# ===========================================================================

def test_property_24_permutation_invariance_on_real_request_events():
    data, _ = load_and_index()
    fx.init_rates(data.exchange_rates)
    template = build_request_case(data.requests[0].request_id)
    original_events = list(template.events)

    rng = random.Random(20260913)  # fixed seed, reported in the final summary
    shuffled_events = list(original_events)
    rng.shuffle(shuffled_events)

    case_a = dataclasses.replace(template, events=list(original_events))
    case_b = dataclasses.replace(template, events=shuffled_events)

    dec_a = decide_case(case_a)
    dec_b = decide_case(case_b)
    assert dec_a == dec_b


# ---------------------------------------------------------------------------
# SECTION 13.1 — MESSAGE ORDER INVARIANCE (PROPERTY 25)
# ---------------------------------------------------------------------------

def test_property_25_message_order_invariance_with_equal_timestamps():
    salary = payroll("sal", "Base salary", "2025-05-15", 3000.0)
    rent = event("rent", "Rent", "2025-04-15", 1000.0, category="rent",
                 flexibility="reducible_or_stoppable", minimum_allowed_amount=100.0)
    same_ts = "2025-06-01T00:00:00Z"
    msg_salary = ("msg_ord_sal", same_ts, "Your monthly pay is now USD 3600.")
    msg_rent = ("msg_ord_rent", same_ts, "The renewed lease increases monthly rent by 10%. Case ref SER-9999.")

    from code.domain.models import Message

    def build(order):
        msgs = [Message(mid, "prop-user", None, None, ts, "sender", text) for mid, ts, text in order]
        return case([salary, rent], request="2025-06-01", messages=msgs, balance=5000.0, minimum=200.0)

    dec_fwd = decide_case(build([msg_salary, msg_rent]))
    dec_rev = decide_case(build([msg_rent, msg_salary]))
    assert dec_fwd == dec_rev


# ===========================================================================
# SECTION 14 — CALENDAR / RECURRENCE REGRESSION (PROPERTIES 26-30)
# ===========================================================================

def test_property_26_monthly_stream_rolls_by_calendar_month_not_plus_30_days():
    assert occurrences(d("2025-01-31"), 30, d("2025-04-01")) == [d("2025-02-28"), d("2025-03-31")]


def test_property_27_seven_day_recurrence_remains_exactly_seven_days():
    assert occurrences(d("2025-01-01"), 7, d("2025-01-29")) == [
        d("2025-01-08"), d("2025-01-15"), d("2025-01-22"), d("2025-01-29"),
    ]


def test_property_28_29_30_horizon_boundaries():
    start = d("2025-02-01")
    rows = [event("s", "Scheduled debit", start, 5.0, status="scheduled"),
            event("e", "Scheduled debit", start + timedelta(days=90), 6.0, status="scheduled"),
            event("x", "Scheduled debit", start + timedelta(days=91), 7.0, status="scheduled")]
    ledg = ledger_of(rows, request="2025-02-01")
    assert ledg[start] == -5.0                                    # PROPERTY 28: request_date included
    assert ledg[start + timedelta(days=90)] == -6.0                # PROPERTY 29: +90 days included
    assert start + timedelta(days=91) not in ledg                  # PROPERTY 30: +91 days excluded


# ===========================================================================
# SECTION 15 — LEDGER REGRESSION (PROPERTIES 31-42)
# ===========================================================================

def test_property_31_historical_settled_transactions_are_not_replayed():
    rows = [event("old", "Rent", "2025-01-01", 400.0, category="rent")]
    assert not any(ledger_of(rows, request="2025-02-01").values())


def test_property_32_pending_debit_counts_on_settlement_date():
    rows = [event("p", "Pending debit", "2025-01-20", 300.0, status="pending", settlement_date=d("2025-02-10"))]
    ledg = ledger_of(rows, request="2025-02-01")
    assert ledg[d("2025-02-10")] == -300.0


def test_property_33_pending_credit_does_not_enter_forecast():
    rows = [event("c", "Pending credit", "2025-02-05", 300.0, status="pending", direction="credit")]
    assert not any(ledger_of(rows, request="2025-02-01").values())


def test_property_34_cancelled_transaction_does_not_enter_future_cash():
    rows = [event("c", "Cancelled debit", "2025-02-05", 300.0, status="cancelled")]
    assert not any(ledger_of(rows, request="2025-02-01").values())


def test_property_35_failed_debit_alone_does_not_enter_future_cash():
    rows = [event("f", "Failed debit", "2025-02-05", 300.0, status="failed")]
    assert not any(ledger_of(rows, request="2025-02-01").values())


def test_property_36_failed_debit_with_scheduled_retry_counts_the_retry_once():
    rows = [event("f", "Failed bill payment attempt", "2025-02-02", 125.0, status="failed", category="utilities"),
            event("r", "Scheduled bill payment retry", "2025-02-03", 125.0, status="scheduled",
                  category="utilities", linked_event_id="f", settlement_date=d("2025-02-07"))]
    ledg = ledger_of(rows, request="2025-02-01")
    assert ledg[d("2025-02-02")] == 0.0
    assert ledg[d("2025-02-07")] == -125.0
    assert math.fsum(ledg.values()) == -125.0


def test_property_37_duplicate_card_authorization_purchase_pair_counts_once():
    rows = [event("auth", "Card authorization", "2025-02-02", 125.0, status="pending"),
            event("purchase", "Settled card purchase", "2025-02-05", 130.0, linked_event_id="auth")]
    assert math.fsum(ledger_of(rows, request="2025-02-01").values()) == -130.0


def test_property_38_duplicate_charge_pair_counts_once():
    rows = [event("orig", "Original card charge", "2025-02-03", 125.0),
            event("dup", "Possible duplicate card charge", "2025-02-05", 125.0, status="pending",
                  linked_event_id="orig")]
    assert math.fsum(ledger_of(rows, request="2025-02-01").values()) == -125.0


def test_property_39_reversal_pair_nets_to_zero():
    rows = [event("charge", "Card charge later reversed", "2025-02-03", 125.0),
            event("rev", "Settled card charge reversal", "2025-02-05", 125.0, event_type="refund",
                  direction="credit", linked_event_id="charge")]
    assert math.fsum(ledger_of(rows, request="2025-02-01").values()) == 0.0


def test_property_40_unrealized_valuation_never_becomes_cash():
    rows = [event("v", "Current portfolio valuation", "2025-02-05", 9000.0,
                  event_type="investment_valuation", status="unrealized")]
    assert not any(ledger_of(rows, request="2025-02-01").values())


def test_property_41_already_settled_investment_sale_creates_no_future_proceeds():
    rows = [event("sell", "Investment sale proceeds", "2025-01-20", 500.0,
                  event_type="investment_sale", direction="credit")]
    assert not any(ledger_of(rows, request="2025-02-01").values())


def test_property_42_reimbursement_is_not_converted_into_recurring_salary():
    rows = [event("work", "Reimbursable work expense", "2025-02-03", 125.0, category="work_expense"),
            event("reimb", "Employer expense reimbursement", "2025-02-20", 125.0, category="salary",
                  direction="credit", event_type="refund", linked_event_id="work")]
    assert project_salary_streams(rows, d("2025-02-01"), home_currency="USD") == []


# ===========================================================================
# SECTION 16 — SALARY REGRESSION (PROPERTIES 43-51)
# ===========================================================================

def test_property_43_final_employer_payroll_terminates_projected_salary():
    rows = [payroll(on_date="2024-12-15"), payroll("final", "Final employer payroll", "2025-01-15")]
    assert project_salary_streams(rows, d("2025-02-01"), home_currency="USD") == []


def test_property_44_next_confirmed_replaces_projected_occurrence_not_doubles_it():
    rows = [payroll(on_date="2025-01-15", amount=1000.0),
            payroll("next", "Next confirmed salary", "2025-02-15", 1600.0, status="scheduled")]
    stream, = project_salary_streams(rows, d("2025-02-01"), home_currency="USD")
    feb_total = sum(o.amount for o in stream.occurrences if o.date.month == 2)
    assert feb_total == 1600.0  # never 1000 + 1600


def test_property_45_two_independent_salary_streams_remain_separate():
    rows = [payroll("p1", "Primary household salary", "2025-01-15", 3000.0),
            payroll("p2", "Second household income", "2025-01-20", 1500.0)]
    streams = project_salary_streams(rows, d("2025-02-01"), home_currency="USD")
    assert {s.level for s in streams} == {3000.0, 1500.0}


def test_property_46_salary_amount_amendment_changes_intended_stream_only():
    salary = payroll("sal", "Base salary", "2025-05-15", 3000.0)
    rent = event("rent", "Rent", "2025-04-15", 1000.0, category="rent")
    c = case([salary, rent], request="2025-06-01")
    delta = EvidenceDelta(source_id="m46", user_id="prop-user", intent=EvidenceIntent.SALARY_SET_AMOUNT.value,
                           target_type="stream", target="salary", amount=4000.0, currency="USD",
                           effective_date=d("2025-07-01"))
    result = apply_deltas(c, [delta])
    rent_after = next(e for e in result.events if e.event_id == "rent")
    salary_after = next(e for e in result.events if e.event_id == "sal")
    assert rent_after == rent
    assert salary_after == salary  # the original settled record is not rewritten in place


def test_property_47_salary_date_amendment_changes_intended_anchor_only():
    salary = payroll("sal", "Base salary", "2025-05-15", 3000.0)
    c = case([salary], request="2025-06-01")
    delta = EvidenceDelta(source_id="m47", user_id="prop-user", intent=EvidenceIntent.SALARY_SET_DATE.value,
                           target_type="stream", target="salary", effective_date=d("2025-07-20"))
    result = apply_deltas(c, [delta])
    new_ev = next(e for e in result.events if e.event_id.startswith("delta_salary_date_"))
    assert new_ev.settlement_date == d("2025-07-20")
    assert new_ev.amount == 3000.0  # carries the prior confirmed level forward, never invented


def test_property_48_salary_resume_restores_the_intended_recurring_stream():
    # "Payroll before leave" is a temporary pause, not a permanent termination
    # (unlike "Final employer payroll", which BUILD-PLAN and test_salary.py's
    # own test_next_confirmation_cannot_revive_terminated_salary establish as
    # unrecoverable by design) -- so a resume signal can legitimately restart it.
    rows = [payroll("before", "Payroll before leave", "2024-11-15", 2000.0)]
    c = case(rows, request="2025-03-01")
    delta = EvidenceDelta(source_id="m48", user_id="prop-user", intent=EvidenceIntent.SALARY_RESUME.value,
                           target_type="stream", target="salary", amount=2200.0, currency="USD",
                           effective_date=d("2025-04-01"))
    result = apply_deltas(c, [delta])
    streams = project_salary_streams(result.events, d("2025-03-01"), home_currency="USD")
    assert len(streams) == 1 and streams[0].level == 2200.0


def test_property_49_prorated_salary_does_not_set_the_recurring_level():
    rows = [event("prorated", "Prorated first salary", "2025-01-10", 400.0, category="salary",
                  direction="credit", event_type="income"),
            payroll("normal", "Payroll credit", "2025-01-15", 2000.0)]
    stream, = project_salary_streams(rows, d("2025-02-01"), home_currency="USD")
    assert stream.level == 2000.0


def test_property_50_gig_and_irregular_income_is_never_projected_as_salary():
    rows = [event(f"gig{n}", "Weekly app earnings", f"2025-01-{n:02d}", 200.0, category="salary",
                  direction="credit", event_type="income") for n in (3, 10, 17, 24)]
    assert project_salary_streams(rows, d("2025-02-01"), home_currency="USD") == []


def test_property_51_foreign_salary_uses_established_fx_conversion(monkeypatch):
    monkeypatch.setattr(fx, "_rate_table", {})
    monkeypatch.setattr(fx, "_conversion_log", [])
    fx.init_rates([ExchangeRate(d("2025-02-15"), "USD", "INR", 80.0)])
    rows = [event("usd", "International employer payroll", "2025-01-15", 100.0, category="salary",
                  direction="credit", event_type="income", currency="USD")]
    stream, = project_salary_streams(rows, d("2025-02-01"), horizon=20, home_currency="INR")
    assert stream.occurrences[0].amount == 8000.0


# ===========================================================================
# SECTION 17 — IMAGE REGRESSION (PROPERTIES 52-57)
# ===========================================================================

def _register_sample_requests():
    from code.io import indexes
    load_and_index()
    sample_df = pd.read_csv(REPO_ROOT / "dataset" / "sample_requests.csv")
    for _, row in sample_df.iterrows():
        req = _row_to_request(row)
        indexes._requests_by_id[req.request_id] = req


def test_property_52_blank_amount_never_becomes_zero():
    extractions = extract_all_images()
    for ev in extractions.values():
        assert ev.amount != 0.0


def test_property_53_unresolved_image_never_silently_becomes_cash():
    _register_sample_requests()
    case_ = build_request_case("request_19")  # image_04 -> event_1700, a rejected 'Item Bill' subtotal
    extractions = extract_all_images()
    img04 = extractions["image_04"]
    assert img04.is_valid is False and img04.amount is None

    result = apply_image_evidence(case_, extractions)
    ev = next(e for e in result.events if e.event_id == "event_1700")
    assert ev.amount is None  # still blank, never defaulted to 0 or invented

    # event_1700 is the NEWEST event in its (weekly) groceries stream, so it
    # legitimately supplies the stream's `representative_event_id` for
    # spending_changes_needed purposes -- but its own blank amount must never
    # enter the mean used to project future cash (test_ledger.py proves the
    # same exclusion rule; this re-verifies it on the real unresolved image).
    from code.reconstruct.streams import reconstruct_expense_streams
    streams = reconstruct_expense_streams(result.events, result.request.request_date)
    groceries_stream = next(s for s in streams if s.representative_event_id == "event_1700")
    assert "event_1700" not in groceries_stream.amount_event_ids


def test_property_54_image_01_resolves_to_the_required_verified_values():
    extractions = extract_all_images()
    img_01 = extractions["image_01"]
    assert img_01.amount == 4365000.0
    assert img_01.currency == "IDR"
    assert img_01.amount_label == "Net Pay"


def test_property_55_gross_salary_is_rejected_when_event_requires_net_pay():
    from code.evidence.images import ImageEventMapping, ImageEvidence, resolve_image_path
    mapping = ImageEventMapping(
        image_id="image_01", event_id="event_253", user_id="user_03", request_id="request_03",
        description="August 2019 net salary", category="salary", status="settled",
        declared_currency="IDR", event_date="2019-08-31", settlement_date="2019-08-31",
        image_path=resolve_image_path("image_01"), priority="LOW",
    )
    gross = ImageEvidence(document_type="payslip", amount=4780800.0, amount_label="Total Earnings",
                           currency="IDR", date="2019-08-31", status="settled")
    ok, reason = validate_image_evidence(gross, mapping)
    assert ok is False and reason is not None


def test_property_56_image_belongs_only_to_its_linked_event_and_user():
    _register_sample_requests()
    extractions = extract_all_images()
    case_16 = apply_image_evidence(build_request_case("request_16"), extractions)
    case_19 = apply_image_evidence(build_request_case("request_19"), extractions)
    # image_02 (-> event_1442, user_16) must never surface on user_19's case.
    assert not any(e.event_id == "event_1442" for e in case_19.events if e.amount == -100000.0)
    prov_16 = getattr(case_16, "_evidence_provenance", {})
    prov_19 = getattr(case_19, "_evidence_provenance", {})
    assert set(prov_16).isdisjoint(set(prov_19))


def test_property_57_applying_identical_image_evidence_twice_is_idempotent():
    _register_sample_requests()
    extractions = extract_all_images()
    once = apply_image_evidence(build_request_case("request_16"), extractions)
    twice = apply_image_evidence(apply_image_evidence(build_request_case("request_16"), extractions), extractions)
    fp = lambda c: sorted((e.event_id, e.amount) for e in c.events)
    assert fp(once) == fp(twice)


# ===========================================================================
# SECTION 18 — MESSAGE REGRESSION (PROPERTIES 58-68)
# ===========================================================================

def test_property_58_no_op_does_nothing():
    events = [payroll()]
    c = case(events)
    before = [(e.event_id, e.amount, e.status) for e in c.events]
    delta = EvidenceDelta(source_id="m58", user_id="prop-user", intent=EvidenceIntent.NO_OP.value,
                           target_type="stream", target="none")
    result = apply_deltas(c, [delta])
    assert [(e.event_id, e.amount, e.status) for e in result.events] == before


def test_property_59_unconfirmed_income_does_not_become_recurring():
    rows = [payroll("confirmed", "Payroll credit", "2024-12-15", 1000.0),
            event("bonus", "Quarterly bonus", "2025-06-05", 1200.0, category="salary",
                  direction="credit", event_type="income", status="pending")]
    c = case(rows, request="2025-06-01")
    delta = EvidenceDelta(source_id="m59", user_id="prop-user", intent=EvidenceIntent.INCOME_UNCONFIRMED.value,
                           target_type="event", target="bonus")
    result = apply_deltas(c, [delta])
    streams = project_salary_streams(result.events, d("2025-06-01"), home_currency="USD")
    assert len(streams) == 1 and streams[0].level == 1000.0  # bonus never joins the recurring level
    bonus_after = next(e for e in result.events if e.event_id == "bonus")
    assert bonus_after.status == "pending"


def test_property_60_pending_refund_does_not_become_future_cash():
    refund = event("refund", "Store refund", "2025-06-03", 80.0, category="shopping",
                   direction="credit", status="settled")
    c = case([refund], request="2025-06-01")
    delta = EvidenceDelta(source_id="m60", user_id="prop-user", intent=EvidenceIntent.REFUND_PENDING.value,
                           target_type="event", target="refund", amount=80.0, currency="USD")
    result = apply_deltas(c, [delta])
    trace = build_ledger_trace(result, result.request.request_date)
    assert all(row.event_id != "refund" for row in trace.contributions)


def test_property_61_salary_amendment_never_touches_second_household_income():
    rows = [payroll("primary", "Primary household salary", "2025-01-15", 3000.0),
            payroll("second", "Second household income", "2025-01-20", 1500.0)]
    c = case(rows, request="2025-02-01")
    delta = EvidenceDelta(source_id="m61", user_id="prop-user", intent=EvidenceIntent.SALARY_SET_AMOUNT.value,
                           target_type="stream", target="salary", amount=4000.0, currency="USD",
                           effective_date=d("2025-03-01"))
    result = apply_deltas(c, [delta])
    streams = project_salary_streams(result.events, d("2025-02-01"), home_currency="USD")
    levels = {s.description: s.level for s in streams}
    assert levels["Second household income"] == 1500.0
    assert levels["Primary household salary"] == 4000.0


def test_property_62_one_time_arrears_remains_one_time():
    salary = payroll("sal", "Base salary", "2025-05-15", 3000.0)
    c = case([salary], request="2025-06-01")
    delta = EvidenceDelta(source_id="m62", user_id="prop-user", intent=EvidenceIntent.ONE_TIME_ARREARS.value,
                           target_type="stream", target="salary", amount=500.0, currency="USD",
                           effective_date=d("2025-06-20"))
    result = apply_deltas(c, [delta])
    unchanged_salary = next(e for e in result.events if e.event_id == "sal")
    assert unchanged_salary.amount == 3000.0
    one_off = [e for e in result.events if e.event_id.startswith("delta_oneoff_")]
    assert len(one_off) == 1 and one_off[0].amount == 500.0
    streams = project_salary_streams(result.events, d("2025-06-01"), home_currency="USD")
    assert len(streams) == 1 and streams[0].level == 3000.0  # recurring level unaffected


def test_property_63_salary_resume_never_fabricates_an_expense():
    rows = [payroll("old", "Payroll credit", "2024-11-15", 2000.0),
            payroll("final", "Final employer payroll", "2024-12-15")]
    c = case(rows, request="2025-02-01")
    delta = EvidenceDelta(source_id="m63", user_id="prop-user", intent=EvidenceIntent.SALARY_RESUME.value,
                           target_type="stream", target="salary", amount=2100.0, currency="USD",
                           effective_date=d("2025-03-01"))
    result = apply_deltas(c, [delta])
    new_events = [e for e in result.events if e.event_id not in {"old", "final"}]
    assert new_events and all(e.category == "salary" and e.direction == "credit" for e in new_events)


def test_property_64_rent_increase_affects_future_rent_only():
    old = event("rent_old", "Rent", "2024-12-15", 1000.0, category="rent",
                flexibility="reducible_or_stoppable", minimum_allowed_amount=100.0)
    new = event("rent_new", "Rent", "2025-01-15", 1000.0, category="rent",
                flexibility="reducible_or_stoppable", minimum_allowed_amount=100.0)
    c = case([old, new], request="2025-02-01")
    delta = EvidenceDelta(source_id="m64", user_id="prop-user", intent=EvidenceIntent.RENT_INCREASE_PCT.value,
                           target_type="stream", target="rent", percent=20.0)
    result = apply_deltas(c, [delta])

    old_after = next(e for e in result.events if e.event_id == "rent_old")
    new_after = next(e for e in result.events if e.event_id == "rent_new")
    assert old_after.amount == 1000.0, (
        "PRE-EXISTING SOLVER DEFECT (Step 10, code/evidence/apply.py RENT_INCREASE_PCT): "
        "the handler rewrites EVERY matching rent debit event's amount, including history "
        "dated before the amendment, instead of scaling only future projected occurrences."
    )
    assert new_after.amount == 1000.0

    # The functional guarantee that actually matters for the forecast: the
    # projected FUTURE rent debit reflects the increase.
    trace = build_ledger_trace(result, d("2025-02-01"))
    future_rent = [row for row in trace.contributions if row.category == "rent"]
    assert future_rent and all(row.amount == -1200.0 for row in future_rent)


def test_property_65_self_transfer_duplicate_does_not_inflate_cash():
    transfer = event("xfer", "Internal transfer", "2025-02-05", 500.0, category="transfer",
                      direction="credit", event_type="transfer", status="settled")
    c = case([transfer], request="2025-02-01")
    delta = EvidenceDelta(source_id="m65", user_id="prop-user", intent=EvidenceIntent.SELF_TRANSFER_DUPLICATE.value,
                           target_type="event", target="xfer", amount=500.0, currency="USD")
    result = apply_deltas(c, [delta])
    trace = build_ledger_trace(result, d("2025-02-01"))
    assert math.fsum(trace.ledger.values()) == 0.0


def test_property_66_dispute_does_not_silently_erase_the_debit():
    debit = event("card_dispute", "Card charge", "2025-02-05", 300.0, status="settled")
    c = case([debit], request="2025-02-01")
    delta = EvidenceDelta(source_id="m66", user_id="prop-user", intent=EvidenceIntent.DISPUTE_OPEN.value,
                           target_type="event", target="card_dispute", amount=300.0, currency="USD")
    result = apply_deltas(c, [delta])
    updated = next(e for e in result.events if e.event_id == "card_dispute")
    assert updated.amount == 300.0  # amount preserved, not erased
    assert updated.status == "pending"  # under investigation, not settled cash either


def test_property_67_failed_debit_retry_creates_exactly_one_obligation():
    debit = event("retry_debit", "Card payment", "2025-02-05", 200.0, status="failed",
                  category="debt_repayment", flexibility="fixed")
    c = case([debit], request="2025-02-01")
    delta = EvidenceDelta(source_id="m67", user_id="prop-user", intent=EvidenceIntent.FAILED_DEBIT_RETRY.value,
                           target_type="event", target="retry_debit")
    result = apply_deltas(c, [delta])
    trace = build_ledger_trace(result, d("2025-02-01"))
    matches = [row for row in trace.contributions if row.event_id == "retry_debit"]
    assert len(matches) == 1
    assert matches[0].amount == -200.0


def test_property_68_fx_settlement_uses_established_date_aware_fx_logic(monkeypatch):
    monkeypatch.setattr(fx, "_rate_table", {})
    monkeypatch.setattr(fx, "_conversion_log", [])
    fx.init_rates([ExchangeRate(d("2025-02-01"), "USD", "INR", 80.0),
                   ExchangeRate(d("2025-03-01"), "USD", "INR", 85.0)])
    debit = event("fx_evt", "Foreign card charge", "2025-02-05", 100.0, status="scheduled",
                  currency="USD", settlement_date=d("2025-02-05"))
    c = case([debit], request="2025-02-01", currency="INR")
    delta = EvidenceDelta(source_id="m68", user_id="prop-user", intent=EvidenceIntent.FX_SETTLEMENT.value,
                           target_type="event", target="fx_evt", effective_date=d("2025-03-01"))
    result = apply_deltas(c, [delta])
    trace = build_ledger_trace(result, d("2025-02-01"))
    assert trace.ledger[d("2025-03-01")] == -8500.0  # 100 * 85 at the AMENDED settlement date
    assert trace.ledger.get(d("2025-02-05"), 0.0) == 0.0


# ===========================================================================
# SECTION 19 — OUTPUT CONTRACT SWEEP (real, current output.csv; read-only)
# ===========================================================================

def test_output_contract_sweep_on_current_output_csv():
    data = load_all()
    out_df = pd.read_csv(REPO_ROOT / "output.csv", dtype=str, keep_default_na=False)
    assert len(out_df) == 250
    assert out_df["request_id"].tolist() == data.requests_df["request_id"].tolist()
    assert out_df["request_id"].duplicated().sum() == 0

    rows = out_df.to_dict("records")
    try:
        violations = validate_rows(rows, data.requests_df, data.options_df, data.profiles_df, data.events_df)
    except ValidationError as exc:
        pytest.fail("Output contract violations:\n" + "\n".join(exc.violations))
    assert violations == []


# ===========================================================================
# SECTION 20/21 — DETERMINISTIC REPLAY AND HASH VERIFICATION
# ===========================================================================

def _run_full_pipeline(out_path: Path) -> list[dict]:
    data, _ = load_and_index()
    fx.init_rates(data.exchange_rates)
    cases = [build_request_case(r.request_id) for r in data.requests]
    deltas = extract_all_deltas(cases)
    images = extract_all_images()
    rows = [decision_row(decide_case(c, deltas, images)) for c in cases]
    write_output(rows, out_path)
    return rows


def test_deterministic_replay_cold_warm_and_fresh_cache(tmp_path, monkeypatch):
    """RUN A (cold) vs RUN B (warm, same cache) vs RUN C (fresh, separate
    cold cache) must all be byte-identical -- proving both that a warm cache
    changes nothing observable and that no run accidentally depends on
    incidental cache contents. The real repository `.cache/evidence` is never
    touched: CACHE_DIR is redirected to isolated temp directories for the
    whole test and restored automatically on teardown."""
    cache_a = tmp_path / "cache_a"
    out_a, out_b, out_c = tmp_path / "out_a.csv", tmp_path / "out_b.csv", tmp_path / "out_c.csv"

    monkeypatch.setattr(cache_mod, "CACHE_DIR", cache_a)
    rows_a = _run_full_pipeline(out_a)   # RUN A: cold cache
    rows_b = _run_full_pipeline(out_b)   # RUN B: warm cache (reuses cache_a)

    cache_c = tmp_path / "cache_c"
    monkeypatch.setattr(cache_mod, "CACHE_DIR", cache_c)
    rows_c = _run_full_pipeline(out_c)   # RUN C: independent fresh cold cache

    bytes_a, bytes_b, bytes_c = out_a.read_bytes(), out_b.read_bytes(), out_c.read_bytes()
    assert bytes_a == bytes_b, "warm-cache replay is not byte-identical to the cold run"
    assert bytes_a == bytes_c, "an independent fresh cold cache changed the output"
    assert rows_a == rows_b == rows_c

    sha_before = hashlib.sha256(bytes_a).hexdigest()
    sha_after = hashlib.sha256(out_b.read_bytes()).hexdigest()
    assert sha_before == sha_after


# ---------------------------------------------------------------------------
# Protected-file guard: the real output.csv / v1 / v2 / v3 must be untouched
# by everything this module did (the replay test above only ever wrote to
# `tmp_path`). Hashes are captured once at collection time, before any test
# in this session runs any solver code against the real repo-root paths.
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_PROTECTED_OUTPUTS = {
    name: _sha256(REPO_ROOT / name)
    for name in ("output.csv", "output_v1_deterministic.csv", "output_v2_evidence.csv", "output_v3_image.csv")
}


def test_protected_output_files_are_unchanged_by_the_step13_test_run():
    for name, before in _PROTECTED_OUTPUTS.items():
        after = _sha256(REPO_ROOT / name)
        assert after == before, f"{name} was modified during the Step 13 test run"

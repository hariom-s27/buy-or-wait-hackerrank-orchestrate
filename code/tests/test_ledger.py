"""Synthetic tests for Step 4; no real user/request IDs or output generation."""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from datetime import date, timedelta
from itertools import permutations

import pytest

from code.domain import fx
from code.domain.models import Event, ExchangeRate, Profile, Request, RequestCase, Stream
from code.forecast.ledger import (
    UnresolvedCashEvents, build_ledger, build_ledger_trace, occurrences, trough,
)
from code.reconstruct.anomalies import partition_events, resolve_special_events
from code.reconstruct.salary import project_salary_streams
from code.reconstruct.streams import reconstruct_expense_streams


def d(value):
    return date.fromisoformat(value)


def event(event_id, description, on_date, amount=100, **changes):
    on_date = d(on_date)
    return replace(Event(
        event_id=event_id, user_id="ledger-test-user", event_type="expense",
        description=description, category="shopping", direction="debit", amount=amount,
        currency="USD", event_date=on_date, settlement_date=on_date, status="settled",
        linked_event_id=None, flexibility="reducible", minimum_allowed_amount=10,
    ), **changes)


def payroll(event_id="pay", description="Payroll credit", on_date="2025-01-15", amount=1000, **changes):
    return event(event_id, description, on_date, amount,
                 category="salary", direction="credit", event_type="income", **changes)


def case(events=(), request="2025-02-01", currency="USD", balance=2000):
    request_date = d(request)
    return RequestCase(
        request=Request("synthetic-purchase", "ledger-test-user", request_date, "purchase", 1,
                        request_date + timedelta(days=90), False, "Synthetic fixture"),
        profile=Profile("ledger-test-user", currency, balance, 0, [], [], [], [], [], None),
        events=list(events), payment_options=[], messages=[], images=[],
    )


def ledger(rows, request="2025-02-01", horizon=90, changes=None, currency="USD"):
    return build_ledger(case(rows, request, currency), d(request), horizon, changes)


def resolve(rows, request="2025-02-01", horizon=90):
    return resolve_special_events(rows, d(request), horizon, home_currency="USD")


def dropped(resolution):
    return {row.event_id: row.reason for row in resolution.dropped_events}


@pytest.mark.parametrize("year,feb_day", [(2024, 29), (2025, 28)])
@pytest.mark.parametrize("gap", [26, 30, 30.5, 32])
def test_calendar_month_rollover(year, feb_day, gap):
    assert occurrences(date(year, 1, 31), gap, date(year, 3, 31)) == [
        date(year, 2, feb_day), date(year, 3, 31),
    ]


def test_fixed_seven_day_gap():
    assert occurrences(d("2025-01-31"), 7, d("2025-02-28")) == [
        d("2025-02-07"), d("2025-02-14"), d("2025-02-21"), d("2025-02-28"),
    ]


@pytest.mark.parametrize("year,feb_day", [(2024, 29), (2025, 28)])
def test_expense_history_preserves_day_31_after_february(year, feb_day):
    rows = [event("jan", "Rent", f"{year}-01-31", 400, category="rent"),
            event("feb", "Rent", f"{year}-02-{feb_day}", 400, category="rent")]
    trace = build_ledger_trace(case(rows, request=f"{year}-03-01"), date(year, 3, 1), horizon=60)
    stream, = trace.expense_streams
    assert stream.last_date == date(year, 2, feb_day)
    assert stream.representative_event_id == "feb"
    assert stream.anchor_day == 31 and stream.calendar_day_event_id == "jan"
    assert [(row.date, row.amount) for row in trace.contributions] == [
        (date(year, 3, 31), -400), (date(year, 4, 30), -400),
    ]


@pytest.mark.parametrize("gap", [25, 33])
def test_outside_monthly_band_uses_elapsed_days(gap):
    anchor = d("2025-01-31")
    assert occurrences(anchor, gap, anchor + timedelta(days=gap * 2)) == [
        anchor + timedelta(days=gap), anchor + timedelta(days=gap * 2),
    ]


def test_fractional_fixed_gap_retains_accumulated_days():
    assert occurrences(d("2025-01-01"), 7.5, d("2025-01-31")) == [
        d("2025-01-08"), d("2025-01-16"), d("2025-01-23"), d("2025-01-31"),
    ]


def test_same_day_netting_has_no_artificial_dip():
    rows = [payroll(), event("rent-old", "Rent", "2024-12-15", 400, category="rent"),
            event("rent-new", "Rent", "2025-01-15", 400, category="rent")]
    result = ledger(rows)
    assert result[d("2025-02-15")] == 600
    assert result[d("2025-03-15")] == 600
    assert trough(result, 100, d("2025-02-01")) == 100
    assert math.fsum(result.values()) == 1800


def test_pending_debit_uses_settlement_date_once():
    rows = [event("pending", "Pending merchant debit", "2025-01-30", 125,
                  status="pending", settlement_date=d("2025-02-05"))]
    result = ledger(rows)
    assert result[d("2025-02-05")] == -125
    assert sum(value != 0 for value in result.values()) == 1
    contribution, = resolve(rows).contributions
    assert contribution.source == "special:PENDING_DEBIT"
    assert contribution.source_event_ids == ("pending",)


def test_pending_credit_is_dropped_even_when_linked():
    rows = [event("purchase", "Purchase awaiting refund", "2025-02-03", 125),
            event("refund", "Pending merchant refund", "2025-02-05", 125,
                  status="pending", event_type="refund", direction="credit", linked_event_id="purchase")]
    result = ledger(rows)
    assert result[d("2025-02-03")] == -125
    assert result[d("2025-02-05")] == 0
    assert dropped(resolve(rows))["refund"] == "PENDING_CREDIT"


def test_failed_plus_scheduled_retry_counts_retry_only():
    rows = [event("failed", "Failed bill payment attempt", "2025-02-02", 125,
                  status="failed", category="utilities"),
            event("retry", "Scheduled bill payment retry", "2025-02-03", 125,
                  status="scheduled", category="utilities", linked_event_id="failed",
                  settlement_date=d("2025-02-07"))]
    result = ledger(rows)
    assert result[d("2025-02-02")] == 0
    assert result[d("2025-02-07")] == -125
    assert math.fsum(result.values()) == -125
    resolved = resolve(rows)
    assert dropped(resolved)["failed"] == "FAILED_WITH_RETRY"
    assert resolved.contributions[0].source == "special:SCHEDULED_RETRY"
    assert resolved.contributions[0].source_event_ids == ("failed", "retry")


def test_failed_without_retry_never_enters_ledger():
    rows = [event("failed", "Failed utility debit", "2025-02-05", status="failed")]
    assert not any(ledger(rows).values())
    assert dropped(resolve(rows)) == {"failed": "FAILED"}


@pytest.mark.parametrize("status", ["cancelled", "pending"])
def test_authorization_and_settled_purchase_count_purchase_once(status):
    rows = [event("auth", "Card authorization", "2025-02-02", 125, status=status),
            event("purchase", "Settled card purchase", "2025-02-05", 130, linked_event_id="auth")]
    result = ledger(rows)
    assert result[d("2025-02-02")] == 0
    assert result[d("2025-02-05")] == -130
    assert math.fsum(result.values()) == -130
    assert dropped(resolve(rows))["auth"] in {"CANCELLED", "AUTHORIZATION_REPLACED"}


def test_reversal_pair_nets_to_zero_at_actual_dates():
    rows = [event("charge", "Card charge later reversed", "2025-02-03", 125),
            event("reversal", "Settled card charge reversal", "2025-02-05", 125,
                  event_type="refund", direction="credit", linked_event_id="charge")]
    result = ledger(rows)
    assert result[d("2025-02-03")] == -125
    assert result[d("2025-02-05")] == 125
    assert math.fsum(result.values()) == 0
    assert len(resolve(rows).contributions) == 2


def test_reversal_of_historical_charge_does_not_replay_charge():
    rows = [event("charge", "Card charge later reversed", "2025-01-30", 125),
            event("reversal", "Settled card charge reversal", "2025-02-05", 125,
                  event_type="refund", direction="credit", linked_event_id="charge")]
    assert math.fsum(ledger(rows).values()) == 125
    assert dropped(resolve(rows))["charge"] == "OUTSIDE_WINDOW"


@pytest.mark.parametrize("original_date,expected", [("2025-02-03", -125), ("2025-01-30", 0)])
def test_duplicate_charge_counted_once_even_if_original_is_historical(original_date, expected):
    rows = [event("original", "Original card charge", original_date, 125),
            event("duplicate", "Possible duplicate card charge", "2025-02-05", 125,
                  status="pending", linked_event_id="original")]
    assert math.fsum(ledger(rows).values()) == expected
    assert dropped(resolve(rows))["duplicate"] == "DUPLICATE_CHARGE"
    assert reconstruct_expense_streams(rows, d("2025-02-10")) == []


def test_linked_duplicate_chain_does_not_count_multiple_obligations():
    rows = [event("original", "Original card charge", "2025-02-01", 125),
            event("copy-one", "Possible duplicate card charge", "2025-02-02", 125,
                  linked_event_id="original", status="pending"),
            event("copy-two", "Possible duplicate card charge", "2025-02-03", 125,
                  linked_event_id="copy-one", status="pending")]
    assert math.fsum(ledger(rows).values()) == -125
    assert len(resolve(rows).contributions) == 1


@pytest.mark.parametrize("status,direction", [("unrealized", "credit"), ("unrealized", "non_cash"),
                                             ("settled", "non_cash"), ("scheduled", "non_cash")])
def test_non_cash_and_unrealized_never_enter(status, direction):
    rows = [event("valuation", "Current portfolio valuation", "2025-02-05", 9000,
                  event_type="investment_valuation", status=status, direction=direction)]
    assert not any(ledger(rows).values())
    assert dropped(resolve(rows)) == {"valuation": "NON_CASH"}


def test_reimbursement_is_cash_but_not_recurring_income():
    rows = [event("work", "Reimbursable work expense", "2025-02-03", 125, category="work_expense"),
            event("reimbursement", "Employer expense reimbursement", "2025-02-20", 125,
                  category="salary", direction="credit", event_type="refund", linked_event_id="work")]
    result = ledger(rows)
    assert result[d("2025-02-03")] == -125
    assert result[d("2025-02-20")] == 125
    assert math.fsum(result.values()) == 0
    assert len(resolve(rows).contributions) == 2
    assert project_salary_streams(rows, d("2025-03-01"), home_currency="USD") == []


def test_investment_purchase_and_sale_are_independent_settled_cash():
    rows = [event("buy", "Investment contribution", "2025-02-03", 125,
                  event_type="investment_purchase"),
            event("sell", "Investment sale proceeds", "2025-02-20", 175,
                  event_type="investment_sale", direction="credit", linked_event_id="buy")]
    assert math.fsum(ledger(rows).values()) == 50


def test_single_historical_expense_does_not_create_recurrence():
    rows = [event("only", "Rent", "2025-01-02", 400, category="rent")]
    assert reconstruct_expense_streams(rows, d("2025-02-01")) == []
    assert not any(ledger(rows).values())


def test_expenses_group_by_category_use_mean_median_and_newest_metadata():
    rows = [event("old", "Old landlord", "2024-11-15", 300, category="rent"),
            event("middle", "Changed description", "2024-12-15", 400, category="rent"),
            event("new", "Current landlord", "2025-01-15", 800, category="rent",
                  flexibility="stoppable", minimum_allowed_amount=50),
            payroll()]
    stream, = reconstruct_expense_streams(reversed(rows), d("2025-02-01"))
    assert isinstance(stream, Stream)
    assert stream.stream_id == "expense:rent" and stream.category == "rent"
    assert stream.description == "Current landlord"
    assert stream.level == 500
    assert stream.cadence_days == 30.5
    assert stream.representative_event_id == "new"
    assert stream.last_date == d("2025-01-15")
    assert stream.flexibility == "stoppable" and stream.minimum_allowed_amount == 50
    assert stream.source_event_ids == ("middle", "new", "old")


def test_partition_marks_referenced_parents_and_all_special_types():
    rows = [event("ordinary", "Rent", "2025-01-02"),
            event("parent", "Purchase awaiting refund", "2025-01-03"),
            event("child", "Pending merchant refund", "2025-02-03",
                  status="pending", direction="credit", linked_event_id="parent")]
    rows += [event(kind, kind, "2025-01-03", event_type=kind) for kind in
             ("refund", "investment_purchase", "investment_sale", "investment_valuation")]
    partition = partition_events(rows)
    assert [e.event_id for e in partition.ordinary] == ["ordinary"]
    assert {e.event_id for e in partition.special} == {e.event_id for e in rows} - {"ordinary"}


def test_all_special_debits_are_excluded_from_recurring_expenses():
    rows = [event("rent-old", "Rent", "2024-12-02", 400, category="rent"),
            event("rent-new", "Rent", "2025-01-02", 400, category="rent"),
            event("failed", "Failed payment", "2025-01-20", 8000, status="failed", category="rent"),
            event("linked", "Linked debit", "2025-01-25", 9000, linked_event_id="failed", category="rent")]
    stream, = reconstruct_expense_streams(rows, d("2025-02-01"))
    assert stream.level == 400
    assert stream.source_event_ids == ("rent-new", "rent-old")


def test_stop_and_reduce_are_future_only_and_scenarios_are_immutable():
    rows = [event("rent-old", "Rent", "2024-12-15", 400, category="rent"),
            event("rent-new", "Rent", "2025-01-15", 400, category="rent"), payroll()]
    user = case(rows)
    original = copy.deepcopy(user)
    baseline = build_ledger_trace(user, d("2025-02-01"))
    snapshot = copy.deepcopy(baseline)
    overlays = {"rent-new": 200}
    reduced = build_ledger(user, d("2025-02-01"), changes=overlays)
    stopped = build_ledger(user, d("2025-02-01"), changes={"rent-new": None})
    assert reduced[d("2025-02-15")] == 800
    assert stopped[d("2025-02-15")] == 1000
    assert baseline.ledger[d("2025-02-15")] == 600
    assert baseline == snapshot
    assert user == original
    assert overlays == {"rent-new": 200}
    assert build_ledger(user, d("2025-02-01")) == baseline.ledger
    assert reduced is not baseline.ledger and stopped is not reduced


def test_overlay_does_not_rewrite_known_settled_cash_on_request_date():
    rows = [event("old", "Rent", "2025-01-01", 400, category="rent"),
            event("today", "Rent", "2025-02-01", 400, category="rent")]
    result = ledger(rows, changes={"today": None})
    assert result[d("2025-02-01")] == -400
    assert result[d("2025-03-01")] == 0
    assert rows[1].amount == 400


def test_request_day_and_exact_ninety_day_boundary_are_included():
    start = d("2025-02-01")
    rows = [event("start", "Scheduled debit", str(start), 10, status="scheduled"),
            event("end", "Scheduled debit", str(start + timedelta(days=90)), 20, status="scheduled"),
            event("outside", "Scheduled debit", str(start + timedelta(days=91)), 40, status="scheduled")]
    result = ledger(rows)
    assert len(result) == 91
    assert list(result) == [start + timedelta(days=i) for i in range(91)]
    assert next(iter(result)) == start
    assert next(reversed(result)) == start + timedelta(days=90)
    assert result[start] == -10 and result[start + timedelta(days=90)] == -20
    assert math.fsum(result.values()) == -30
    assert dropped(resolve(rows))["outside"] == "OUTSIDE_WINDOW"


def test_request_date_settled_cash_counts_without_replaying_history():
    rows = [event("before", "Known debit", "2025-01-31", 900),
            event("today", "Known debit", "2025-02-01", 100)]
    assert ledger(rows, horizon=0) == {d("2025-02-01"): -100}


def test_expense_projection_on_request_date_is_included():
    rows = [event("old", "Rent", "2024-12-01", 400, category="rent"),
            event("new", "Rent", "2025-01-01", 400, category="rent")]
    assert ledger(rows, horizon=0) == {d("2025-02-01"): -400}


def test_ledger_uses_salary_output_and_keeps_second_household_independent():
    rows = [payroll(description="Primary household salary", amount=1000),
            payroll("second", "Second household income", "2025-01-20", 600)]
    actual = ledger(rows)
    expected = project_salary_streams(rows, d("2025-02-01"), home_currency="USD")
    assert len(expected) == 2
    for stream in expected:
        for row in stream.occurrences:
            assert actual[row.date] == row.amount
    assert math.fsum(actual.values()) == 4800


def test_next_salary_not_counted_again_as_special_event():
    rows = [payroll(), payroll("next", "Next confirmed salary", "2025-02-15", 1500, status="scheduled")]
    trace = build_ledger_trace(case(rows), d("2025-02-01"))
    assert trace.ledger[d("2025-02-15")] == 1500
    assert trace.ledger[d("2025-03-15")] == 1500
    assert len([row for row in trace.contributions if row.date == d("2025-02-15")]) == 1
    assert dropped(trace.special_resolution)["next"] == "SALARY_PROJECTION"


def test_terminated_salary_cannot_leak_through_scheduled_special_rule():
    rows = [payroll(on_date="2024-12-15"),
            payroll("final", "Final employer payroll", "2025-01-15"),
            payroll("next", "Next confirmed salary", "2025-02-15", status="scheduled")]
    assert not any(ledger(rows).values())


@pytest.mark.parametrize("scheduled", [False, True])
def test_salary_projection_includes_request_date_without_changing_step_three(scheduled):
    rows = [payroll()]
    if scheduled:
        rows.append(payroll("next", "Next confirmed salary", "2025-02-15", 1500, status="scheduled"))
    assert ledger(rows, request="2025-02-15", horizon=0) == {
        d("2025-02-15"): 1500 if scheduled else 1000,
    }


def test_salary_settled_today_is_counted_once_at_new_level():
    rows = [payroll(), payroll("today", on_date="2025-02-15", amount=1800)]
    result = ledger(rows, request="2025-02-15")
    assert result[d("2025-02-15")] == 1800
    assert result[d("2025-03-15")] == 1800


def test_terminal_settled_today_does_not_revive_old_payroll_at_boundary():
    rows = [payroll(), payroll("final", "Final employer payroll", "2025-02-15", 1400)]
    result = ledger(rows, request="2025-02-15")
    assert result[d("2025-02-15")] == 1400
    assert math.fsum(result.values()) == 1400


@pytest.fixture
def synthetic_fx(monkeypatch):
    monkeypatch.setattr(fx, "_rate_table", {})
    monkeypatch.setattr(fx, "_conversion_log", [])
    fx.init_rates([ExchangeRate(d("2025-01-15"), "USD", "INR", 80),
                   ExchangeRate(d("2025-02-15"), "USD", "INR", 81),
                   ExchangeRate(d("2025-03-15"), "USD", "INR", 82)])


def test_salary_expenses_and_special_cash_use_occurrence_date_fx(synthetic_fx):
    rows = [payroll(amount=100),
            event("old", "Rent", "2024-12-15", 10, category="rent"),
            event("new", "Rent", "2025-01-15", 10, category="rent"),
            event("pending", "Pending debit", "2025-02-10", 5, status="pending",
                  settlement_date=d("2025-02-15"))]
    result = ledger(rows, horizon=45, currency="INR")
    assert result[d("2025-02-15")] == (100 - 10 - 5) * 81
    assert result[d("2025-03-15")] == (100 - 10) * 82
    log = fx.get_conversion_log()
    assert any(row["amount"] == 5 and row["on_date"] == "2025-02-15" for row in log)
    assert any(row["amount"] == 100 and row["on_date"] == "2025-03-15" for row in log)


def test_mixed_currency_expenses_are_normalized_before_averaging(synthetic_fx):
    rows = [event("usd", "Rent A", "2024-12-15", 10, category="rent"),
            event("inr", "Rent B", "2025-01-15", 1600, currency="INR", category="rent")]
    stream, = reconstruct_expense_streams(rows, d("2025-02-01"))
    assert stream.currency == "INR"
    assert stream.level == 1200


def test_blank_historical_amount_is_excluded_from_mean_not_zeroed(caplog):
    rows = [event("a", "Rent", "2024-11-15", 400),
            event("b", "Rent", "2024-12-15", None),
            event("c", "Rent", "2025-01-15", 800)]
    stream, = reconstruct_expense_streams(rows, d("2025-02-01"))
    assert stream.level == 600
    assert stream.amount_event_ids == ("a", "c")
    assert "blank historical amount" in caplog.text
    assert rows[1].amount is None


@pytest.mark.parametrize("changes,reason", [({"amount": None}, "MISSING_AMOUNT"),
                                          ({"amount": float("nan")}, "INVALID_AMOUNT"),
                                          ({"settlement_date": None}, "MISSING_DATE"),
                                          ({"status": "unknown"}, "UNKNOWN_STATE")])
def test_unresolved_cash_is_audited_and_never_silently_zeroed(changes, reason):
    row = replace(event("unresolved", "Pending debit", "2025-02-05", status="pending"), **changes)
    resolution = resolve([row])
    assert resolution.unresolved_events[0].reason == reason
    with pytest.raises(UnresolvedCashEvents, match=reason):
        ledger([row])


def test_special_resolution_accounts_for_every_event_once():
    rows = [event("pending", "Pending debit", "2025-02-05", status="pending"),
            event("credit", "Pending credit", "2025-02-05", status="pending", direction="credit"),
            event("cancel", "Cancelled authorization", "2025-02-05", status="cancelled"),
            event("missing", "Scheduled debit", "2025-02-05", None, status="scheduled"),
            event("history", "Investment contribution", "2024-12-01", event_type="investment_purchase")]
    result = resolve(rows)
    accounted = [row.event_id for group in
                 (result.contributions, result.dropped_events, result.unresolved_events) for row in group]
    assert sorted(accounted) == sorted(row.event_id for row in rows)
    assert len(accounted) == len(set(accounted))


def test_input_order_and_same_day_sum_are_deterministic():
    rows = [payroll(),
            event("pending", "Pending debit", "2025-02-15", 125, status="pending"),
            event("cancelled", "Cancelled debit", "2025-02-15", status="cancelled"),
            event("scheduled", "Scheduled expense", "2025-02-15", 225, status="scheduled")]
    snapshot = copy.deepcopy(rows)
    expected = ledger(rows)
    for ordering in permutations(rows):
        assert ledger(ordering) == expected
    assert expected[d("2025-02-15")] == 650
    assert rows == snapshot


def test_every_ledger_value_equals_sum_of_same_day_trace():
    rows = [payroll(), event("pending", "Pending debit", "2025-02-15", 125, status="pending")]
    trace = build_ledger_trace(case(rows), d("2025-02-01"))
    assert len(trace.ledger) == 91
    for on_date, net in trace.ledger.items():
        assert net == math.fsum(row.amount for row in trace.contributions if row.date == on_date)
    assert all(row.source_event_ids and row.currency == "USD" for row in trace.contributions)


@pytest.mark.parametrize("gap", [0, -1, float("nan"), float("inf"), True])
def test_invalid_gap_rejected(gap):
    with pytest.raises(ValueError, match="gap"):
        occurrences(d("2025-01-01"), gap, d("2025-02-01"))


@pytest.mark.parametrize("horizon", [-1, 2.5, True])
def test_invalid_horizon_rejected(horizon):
    with pytest.raises(ValueError, match="horizon"):
        ledger([], horizon=horizon)


def test_unknown_change_representative_rejected():
    with pytest.raises(ValueError, match="representative"):
        ledger([], changes={"absent": None})


def test_mixed_users_and_duplicate_event_ids_rejected():
    one = event("one", "Expense", "2025-01-01")
    with pytest.raises(ValueError, match="mismatched users"):
        ledger([replace(one, user_id="another-user")])
    with pytest.raises(ValueError, match="Duplicate event"):
        ledger([one, one])


def test_trough_nets_extra_debit_with_daily_movement_without_mutation():
    movements = {d("2025-02-01"): 600, d("2025-02-02"): -50}
    snapshot = dict(movements)
    assert trough(movements, 100, d("2025-02-01"), {d("2025-02-01"): 500}) == 150
    assert movements == snapshot


def test_trough_uses_completed_day_not_opening_or_intraday_balance():
    assert trough({d("2025-02-01"): 600}, 100, d("2025-02-01")) == 700


def test_trough_preserves_prior_daily_movements_for_later_inspection():
    movements = {d("2025-02-01"): 600, d("2025-02-02"): -50}
    assert trough(movements, 100, d("2025-02-02")) == 650

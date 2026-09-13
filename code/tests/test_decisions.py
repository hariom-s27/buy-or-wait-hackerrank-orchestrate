"""Synthetic Step 6 eligibility, ranking, explanations and pipeline tests."""
from __future__ import annotations

import copy
import csv
import re
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from code import main
from code.config import OUTPUT_COLUMNS
from code.decide import plans
from code.decide.explain import build_facts, explain
from code.decide.plans import enumerate_plans, payment_debits, schedule_is_safe
from code.decide.rank import affordability_status, plan_sort_key, rank_plans, select_plan
from code.domain.models import CandidatePlan, Event, PaymentOption, Profile, Request, RequestCase
from code.forecast.capacity import amount_safe_to_pay, earliest_date_for_full_payment
from code.forecast.ledger import trough
from code.output.validator import ValidationError


TODAY = date(2025, 2, 1)
LATER = TODAY + timedelta(days=10)


def make_case(*, balance=1100.0, minimum=100.0, amount=1000.0,
              methods=("full_payment", "partial_payment", "installments"),
              partial=True, deadline=None, limit=3, events=(), options=()):
    return RequestCase(
        Request("request_synthetic", "user_synthetic", TODAY, "purchase", amount,
                deadline or TODAY + timedelta(days=90), partial, "Synthetic purchase"),
        Profile("user_synthetic", "USD", balance, minimum, [], [], [], [], list(methods), limit),
        list(events), list(options), [], [],
    )


def make_option(*, option_id="option_a", count=2, amount=550.0, total=1100.0,
                start=TODAY, gap=30, method="installments", request_id="request_synthetic"):
    return PaymentOption(option_id, request_id, method, amount, count, start, gap, 100.0, total)


def make_ledger(movements=None):
    movements = movements or {}
    return {TODAY + timedelta(days=i): movements.get(TODAY + timedelta(days=i), 0.0)
            for i in range(91)}


def make_event(*, amount=1000.0, status="settled", direction="credit",
               description="Payroll credit", category="salary", on_date=date(2025, 1, 11)):
    return Event("event_synthetic", "user_synthetic", "income" if direction == "credit" else "expense",
                 description, category, direction, amount, "USD", on_date, on_date, status,
                 None, None, None)


def methods(candidates):
    return {plan.method for plan in candidates}


def test_full_payment_today_requires_acceptance_and_earliest_today():
    ledger = make_ledger()
    assert methods(enumerate_plans(make_case(), ledger, 1000, TODAY)) == {"full_payment"}
    assert enumerate_plans(make_case(methods=("installments",)), ledger, 1000, TODAY) == []
    assert enumerate_plans(make_case(), ledger, 1000, None) == []
    assert methods(enumerate_plans(make_case(), ledger, 1000, LATER)) == {"wait"}


@pytest.mark.parametrize("earliest", [None, TODAY - timedelta(days=1), TODAY + timedelta(days=91)])
def test_wait_requires_valid_earliest(earliest):
    assert enumerate_plans(make_case(), make_ledger(), 1000, earliest) == []


def test_wait_requires_full_payment_acceptance_and_meets_deadline():
    assert enumerate_plans(make_case(methods=("wait",)), make_ledger(), 1000, LATER) == []
    assert enumerate_plans(make_case(deadline=LATER - timedelta(days=1)), make_ledger(), 1000, LATER) == []
    result = enumerate_plans(make_case(deadline=LATER), make_ledger(), 1000, LATER)
    assert result[0].schedule == [(LATER, 1000)]


def test_partial_has_exactly_two_payments_with_exact_cent_remainder():
    case = make_case(balance=433.33, amount=1000.01)
    ledger = make_ledger({LATER: 1000})
    result = enumerate_plans(case, ledger, 333.33, LATER)
    plan = next(plan for plan in result if plan.method == "partial_payment")
    assert plan.schedule == [(TODAY, 333.33), (LATER, 666.68)]
    assert sum(Decimal(str(amount)) for _, amount in plan.schedule) == Decimal("1000.01")
    assert plan.total_payable == case.request.requested_amount
    assert select_plan(result, case.request.desired_completion_date) is plan


@pytest.mark.parametrize("case_kwargs,safe,earliest", [
    ({"partial": False}, 300, LATER),
    ({"methods": ("full_payment",)}, 300, LATER),
    ({}, 0, LATER), ({}, 1000, LATER), ({}, -1, LATER), ({}, 300, None),
    ({}, 300, TODAY), ({"deadline": TODAY}, 300, LATER),
])
def test_partial_eligibility_gates(case_kwargs, safe, earliest):
    result = enumerate_plans(make_case(**case_kwargs), make_ledger({LATER: 1000}), safe, earliest)
    assert "partial_payment" not in methods(result)


def test_rounded_up_partial_is_rejected_without_a_safety_tolerance():
    case = make_case(balance=433.336)
    ledger = make_ledger({LATER: 1000})
    safe = amount_safe_to_pay(ledger, 433.336, 100, TODAY, 1000)
    assert round(safe, 2) == 333.34
    assert methods(enumerate_plans(case, ledger, safe, LATER)) == {"wait"}


def test_rounded_down_partial_is_safe_and_not_zero_or_full_after_rounding():
    ledger = make_ledger({LATER: 1000})
    result = enumerate_plans(make_case(balance=433.334), ledger, 333.334, LATER)
    plan = next(plan for plan in result if plan.method == "partial_payment")
    assert plan.schedule[0] == (TODAY, 333.33)
    assert trough(ledger, 433.334, TODAY, payment_debits(plan.schedule)) >= 100
    assert "partial_payment" not in methods(enumerate_plans(make_case(), ledger, 0.004, LATER))
    assert "partial_payment" not in methods(enumerate_plans(make_case(), ledger, 999.999, LATER))


def test_installment_dates_are_exact_fixed_gaps_not_calendar_monthly():
    option = make_option(start=date(2025, 2, 28), gap=30, count=3, amount=350, total=1050)
    case = make_case(balance=2000, options=(option,))
    plan = enumerate_plans(case, make_ledger(), 1000, None)[0]
    assert plan.schedule == [(date(2025, 2, 28), 350), (date(2025, 3, 30), 350),
                             (date(2025, 4, 29), 350)]
    assert plan.total_payable == 1050
    assert plan.payment_option_id == option.payment_option_id


@pytest.mark.parametrize("case_kwargs,option_kwargs", [
    ({"methods": ("full_payment",)}, {}), ({"limit": None}, {}),
    ({"limit": 1}, {}), ({}, {"count": 4}), ({}, {"count": 0}),
    ({}, {"gap": None}), ({}, {"gap": 0}), ({}, {"gap": -1}),
    ({}, {"method": "full_payment"}), ({}, {"request_id": "request_unrelated"}),
    ({"deadline": TODAY + timedelta(days=29)}, {}),
    ({}, {"start": TODAY - timedelta(days=1)}),
    ({"deadline": TODAY + timedelta(days=100)}, {"start": TODAY + timedelta(days=61)}),
    ({}, {"amount": 10000}), ({}, {"amount": 0}), ({}, {"amount": 550.001}),
])
def test_ineligible_installment_is_never_adjusted(case_kwargs, option_kwargs):
    case = make_case(balance=2000, options=(make_option(**option_kwargs),), **case_kwargs)
    before = copy.deepcopy(case)
    assert enumerate_plans(case, make_ledger(), 1000, None) == []
    assert case == before


def test_installment_on_deadline_and_forecast_boundary_is_included():
    option = make_option(start=TODAY + timedelta(days=60))
    plan = enumerate_plans(make_case(balance=2000, options=(option,)), make_ledger(), 1000, None)[0]
    assert plan.schedule[-1][0] == TODAY + timedelta(days=90)


def test_single_payment_supplied_installment_does_not_invent_frequency():
    option = make_option(count=1, gap=None, amount=1050, total=1050)
    result = enumerate_plans(make_case(balance=2000, options=(option,)), make_ledger(), 1000, None)
    assert result[0].schedule == [(TODAY, 1050)]


@pytest.mark.parametrize("earliest", [TODAY, LATER])
def test_even_claimed_capacity_cannot_bypass_safety(earliest):
    case = make_case(balance=200)
    assert enumerate_plans(case, make_ledger(), 1000, earliest) == []


def test_every_candidate_uses_the_same_whole_window_safety_check(monkeypatch):
    calls = []
    original = plans.trough

    def record(ledger, balance, from_date, extra_payments):
        calls.append((from_date, extra_payments))
        return original(ledger, balance, from_date, extra_payments)

    monkeypatch.setattr(plans, "trough", record)
    ledger = make_ledger({LATER: 2000})
    case = make_case(balance=400, options=(make_option(amount=200, count=3, total=600),))
    candidates = enumerate_plans(case, ledger, 300, LATER)
    assert methods(candidates) == {"wait", "partial_payment", "installments"}
    assert len(calls) == 3
    assert all(from_date == TODAY for from_date, _ in calls)
    calls.clear()
    enumerate_plans(make_case(), make_ledger(), 1000, TODAY)
    assert calls == [(TODAY, {TODAY: 1000})]


def test_prior_breach_is_not_hidden_by_later_payment_and_salary():
    ledger = make_ledger({TODAY: -50, LATER: 2000})
    assert not schedule_is_safe(make_case(balance=100), ledger, [(LATER, 1000)])


def test_same_day_payment_debits_are_aggregated_not_overwritten():
    assert payment_debits([(TODAY, 400), (TODAY, 500)]) == {TODAY: 900}
    assert not schedule_is_safe(make_case(balance=800), make_ledger(), [(TODAY, 400), (TODAY, 500)])


def test_payment_nets_with_same_day_salary_and_expenses():
    assert schedule_is_safe(make_case(balance=100), make_ledger({TODAY: 600}), [(TODAY, 600)])


def make_plan(*, method="installments", total=1100, start=TODAY, count=2,
              option_id="option_a", changes=None, safe=True):
    return CandidatePlan(method, option_id,
                         [(start + timedelta(days=30 * k), total / count) for k in range(count)],
                         total, safe, spending_changes=changes)


def test_rank_key_is_exact_official_order():
    plan = make_plan()
    assert plan_sort_key(plan) == (False, 1100, TODAY, 2, "option_a")


@pytest.mark.parametrize("better,worse", [
    (make_plan(total=2000), make_plan(total=500, changes=["stop:event_synthetic"])),
    (make_plan(total=1000, start=LATER), make_plan(total=1100)),
    (make_plan(count=3), make_plan(start=LATER, count=1)),
    (make_plan(count=1), make_plan(count=2)),
    (make_plan(option_id="option_a"), make_plan(option_id="option_b")),
])
def test_each_ranking_priority(better, worse):
    assert select_plan([worse, better], TODAY + timedelta(days=90)) is better


def test_deadline_is_filter_and_not_completion_date_tiebreak():
    cheap_later = make_plan(total=1000, start=LATER)
    expensive_early = make_plan(total=1100)
    late = make_plan(total=500, start=TODAY + timedelta(days=70))
    unsafe = make_plan(total=100, safe=False)
    empty = replace(cheap_later, schedule=[])
    assert rank_plans([expensive_early, late, unsafe, empty, cheap_later],
                      TODAY + timedelta(days=90)) == [cheap_later, expensive_early]


def test_official_option_total_not_sum_of_rounded_payments_drives_ranking():
    cheaper = make_option(option_id="option_z", amount=366.67, count=3, total=1100.00)
    other = make_option(option_id="option_a", amount=366.67, count=3, total=1100.01)
    candidates = enumerate_plans(make_case(balance=2000, options=(other, cheaper)), make_ledger(), 1000, None)
    assert select_plan(candidates, TODAY + timedelta(days=90)).payment_option_id == "option_z"


def test_cost_beats_installments_starting_earlier_than_wait():
    case = make_case(balance=500, partial=False, options=(make_option(amount=350, count=3, total=1050),))
    ledger = make_ledger({LATER: 2000})
    candidates = enumerate_plans(case, ledger, 400, LATER)
    assert methods(candidates) == {"installments", "wait"}
    assert select_plan(candidates, case.request.desired_completion_date).method == "wait"


@pytest.mark.parametrize("method,start,changes,expected", [
    ("full_payment", TODAY, None, "affordable_now"),
    ("wait", LATER, None, "affordable_later"),
    ("partial_payment", TODAY, None, "affordable_with_plan"),
    ("installments", TODAY, None, "affordable_with_plan"),
    ("full_payment", TODAY, ["stop:event_synthetic"], "affordable_with_plan"),
    ("wait", LATER, ["stop:event_synthetic"], "affordable_with_plan"),
])
def test_status_mapping(method, start, changes, expected):
    assert affordability_status(make_plan(method=method, start=start, changes=changes), TODAY) == expected


def test_no_winner_and_unsafe_status():
    assert select_plan([], TODAY) is None
    assert affordability_status(None, TODAY) == "not_affordable"
    with pytest.raises(ValueError, match="unsafe"):
        affordability_status(make_plan(safe=False), TODAY)


def test_enumeration_ranking_and_fact_building_are_immutable():
    case = make_case(balance=2000, options=(make_option(),))
    ledger = make_ledger()
    before = copy.deepcopy((case, ledger))
    result = enumerate_plans(case, ledger, 1000, TODAY)
    result_before = copy.deepcopy(result)
    winner = select_plan(result, case.request.desired_completion_date)
    build_facts(case, winner)
    assert (case, ledger) == before
    assert result == result_before


@pytest.mark.parametrize("method", ["full_payment", "wait", "partial_payment", "installments", None])
def test_explanations_are_at_most_two_sentences_with_no_newlines(method):
    case = make_case()
    plan = make_plan(method=method, start=LATER if method == "wait" else TODAY) if method else None
    facts = build_facts(case, plan)
    before = copy.deepcopy(facts)
    text = explain(facts)
    assert len(re.split(r"(?<=[.!?])\s+", text)) == 2
    assert "\n" not in text
    assert "USD 100 minimum" in text or "USD 100 available" in text
    assert facts == before
    assert explain(facts) == text


def test_explanation_amounts_dates_counts_and_horizon_come_from_fact_dict():
    facts = build_facts(make_case(), make_plan())
    facts.update(currency="EUR", minimum=1234.56, horizon_days=47, payment_count=7,
                 schedule=((date(2032, 11, 19), 7777.25),))
    assert explain(facts) == (
        "Use 7 installments of EUR 7,777.25, starting 2032-11-19. "
        "This leaves at least EUR 1,234.56 available over the next 47 days."
    )


def test_partial_template_uses_schedule_not_requested_amount_for_first_payment():
    plan = make_plan(method="partial_payment")
    plan.schedule = [(TODAY, 123.45), (LATER, 876.55)]
    assert explain(build_facts(make_case(), plan)) == (
        "Pay USD 123.45 today and the remaining USD 876.55 on 2025-02-11. "
        "This completes the full request and keeps the USD 100 minimum protected."
    )


def test_spending_change_template_does_not_claim_unchanged_baseline_guarantee():
    plan = make_plan(method="wait", changes=["stop:event_synthetic"])
    text = explain(build_facts(make_case(), plan))
    assert text.startswith("With the listed spending changes, pay ")
    assert "Paying earlier" not in text


def test_full_pipeline_funded_installments_only_is_with_plan():
    case = make_case(balance=2000, methods=("installments",), options=(make_option(),))
    row = main.decision_row(main.decide_case(case))
    assert list(row) == OUTPUT_COLUMNS
    assert row["affordability_status"] == "affordable_with_plan"
    assert row["recommended_payment_method"] == "installments"
    assert row["earliest_date_for_full_payment"] == TODAY.isoformat()
    assert row["amount_safe_to_pay"] == "1000"


def test_full_pipeline_not_affordable_retains_positive_capacity():
    row = main.decision_row(main.decide_case(make_case(balance=600, methods=())))
    assert row["amount_safe_to_pay"] == "500"
    assert row["affordability_status"] == "not_affordable"
    assert row["recommended_payment_method"] == "not_recommended"
    assert row["payment_plan"] == "none"
    assert row["earliest_date_for_full_payment"] == ""
    assert row["spending_changes_needed"] == "none"


def test_full_pipeline_salary_capacity_and_partial_payment():
    case = make_case(balance=400, events=(make_event(),))
    decision = main.decide_case(case)
    assert decision.recommended_payment_method == "partial_payment"
    assert decision.payment_plan == "2025-02-01:300|2025-02-11:700"
    assert decision.earliest_date_for_full_payment == "2025-02-11"


def test_pipeline_capacity_includes_confirmed_special_credit_dates():
    event = make_event(status="scheduled", description="Investment sale proceeds", category="investment",
                       on_date=LATER)
    decision = main.decide_case(make_case(balance=100, events=(event,)))
    assert decision.recommended_payment_method == "wait"
    assert decision.earliest_date_for_full_payment == LATER.isoformat()


def test_incomplete_future_debit_fails_closed_with_honest_explanation_and_audit(caplog):
    event = make_event(status="pending", direction="debit", amount=None,
                       description="Invoice payable", category="bills", on_date=LATER)
    decision = main.decide_case(make_case(events=(event,)))
    assert decision.affordability_status == "not_affordable"
    assert decision.amount_safe_to_pay == 0
    assert decision.payment_plan == "none"
    assert decision.earliest_date_for_full_payment == ""
    assert "Unresolved cash-flow details prevent verification" in decision.decision_explanation
    assert "event_synthetic" in caplog.text
    assert "MISSING_AMOUNT" in caplog.text


def test_unexpected_pipeline_errors_are_not_hidden(monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("unexpected forecast error")
    monkeypatch.setattr(main, "build_ledger_trace", broken)
    with pytest.raises(ValueError, match="unexpected forecast error"):
        main.decide_case(make_case())


def configure_main(monkeypatch, tmp_path):
    case = make_case()
    data = SimpleNamespace(requests=[case.request], exchange_rates=[], requests_df=object(),
                           options_df=object(), profiles_df=object(), events_df=object())
    monkeypatch.setattr(main, "load_and_index", lambda: (data, None))
    monkeypatch.setattr(main, "build_request_case", lambda request_id: case)
    monkeypatch.setattr(main.fx, "init_rates", lambda rates: None)
    monkeypatch.setattr(main, "OUTPUT_PATH", tmp_path / "output.csv")
    return data


def test_main_validates_before_writing_and_preserves_first_backup(monkeypatch, tmp_path, capsys):
    data = configure_main(monkeypatch, tmp_path)
    validated = []

    def validate(rows, *frames):
        assert frames == (data.requests_df, data.options_df, data.profiles_df, data.events_df)
        validated.append(copy.deepcopy(rows))
        return []

    monkeypatch.setattr(main, "validate_rows", validate)
    main.main()
    output = tmp_path / "output.csv"
    backup = tmp_path / "output_v1_deterministic.csv"
    first = output.read_bytes()
    assert first == backup.read_bytes()
    with output.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == validated[0]
    monkeypatch.setattr(main, "build_request_case", lambda request_id: make_case(balance=100))
    main.main()
    assert output.read_bytes() != first
    assert backup.read_bytes() == first
    stdout = capsys.readouterr().out
    assert "validator: 0 violations" in stdout
    assert "affordability_status:" in stdout
    assert "recommended_payment_method:" in stdout


def test_validation_failure_preserves_existing_output_and_backup(monkeypatch, tmp_path):
    configure_main(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "validate_rows", lambda *args: [])
    main.main()
    paths = [tmp_path / "output.csv", tmp_path / "output_v1_deterministic.csv"]
    before = [path.read_bytes() for path in paths]

    def reject(*args):
        raise ValidationError(["synthetic violation"])

    monkeypatch.setattr(main, "validate_rows", reject)
    with pytest.raises(ValidationError, match="synthetic violation"):
        main.main()
    assert [path.read_bytes() for path in paths] == before


def test_candidate_safety_property_across_synthetic_balances_and_flows():
    for balance in (0, 100, 400, 1000, 1100, 3000):
        for credit in (0, 500, 2000):
            for debit in (0, 300, 1500):
                ledger = make_ledger({LATER: credit, TODAY + timedelta(days=50): -debit})
                before = ledger.copy()
                case = make_case(balance=balance, options=(make_option(),))
                safe = amount_safe_to_pay(ledger, balance, 100, TODAY, 1000)
                earliest = earliest_date_for_full_payment(ledger, balance, 100, TODAY, 1000, [LATER])
                for plan in enumerate_plans(case, ledger, safe, earliest):
                    assert trough(ledger, balance, TODAY, payment_debits(plan.schedule)) >= 100
                    assert max(day for day, _ in plan.schedule) <= case.request.desired_completion_date
                assert ledger == before

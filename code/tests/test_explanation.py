"""
Unit tests for mechanical explanation consistency checker (Step 12).

Tests:
1. valid full-payment explanation passes
2. valid wait explanation passes
3. valid installment explanation passes
4. valid partial-payment explanation passes
5. wrong amount is rejected
6. wrong date is rejected
7. wrong currency is rejected
8. wait explanation saying "today" incorrectly is rejected
9. installment explanation with wrong count is rejected
10. partial explanation with wrong remainder is rejected
11. explanation referencing a date not in facts is rejected
12. explanation referencing a payment amount not in facts is rejected
13. more than 2 sentences is rejected
14. newline is rejected
15. empty explanation is rejected
16. wrong spending-change amount is rejected
17. not_recommended explanation recommending payment is rejected
18. thousands separators are handled correctly
19. decimal numbers are handled correctly
20. dates are not mistaken for monetary values
21. sample-style explanations pass
22. current output.csv explanations pass
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pandas as pd
import pytest

from code.evaluation.explanation import (
    RULE_ACTION_METHOD_MISMATCH,
    RULE_CURRENCY_MISMATCH,
    RULE_DATE_NOT_IN_FACTS,
    RULE_FORMAT_EMPTY,
    RULE_FORMAT_NEWLINE,
    RULE_FORMAT_SENTENCE_COUNT,
    RULE_INSTALLMENT_COUNT_MISMATCH,
    RULE_NOT_RECOMMENDED_RECOMMENDS_PAYMENT,
    RULE_NUMBER_NOT_IN_FACTS,
    RULE_PARTIAL_PAYMENT_MISMATCH,
    RULE_WAIT_SAYS_TODAY,
    build_fact_dict,
    inspect_output_file,
    validate_explanation,
)


def _make_sample_facts(
    *,
    request_id: str = "request_test",
    requested_amount: float = 15656000.0,
    amount_safe_to_pay: float = 15656000.0,
    minimum_balance: float = 24768300.0,
    home_currency: str = "IDR",
    request_date: str = "2025-08-03",
    deadline: str = "2025-09-01",
    earliest_date: str | None = "2025-08-03",
    payment_method: str = "full_payment",
    affordability_status: str = "affordable_now",
    payment_plan: str = "2025-08-03:15656000",
    spending_changes: str = "none",
) -> dict:
    out_row = {
        "request_id": request_id,
        "amount_safe_to_pay": str(amount_safe_to_pay),
        "affordability_status": affordability_status,
        "recommended_payment_method": payment_method,
        "payment_plan": payment_plan,
        "earliest_date_for_full_payment": earliest_date or "",
        "spending_changes_needed": spending_changes,
        "decision_explanation": "",
    }
    req_row = {
        "request_id": request_id,
        "user_id": "user_test",
        "request_date": request_date,
        "requested_amount": str(requested_amount),
        "desired_completion_date": deadline,
    }
    prof_row = {
        "user_id": "user_test",
        "home_currency": home_currency,
        "minimum_balance_to_keep": str(minimum_balance),
    }
    return build_fact_dict(out_row, req_row, prof_row)


# ---------------------------------------------------------------------------
# Tests 1-4: Valid explanations pass
# ---------------------------------------------------------------------------
def test_valid_full_payment_passes():
    """1. valid full-payment explanation passes."""
    facts = _make_sample_facts()
    exp = "Pay IDR 15,656,000 today. This leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert not violations, f"Expected 0 violations, got: {violations}"


def test_valid_wait_passes():
    """2. valid wait explanation passes."""
    facts = _make_sample_facts(
        requested_amount=1302.40,
        amount_safe_to_pay=289.23,
        minimum_balance=1100.0,
        home_currency="EUR",
        request_date="2024-05-15",
        earliest_date="2024-07-15",
        payment_method="wait",
        affordability_status="affordable_later",
        payment_plan="2024-07-15:1302.40",
    )
    exp = "Pay EUR 1,302.40 in full on 2024-07-15. Paying earlier would take the balance below the EUR 1,100 minimum."
    violations = validate_explanation(exp, facts)
    assert not violations, f"Expected 0 violations, got: {violations}"


def test_valid_installment_passes():
    """3. valid installment explanation passes."""
    facts = _make_sample_facts(
        requested_amount=47858720.0,
        amount_safe_to_pay=15952906.67,
        minimum_balance=29158400.0,
        home_currency="IDR",
        request_date="2025-08-08",
        earliest_date="2025-08-08",
        payment_method="installments",
        affordability_status="affordable_with_plan",
        payment_plan="2025-08-08:15952906.67|2025-09-08:15952906.67|2025-10-08:15952906.67",
    )
    exp = "Use 3 installments of IDR 15,952,906.67, starting 2025-08-08. This leaves at least IDR 29,158,400 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert not violations, f"Expected 0 violations, got: {violations}"


def test_valid_partial_payment_passes():
    """4. valid partial-payment explanation passes."""
    facts = _make_sample_facts(
        requested_amount=49450.0,
        amount_safe_to_pay=35318.22,
        minimum_balance=84000.0,
        home_currency="INR",
        request_date="2024-11-15",
        earliest_date="2024-12-15",
        payment_method="partial_payment",
        affordability_status="affordable_with_plan",
        payment_plan="2024-11-15:35318.22|2024-12-15:14131.78",
    )
    exp = "Pay INR 35,318.22 today and the remaining INR 14,131.78 on 2024-12-15. This completes the full request and keeps the INR 84,000 minimum protected."
    violations = validate_explanation(exp, facts)
    assert not violations, f"Expected 0 violations, got: {violations}"


# ---------------------------------------------------------------------------
# Tests 5-12: Error detection and rejection
# ---------------------------------------------------------------------------
def test_wrong_amount_rejected():
    """5. wrong amount is rejected."""
    facts = _make_sample_facts()
    # 99,999,999 is not in facts
    exp = "Pay IDR 99,999,999 today. This leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_NUMBER_NOT_IN_FACTS for v in violations)


def test_wrong_date_rejected():
    """6. wrong date is rejected."""
    facts = _make_sample_facts(
        requested_amount=1302.40,
        minimum_balance=1100.0,
        home_currency="EUR",
        request_date="2024-05-15",
        earliest_date="2024-07-15",
        payment_method="wait",
        affordability_status="affordable_later",
        payment_plan="2024-07-15:1302.40",
    )
    # 2024-08-20 is not in facts
    exp = "Pay EUR 1,302.40 in full on 2024-08-20. Paying earlier would take the balance below the EUR 1,100 minimum."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_DATE_NOT_IN_FACTS for v in violations)


def test_wrong_currency_rejected():
    """7. wrong currency is rejected."""
    facts = _make_sample_facts(home_currency="IDR")
    # USD does not match IDR
    exp = "Pay USD 15,656,000 today. This leaves at least USD 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_CURRENCY_MISMATCH for v in violations)


def test_wait_saying_today_rejected():
    """8. wait explanation saying 'today' incorrectly is rejected."""
    facts = _make_sample_facts(
        payment_method="wait",
        affordability_status="affordable_later",
        earliest_date="2025-09-15",
        payment_plan="2025-09-15:15656000",
    )
    exp = "Wait until 2025-09-15, then pay IDR 15,656,000 today. Paying earlier would take the balance below the IDR 24,768,300 minimum."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_WAIT_SAYS_TODAY for v in violations)


def test_installment_wrong_count_rejected():
    """9. installment explanation with wrong count is rejected."""
    facts = _make_sample_facts(
        payment_method="installments",
        affordability_status="affordable_with_plan",
        payment_plan="2025-08-08:5000000|2025-09-08:5000000|2025-10-08:5000000",  # 3 installments
    )
    # Explanation claims 5 installments instead of 3
    exp = "Use 5 installments of IDR 5,000,000, starting 2025-08-08. This leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert any(v.rule in (RULE_INSTALLMENT_COUNT_MISMATCH, RULE_NUMBER_NOT_IN_FACTS) for v in violations)


def test_partial_wrong_remainder_rejected():
    """10. partial explanation with wrong remainder is rejected."""
    facts = _make_sample_facts(
        requested_amount=49450.0,
        amount_safe_to_pay=35318.22,
        minimum_balance=84000.0,
        home_currency="INR",
        request_date="2024-11-15",
        earliest_date="2024-12-15",
        payment_method="partial_payment",
        affordability_status="affordable_with_plan",
        payment_plan="2024-11-15:35318.22|2024-12-15:14131.78",
    )
    # Remainder claims 20,000 instead of 14,131.78
    exp = "Pay INR 35,318.22 today and the remaining INR 20,000 on 2024-12-15. This completes the full request and keeps the INR 84,000 minimum protected."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_NUMBER_NOT_IN_FACTS for v in violations)


def test_date_not_in_facts_rejected():
    """11. explanation referencing a date not in facts is rejected."""
    facts = _make_sample_facts()
    # 2029-01-01 is completely invented
    exp = "Pay IDR 15,656,000 today by 2029-01-01. This leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_DATE_NOT_IN_FACTS for v in violations)


def test_payment_amount_not_in_facts_rejected():
    """12. explanation referencing a payment amount not in facts is rejected."""
    facts = _make_sample_facts(requested_amount=1000.0, payment_plan="2025-08-03:1000")
    exp = "Pay IDR 850 today. This leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_NUMBER_NOT_IN_FACTS for v in violations)


# ---------------------------------------------------------------------------
# Tests 13-17: Format & semantic checks
# ---------------------------------------------------------------------------
def test_more_than_two_sentences_rejected():
    """13. more than 2 sentences is rejected."""
    facts = _make_sample_facts()
    exp = "Pay IDR 15,656,000 today. This leaves at least IDR 24,768,300 available over the next 90 days. Please verify with your bank."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_FORMAT_SENTENCE_COUNT for v in violations)


def test_newline_rejected():
    """14. newline is rejected."""
    facts = _make_sample_facts()
    exp = "Pay IDR 15,656,000 today.\nThis leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_FORMAT_NEWLINE for v in violations)


def test_empty_explanation_rejected():
    """15. empty explanation is rejected."""
    facts = _make_sample_facts()
    violations = validate_explanation("", facts)
    assert any(v.rule == RULE_FORMAT_EMPTY for v in violations)


def test_wrong_spending_change_amount_rejected():
    """16. wrong spending-change amount is rejected."""
    facts = _make_sample_facts(
        spending_changes="reduce_to:event_1816:23.50",
    )
    # Explanation references 50.00 instead of 23.50
    exp = "With the listed spending changes, reduce to IDR 50.00, then pay IDR 15,656,000 today. This leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert any(v.rule == RULE_NUMBER_NOT_IN_FACTS for v in violations)


def test_not_recommended_recommending_payment_rejected():
    """17. not_recommended explanation recommending payment is rejected."""
    facts = _make_sample_facts(
        payment_method="not_recommended",
        affordability_status="not_affordable",
        payment_plan="none",
        earliest_date=None,
    )
    # Recommends paying
    exp = "Pay IDR 15,656,000 today. This leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert any(v.rule in (RULE_NOT_RECOMMENDED_RECOMMENDS_PAYMENT, RULE_ACTION_METHOD_MISMATCH) for v in violations)


# ---------------------------------------------------------------------------
# Tests 18-20: Numeric & Date precision
# ---------------------------------------------------------------------------
def test_thousands_separators_handled():
    """18. thousands separators are handled correctly."""
    facts = _make_sample_facts(
        requested_amount=15656000.0,
        minimum_balance=24768300.0,
        payment_plan="2025-08-03:15656000",
    )
    exp = "Pay IDR 15,656,000 today. This leaves at least IDR 24,768,300 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert not violations


def test_decimals_handled():
    """19. decimal numbers are handled correctly."""
    facts = _make_sample_facts(
        requested_amount=603.30,
        amount_safe_to_pay=603.30,
        minimum_balance=500.25,
        home_currency="USD",
        payment_plan="2025-08-03:603.30",
    )
    exp = "Pay USD 603.30 today. This leaves at least USD 500.25 available over the next 90 days."
    violations = validate_explanation(exp, facts)
    assert not violations


def test_dates_not_mistaken_for_numbers():
    """20. dates are not mistaken for monetary values."""
    facts = _make_sample_facts(
        requested_amount=5491000.0,
        amount_safe_to_pay=0.0,
        minimum_balance=2668700.0,
        home_currency="IDR",
        request_date="2019-10-01",
        earliest_date="2019-11-15",
        payment_method="wait",
        affordability_status="affordable_later",
        payment_plan="2019-11-15:5491000",
    )
    # In '15 November 2019', 15, 11, and 2019 must not be extracted as monetary numbers
    exp = "Pay IDR 5,491,000 in full on 15 November 2019. Paying earlier would take the balance below the IDR 2,668,700 minimum."
    violations = validate_explanation(exp, facts)
    assert not violations, f"Expected 0 violations, got: {violations}"

    # Also check ISO date '2019-11-15'
    exp_iso = "Pay IDR 5,491,000 in full on 2019-11-15. Paying earlier would take the balance below the IDR 2,668,700 minimum."
    violations_iso = validate_explanation(exp_iso, facts)
    assert not violations_iso, f"Expected 0 violations for ISO date, got: {violations_iso}"


# ---------------------------------------------------------------------------
# Tests 21-22: Full dataset & sample tests
# ---------------------------------------------------------------------------
def test_sample_style_explanations_pass():
    """21. sample-style explanations pass."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    sample_path = repo_root / "dataset" / "sample_requests.csv"
    profiles_path = repo_root / "dataset" / "financial_profiles.csv"

    samples_df = pd.read_csv(sample_path, dtype=str, keep_default_na=False)
    profiles_df = pd.read_csv(profiles_path, dtype=str, keep_default_na=False)
    profiles_by_user = {row["user_id"]: row.to_dict() for _, row in profiles_df.iterrows()}

    for _, row in samples_df.iterrows():
        req_id = row["request_id"]
        exp = row["decision_explanation"]
        user_id = row["user_id"]
        prof = profiles_by_user.get(user_id, {})

        facts = build_fact_dict(row.to_dict(), row.to_dict(), prof)
        violations = validate_explanation(exp, facts)
        assert not violations, f"Sample {req_id} failed with violations: {violations}"


def test_current_output_csv_explanations_pass():
    """22. current output.csv explanations pass (all 250 rows)."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    output_path = repo_root / "output.csv"
    dataset_dir = repo_root / "dataset"

    total, violations = inspect_output_file(output_path, dataset_dir)
    assert total == 250, f"Expected 250 rows in output.csv, got {total}"
    assert len(violations) == 0, f"Expected 0 violations, got {len(violations)}: {violations[:5]}"


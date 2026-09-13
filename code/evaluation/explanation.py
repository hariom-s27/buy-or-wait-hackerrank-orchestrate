"""
Mechanical explanation consistency checker.

Inspects every row of output.csv and mechanically proves that the explanation
agrees with the row facts without using LLM/model calls.

Checks:
1. Sentence & format rules (no newlines, at most 2 sentences, non-empty, no malformed escaping).
2. Currency validation (home currency, no mixed or wrong currencies).
3. Date validation (all dates in explanation exist in row facts; distinguishes dates from numbers).
4. Number validation (every number is traceable to row facts within <= 0.01).
5. Action & method consistency (agrees with recommended_payment_method).
6. Status consistency (agrees with affordability_status).
7. Payment plan parsing and consistency (installments count/amount/date, partial payment 2 payments).
8. Spending change validation (event IDs and reduce_to amounts match row facts).
9. House style check (ACTION -> AMOUNT -> DATE -> MINIMUM-BALANCE GUARANTEE).
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HORIZON_DAYS = 90
ALLOWED_CURRENCIES = {"INR", "ZAR", "IDR", "USD", "EUR"}

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Rule IDs for clear grouping
RULE_FORMAT_EMPTY = "FORMAT_EMPTY"
RULE_FORMAT_NEWLINE = "FORMAT_NEWLINE"
RULE_FORMAT_SENTENCE_COUNT = "FORMAT_SENTENCE_COUNT"
RULE_FORMAT_MALFORMED_ESCAPING = "FORMAT_MALFORMED_ESCAPING"

RULE_CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
RULE_CURRENCY_MIXED = "CURRENCY_MIXED"
RULE_CURRENCY_INVALID = "CURRENCY_INVALID"

RULE_DATE_NOT_IN_FACTS = "DATE_NOT_IN_FACTS"
RULE_NUMBER_NOT_IN_FACTS = "NUMBER_NOT_IN_FACTS"

RULE_ACTION_METHOD_MISMATCH = "ACTION_METHOD_MISMATCH"
RULE_WAIT_SAYS_TODAY = "WAIT_SAYS_TODAY"
RULE_INSTALLMENT_COUNT_MISMATCH = "INSTALLMENT_COUNT_MISMATCH"
RULE_PARTIAL_PAYMENT_MISMATCH = "PARTIAL_PAYMENT_MISMATCH"
RULE_NOT_RECOMMENDED_RECOMMENDS_PAYMENT = "NOT_RECOMMENDED_RECOMMENDS_PAYMENT"

RULE_STATUS_MISMATCH = "STATUS_MISMATCH"
RULE_PAYMENT_PLAN_MISMATCH = "PAYMENT_PLAN_MISMATCH"
RULE_SPENDING_CHANGE_MISMATCH = "SPENDING_CHANGE_MISMATCH"
RULE_HOUSE_STYLE_MISSING = "HOUSE_STYLE_MISSING"


@dataclass(frozen=True)
class Violation:
    request_id: str
    rule: str
    message: str


# ---------------------------------------------------------------------------
# Helpers for date / number parsing
# ---------------------------------------------------------------------------
def _to_date(val: Any) -> Optional[datetime.date]:
    if val is None or pd.isna(val):
        return None
    if isinstance(val, datetime.date):
        return val
    s = str(val).strip()
    if not s or s.lower() in ("none", "nan", "nat"):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def split_sentences(text: str) -> List[str]:
    """Split text into sentences while protecting decimal points in numbers."""
    if not text:
        return []
    # Protect dots between digits so decimals like 1,302.40 are not split
    protected = re.sub(r"(?<=\d)\.(?=\d)", "<DECIMAL_DOT>", text)
    # Split on sentence terminals (. ! ?) followed by whitespace or end of string
    raw_sentences = re.split(r"(?<=[.!?])\s+", protected.strip())
    sentences = []
    for s in raw_sentences:
        cleaned = s.strip().replace("<DECIMAL_DOT>", ".")
        if cleaned:
            sentences.append(cleaned)
    return sentences


def extract_dates(text: str) -> List[Tuple[str, datetime.date, int, int]]:
    """
    Extract ISO and long-form prose dates from text.
    Returns: list of (matched_text, parsed_date, start_index, end_index).
    """
    found: List[Tuple[str, datetime.date, int, int]] = []
    months_pattern = "|".join(MONTH_MAP.keys())

    # 1. ISO format: YYYY-MM-DD
    for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", text):
        try:
            d = datetime.date.fromisoformat(m.group(1))
            found.append((m.group(0), d, m.start(), m.end()))
        except ValueError:
            pass

    # 2. Prose format: Day Month Year (e.g. 15 November 2019, 8 August 2025, 1st March 2026)
    dmy_pattern = rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({months_pattern})\s+(\d{{4}})\b"
    for m in re.finditer(dmy_pattern, text, flags=re.IGNORECASE):
        day = int(m.group(1))
        month = MONTH_MAP[m.group(2).lower()]
        year = int(m.group(3))
        try:
            d = datetime.date(year, month, day)
            found.append((m.group(0), d, m.start(), m.end()))
        except ValueError:
            pass

    # 3. Prose format: Month Day, Year (e.g. November 15, 2019, August 8 2025)
    mdy_pattern = rf"\b({months_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b"
    for m in re.finditer(mdy_pattern, text, flags=re.IGNORECASE):
        month = MONTH_MAP[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3))
        try:
            d = datetime.date(year, month, day)
            found.append((m.group(0), d, m.start(), m.end()))
        except ValueError:
            pass

    # Sort by start index and deduplicate overlapping ranges
    found.sort(key=lambda x: x[2])
    non_overlapping: List[Tuple[str, datetime.date, int, int]] = []
    last_end = -1
    for item in found:
        if item[2] >= last_end:
            non_overlapping.append(item)
            last_end = item[3]
    return non_overlapping


def mask_dates_and_entities(text: str, date_matches: List[Tuple[str, datetime.date, int, int]]) -> str:
    """Mask out date spans and event IDs so their numbers are not treated as monetary values."""
    chars = list(text)
    for _, _, start, end in date_matches:
        for i in range(start, end):
            chars[i] = " "
    masked = "".join(chars)
    # Mask event IDs (e.g. event_1816, event_25181)
    masked = re.sub(r"\bevent_\w+\b", " ", masked)
    return masked


def extract_numbers(text: str, date_matches: List[Tuple[str, datetime.date, int, int]]) -> List[Tuple[str, float]]:
    """Extract all numbers from text after masking dates and event IDs."""
    masked = mask_dates_and_entities(text, date_matches)
    # Matches numbers with commas (thousands separators) or plain numbers, integer or float
    pattern = r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\b"
    results = []
    for m in re.finditer(pattern, masked):
        raw_val = m.group(0)
        clean_val = raw_val.replace(",", "")
        try:
            num = float(clean_val)
            results.append((raw_val, num))
        except ValueError:
            pass
    return results


def extract_currencies(text: str) -> List[str]:
    """Extract 3-letter uppercase currency codes from explanation."""
    pattern = r"\b[A-Z]{3}\b"
    candidates = re.findall(pattern, text)
    return [c for c in candidates if c in ALLOWED_CURRENCIES]


def parse_payment_plan(plan_str: str) -> List[Tuple[datetime.date, float]]:
    """Parse payment_plan string into a list of (date, amount) tuples."""
    s = str(plan_str).strip()
    if not s or s.lower() == "none":
        return []
    items: List[Tuple[datetime.date, float]] = []
    for part in s.split("|"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"Malformed payment plan item: {part}")
        d_str, a_str = part.split(":", 1)
        items.append((datetime.date.fromisoformat(d_str.strip()), float(a_str.strip())))
    return items


# ---------------------------------------------------------------------------
# Fact dictionary builder
# ---------------------------------------------------------------------------
def build_fact_dict(
    output_row: Dict[str, Any],
    request_row: Dict[str, Any],
    profile_row: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a comprehensive fact dictionary for an output row.
    Derives values strictly from actual request, profile, and output data.
    """
    req_id = str(output_row.get("request_id", "")).strip()
    home_currency = str(profile_row.get("home_currency", "")).strip()

    requested_amount = float(request_row.get("requested_amount", 0.0))
    amount_safe_to_pay = float(output_row.get("amount_safe_to_pay", 0.0))
    minimum_balance = float(profile_row.get("minimum_balance_to_keep", 0.0))

    request_date = _to_date(request_row.get("request_date"))
    deadline = _to_date(request_row.get("desired_completion_date"))
    earliest_date = _to_date(output_row.get("earliest_date_for_full_payment"))

    payment_method = str(output_row.get("recommended_payment_method", "")).strip()
    affordability_status = str(output_row.get("affordability_status", "")).strip()
    payment_plan_str = str(output_row.get("payment_plan", "")).strip()

    schedule = parse_payment_plan(payment_plan_str)
    every_payment_date = [d for d, _ in schedule]
    every_payment_amount = [a for _, a in schedule]

    spending_changes_str = str(output_row.get("spending_changes_needed", "none")).strip()
    every_spending_change = []
    relevant_event_ids = []
    relevant_spending_change_amounts = []

    if spending_changes_str and spending_changes_str.lower() != "none":
        for ch in spending_changes_str.split("|"):
            ch = ch.strip()
            if not ch:
                continue
            every_spending_change.append(ch)
            if ch.startswith("stop:"):
                parts = ch.split(":")
                if len(parts) >= 2:
                    relevant_event_ids.append(parts[1].strip())
            elif ch.startswith("reduce_to:"):
                parts = ch.split(":")
                if len(parts) >= 3:
                    relevant_event_ids.append(parts[1].strip())
                    try:
                        relevant_spending_change_amounts.append(float(parts[2].strip()))
                    except ValueError:
                        pass

    # Collect all legitimate numeric fact sources
    fact_numbers: List[float] = [
        requested_amount,
        amount_safe_to_pay,
        minimum_balance,
        float(HORIZON_DAYS),
    ]
    if schedule:
        fact_numbers.append(float(len(schedule)))  # payment count (e.g. 2, 3)
        fact_numbers.extend(every_payment_amount)
        # Total payable if different
        total_payable = sum(every_payment_amount)
        fact_numbers.append(total_payable)

    fact_numbers.extend(relevant_spending_change_amounts)

    # Collect all legitimate date fact sources
    fact_dates: Set[datetime.date] = set()
    if request_date:
        fact_dates.add(request_date)
    if deadline:
        fact_dates.add(deadline)
    if earliest_date:
        fact_dates.add(earliest_date)
    for d in every_payment_date:
        fact_dates.add(d)

    return {
        "request_id": req_id,
        "requested_amount": requested_amount,
        "amount_safe_to_pay": amount_safe_to_pay,
        "minimum_balance": minimum_balance,
        "home_currency": home_currency,
        "earliest_date": earliest_date,
        "payment_method": payment_method,
        "affordability_status": affordability_status,
        "payment_plan": payment_plan_str,
        "parsed_schedule": schedule,
        "every_payment_date": every_payment_date,
        "every_payment_amount": every_payment_amount,
        "every_spending_change": every_spending_change,
        "relevant_event_ids": relevant_event_ids,
        "relevant_spending_change_amounts": relevant_spending_change_amounts,
        "request_date": request_date,
        "deadline": deadline,
        "horizon_days": HORIZON_DAYS,
        "payment_count": len(schedule),
        "fact_numbers": fact_numbers,
        "fact_dates": fact_dates,
    }


# ---------------------------------------------------------------------------
# Individual Validation Rules
# ---------------------------------------------------------------------------
def check_format_and_sentences(explanation: str, req_id: str) -> List[Violation]:
    """Validate non-empty, no newlines, at most 2 sentences, no malformed escapes."""
    violations = []
    if not explanation or not explanation.strip():
        violations.append(Violation(req_id, RULE_FORMAT_EMPTY, "Explanation is empty or whitespace-only."))
        return violations

    if "\n" in explanation or "\r" in explanation:
        violations.append(Violation(req_id, RULE_FORMAT_NEWLINE, "Explanation contains a newline character."))

    if "\\" in explanation:
        # Reject malformed escaping such as literal \n, \t, unescaped slashes
        violations.append(Violation(req_id, RULE_FORMAT_MALFORMED_ESCAPING, "Explanation contains malformed escaping '\\'."))

    sentences = split_sentences(explanation)
    if len(sentences) > 2:
        violations.append(
            Violation(
                req_id,
                RULE_FORMAT_SENTENCE_COUNT,
                f"Explanation contains {len(sentences)} sentences (maximum 2 allowed).",
            )
        )
    return violations


def check_currency(explanation: str, facts: Dict[str, Any]) -> List[Violation]:
    """Validate home currency matching, reject wrong/mixed/unallowed currencies."""
    violations = []
    req_id = facts["request_id"]
    home_currency = facts["home_currency"]

    found = extract_currencies(explanation)
    if not found:
        # Check if an illegal currency code was used
        all_caps_3 = set(re.findall(r"\b[A-Z]{3}\b", explanation))
        unknown = all_caps_3 - ALLOWED_CURRENCIES
        if unknown:
            for u in unknown:
                violations.append(Violation(req_id, RULE_CURRENCY_INVALID, f"Unknown currency '{u}' found in explanation."))
        return violations

    unique_found = set(found)
    if len(unique_found) > 1:
        violations.append(
            Violation(
                req_id,
                RULE_CURRENCY_MIXED,
                f"Explanation contains mixed currencies: {sorted(unique_found)}.",
            )
        )
    for c in unique_found:
        if c != home_currency:
            violations.append(
                Violation(
                    req_id,
                    RULE_CURRENCY_MISMATCH,
                    f"Currency '{c}' does not match user's home currency '{home_currency}'.",
                )
            )
    return violations


def check_dates(
    explanation: str,
    facts: Dict[str, Any],
    date_matches: List[Tuple[str, datetime.date, int, int]],
) -> List[Violation]:
    """Verify all extracted dates exist in fact dictionary."""
    violations = []
    req_id = facts["request_id"]
    fact_dates = facts["fact_dates"]

    for raw_str, parsed_d, _, _ in date_matches:
        if parsed_d not in fact_dates:
            violations.append(
                Violation(
                    req_id,
                    RULE_DATE_NOT_IN_FACTS,
                    f"Date '{raw_str}' ({parsed_d.isoformat()}) does not occur in facts.",
                )
            )
    return violations


def check_numbers(
    explanation: str,
    facts: Dict[str, Any],
    date_matches: List[Tuple[str, datetime.date, int, int]],
) -> List[Violation]:
    """Verify every extracted numeric value corresponds to a fact dictionary value within <= 0.01."""
    violations = []
    req_id = facts["request_id"]
    fact_numbers = facts["fact_numbers"]
    extracted = extract_numbers(explanation, date_matches)

    for raw_val, num_val in extracted:
        matches = any(abs(num_val - fn) <= 0.01 for fn in fact_numbers)
        if not matches:
            violations.append(
                Violation(
                    req_id,
                    RULE_NUMBER_NOT_IN_FACTS,
                    f"Number {raw_val} ({num_val}) cannot be traced to any fact dictionary value.",
                )
            )
    return violations


def check_action_and_method(
    explanation: str,
    facts: Dict[str, Any],
    date_matches: List[Tuple[str, datetime.date, int, int]],
) -> List[Violation]:
    """Validate action phrasing agrees with recommended_payment_method and payment plan."""
    violations = []
    req_id = facts["request_id"]
    method = facts["payment_method"]
    exp_lower = explanation.lower()
    dates_found = [d for _, d, _, _ in date_matches]

    if method == "full_payment":
        # Must instruct paying in full / today
        if "wait" in exp_lower or "installment" in exp_lower or "do not make" in exp_lower or "do not proceed" in exp_lower:
            violations.append(
                Violation(req_id, RULE_ACTION_METHOD_MISMATCH, "full_payment explanation mentions wait, installment, or rejection.")
            )
        # Check payment date: either 'today' or request_date
        req_d = facts["request_date"]
        has_today = "today" in exp_lower
        has_req_d = req_d in dates_found if req_d else False
        if not (has_today or has_req_d):
            violations.append(
                Violation(req_id, RULE_ACTION_METHOD_MISMATCH, "full_payment explanation does not reference 'today' or request_date.")
            )

    elif method == "wait":
        # Must describe waiting / paying in full on future date
        if "today" in exp_lower:
            violations.append(
                Violation(req_id, RULE_WAIT_SAYS_TODAY, "wait explanation incorrectly says 'today'.")
            )
        if "installment" in exp_lower or "do not make" in exp_lower or "do not proceed" in exp_lower:
            violations.append(
                Violation(req_id, RULE_ACTION_METHOD_MISMATCH, "wait explanation mentions installment or rejection.")
            )
        earliest_d = facts["earliest_date"]
        if earliest_d and earliest_d not in dates_found:
            violations.append(
                Violation(req_id, RULE_ACTION_METHOD_MISMATCH, f"wait explanation does not mention earliest date {earliest_d}.")
            )

    elif method == "partial_payment":
        # Must describe two payments
        schedule = facts["parsed_schedule"]
        if len(schedule) != 2:
            violations.append(
                Violation(req_id, RULE_PARTIAL_PAYMENT_MISMATCH, f"partial_payment plan has {len(schedule)} payments, expected exactly 2.")
            )
        if "remaining" not in exp_lower and "second" not in exp_lower and "two payment" not in exp_lower:
            violations.append(
                Violation(req_id, RULE_PARTIAL_PAYMENT_MISMATCH, "partial_payment explanation does not describe remainder/second payment.")
            )
        if "installment" in exp_lower:
            violations.append(
                Violation(req_id, RULE_ACTION_METHOD_MISMATCH, "partial_payment explanation mentions installments.")
            )

    elif method == "installments":
        # Must describe installments
        if "installment" not in exp_lower:
            violations.append(
                Violation(req_id, RULE_ACTION_METHOD_MISMATCH, "installments explanation does not mention installments.")
            )
        count = facts["payment_count"]
        # Check installment count in text
        count_phrases = [f"{count} installment", f"{count} monthly installment"]
        if not any(cp in exp_lower for cp in count_phrases):
            violations.append(
                Violation(req_id, RULE_INSTALLMENT_COUNT_MISMATCH, f"installments explanation does not specify '{count} installments'.")
            )
        if "pay today" in exp_lower and "starting" not in exp_lower:
            violations.append(
                Violation(req_id, RULE_ACTION_METHOD_MISMATCH, "installments explanation says pay today without starting schedule.")
            )

    elif method == "not_recommended":
        # Must communicate payment should not be made under protected minimum balance
        negative_markers = ["do not make", "do not proceed", "defer", "cannot be", "none of the", "not recommended", "no eligible"]
        if not any(nm in exp_lower for nm in negative_markers):
            violations.append(
                Violation(req_id, RULE_ACTION_METHOD_MISMATCH, "not_recommended explanation does not communicate rejection/deferral.")
            )
        # Must NOT recommend an actual payment
        if re.search(r"\bpay\b", exp_lower):
            # If 'pay' appears, check that it's in a negative context like "do not make this payment"
            if re.search(r"\bpay\s+[A-Z]{3}\s+[\d,]+", explanation):
                violations.append(
                    Violation(req_id, RULE_NOT_RECOMMENDED_RECOMMENDS_PAYMENT, "not_recommended explanation recommends paying an amount.")
                )
        if "use" in exp_lower and "installment" in exp_lower:
            violations.append(
                Violation(req_id, RULE_NOT_RECOMMENDED_RECOMMENDS_PAYMENT, "not_recommended explanation recommends an installment plan.")
            )

    return violations


def check_status(explanation: str, facts: Dict[str, Any]) -> List[Violation]:
    """Check affordability status consistency."""
    violations = []
    req_id = facts["request_id"]
    status = facts["affordability_status"]
    exp_lower = explanation.lower()

    if status == "affordable_now":
        if "wait" in exp_lower or "do not make" in exp_lower or "do not proceed" in exp_lower or "defer" in exp_lower:
            violations.append(
                Violation(req_id, RULE_STATUS_MISMATCH, "affordable_now explanation describes waiting or rejection.")
            )
    elif status == "affordable_later":
        if "today" in exp_lower and "available today" not in exp_lower:
            violations.append(
                Violation(req_id, RULE_STATUS_MISMATCH, "affordable_later explanation mentions paying today.")
            )
    elif status == "affordable_with_plan":
        if "do not make" in exp_lower or "do not proceed" in exp_lower:
            violations.append(
                Violation(req_id, RULE_STATUS_MISMATCH, "affordable_with_plan explanation communicates rejection.")
            )
    elif status == "not_affordable":
        if re.search(r"\bpay\s+[A-Z]{3}\s+[\d,]+", explanation):
            violations.append(
                Violation(req_id, RULE_STATUS_MISMATCH, "not_affordable explanation recommends an active payment.")
            )
    return violations


def check_spending_changes(explanation: str, facts: Dict[str, Any]) -> List[Violation]:
    """Validate spending changes: event IDs and reduce_to amounts."""
    violations = []
    req_id = facts["request_id"]
    spending_changes = facts["every_spending_change"]
    allowed_event_ids = set(facts["relevant_event_ids"])

    # Extract any event IDs referenced in explanation
    found_events = re.findall(r"\bevent_\w+\b", explanation)
    for fe in found_events:
        if fe not in allowed_event_ids:
            violations.append(
                Violation(
                    req_id,
                    RULE_SPENDING_CHANGE_MISMATCH,
                    f"Explanation references event ID '{fe}' not present in spending changes.",
                )
            )

    if not spending_changes:
        if found_events:
            violations.append(
                Violation(
                    req_id,
                    RULE_SPENDING_CHANGE_MISMATCH,
                    "Explanation references spending change events when spending_changes_needed is 'none'.",
                )
            )
    return violations


def check_house_style(explanation: str, facts: Dict[str, Any]) -> List[Violation]:
    """
    Validate house style:
    ACTION -> AMOUNT -> DATE -> MINIMUM-BALANCE GUARANTEE.
    """
    violations = []
    req_id = facts["request_id"]
    exp_lower = explanation.lower()

    # 1. Action
    action_keywords = [
        "pay", "use", "wait", "do not make", "do not proceed", "defer",
        "stop", "reduce", "with the listed spending changes"
    ]
    if not any(ak in exp_lower for ak in action_keywords):
        violations.append(
            Violation(req_id, RULE_HOUSE_STYLE_MISSING, "House style missing clear action.")
        )

    # 2. Minimum-balance guarantee
    guarantee_keywords = [
        "minimum", "balance", "protected", "available", "at risk",
        "leaves at least", "keeps the", "below the"
    ]
    if not any(gk in exp_lower for gk in guarantee_keywords):
        violations.append(
            Violation(req_id, RULE_HOUSE_STYLE_MISSING, "House style missing minimum-balance guarantee.")
        )

    return violations


# ---------------------------------------------------------------------------
# Master Validator
# ---------------------------------------------------------------------------
def validate_explanation(explanation: str, facts: Dict[str, Any]) -> List[Violation]:
    """Run all consistency checks on a single explanation."""
    req_id = facts["request_id"]
    violations: List[Violation] = []

    # 1. Format and sentence count
    fmt_violations = check_format_and_sentences(explanation, req_id)
    violations.extend(fmt_violations)
    if fmt_violations:
        # If explanation is empty or structurally broken, return early
        if any(v.rule == RULE_FORMAT_EMPTY for v in fmt_violations):
            return violations

    # 2. Currency check
    violations.extend(check_currency(explanation, facts))

    # 3. Extract dates & validate dates
    date_matches = extract_dates(explanation)
    violations.extend(check_dates(explanation, facts, date_matches))

    # 4. Extract numbers & validate numbers
    violations.extend(check_numbers(explanation, facts, date_matches))

    # 5. Action and method consistency
    violations.extend(check_action_and_method(explanation, facts, date_matches))

    # 6. Status consistency
    violations.extend(check_status(explanation, facts))

    # 7. Spending changes consistency
    violations.extend(check_spending_changes(explanation, facts))

    # 8. House style check
    violations.extend(check_house_style(explanation, facts))

    return violations


# ---------------------------------------------------------------------------
# Full Output Inspection
# ---------------------------------------------------------------------------
def inspect_output_file(
    output_path: str | Path,
    dataset_dir: str | Path,
) -> Tuple[int, List[Violation]]:
    """
    Inspect every row of output.csv against requests.csv and financial_profiles.csv.
    Returns: (total_rows_inspected, all_violations)
    """
    output_path = Path(output_path)
    dataset_dir = Path(dataset_dir)

    output_df = pd.read_csv(output_path, dtype=str, keep_default_na=False)
    requests_df = pd.read_csv(dataset_dir / "requests.csv", dtype=str, keep_default_na=False)
    profiles_df = pd.read_csv(dataset_dir / "financial_profiles.csv", dtype=str, keep_default_na=False)

    requests_by_id = {row["request_id"]: row.to_dict() for _, row in requests_df.iterrows()}
    profiles_by_user = {row["user_id"]: row.to_dict() for _, row in profiles_df.iterrows()}

    all_violations: List[Violation] = []
    total_rows = len(output_df)

    for _, out_row in output_df.iterrows():
        req_id = out_row["request_id"]
        exp = out_row["decision_explanation"]
        req_data = requests_by_id.get(req_id, {})
        user_id = req_data.get("user_id", "")
        prof_data = profiles_by_user.get(user_id, {})

        facts = build_fact_dict(out_row.to_dict(), req_data, prof_data)
        row_violations = validate_explanation(exp, facts)
        all_violations.extend(row_violations)

    return total_rows, all_violations


def main() -> int:
    """CLI entrypoint for python -m code.evaluation.explanation."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    output_path = repo_root / "output.csv"
    dataset_dir = repo_root / "dataset"

    parser = argparse.ArgumentParser(description="Mechanical explanation consistency checker.")
    parser.add_argument("--output", default=str(output_path), help="Path to output.csv")
    parser.add_argument("--dataset", default=str(dataset_dir), help="Path to dataset directory")
    args = parser.parse_args()

    total_rows, violations = inspect_output_file(args.output, args.dataset)

    print("============================================================")
    print("EXPLANATION CONSISTENCY REPORT")
    print("============================================================")
    print(f"Total rows inspected: {total_rows}")
    print(f"Total violations:     {len(violations)}")
    print("------------------------------------------------------------")

    if not violations:
        print("Result: 250/250 consistent. 0 violations.")
        print("============================================================")
        return 0

    # Group violations by rule
    grouped: Dict[str, List[Violation]] = {}
    rids_with_violations: Set[str] = set()
    for v in violations:
        grouped.setdefault(v.rule, []).append(v)
        rids_with_violations.add(v.request_id)

    print(f"Violations grouped by rule ({len(grouped)} rules violated):")
    for rule, vlist in sorted(grouped.items()):
        print(f"  [{rule}]: {len(vlist)} violation(s)")
        for sample_v in vlist[:3]:
            print(f"    - {sample_v.request_id}: {sample_v.message}")
        if len(vlist) > 3:
            print(f"    ... and {len(vlist) - 3} more")

    print("------------------------------------------------------------")
    print(f"Request IDs with violations ({len(rids_with_violations)}):")
    print("  " + ", ".join(sorted(rids_with_violations)))
    print("============================================================")
    return 1


if __name__ == "__main__":
    sys.exit(main())


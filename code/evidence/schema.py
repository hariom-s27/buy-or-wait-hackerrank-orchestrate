"""
Evidence schema for Buy-or-Wait (Step 10).

Defines the closed EvidenceIntent enum and the flat EvidenceDelta dataclass.

CRITICAL INVARIANT (BUILD-PLAN Part A.3 rule 7 and Step 10):
EvidenceDelta MUST be STRUCTURALLY INCAPABLE of expressing a decision.
No affordability_status, no recommended_payment_method, no amount_safe_to_pay,
no payment_plan, no spending_changes_needed field may exist on it. This module
must never import from code.decide, code.output, or code.forecast.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# Field names that would let EvidenceDelta express a decision. Asserted against
# in code/tests/test_evidence.py so the structural guarantee is enforced by CI,
# not just by convention.
FORBIDDEN_DECISION_FIELDS = frozenset({
    "affordability_status",
    "recommended_payment_method",
    "amount_safe_to_pay",
    "payment_plan",
    "spending_changes_needed",
})


class EvidenceIntent(str, Enum):
    """The closed vocabulary of exactly 18 financial evidence intents plus NO_OP (19 total)."""
    SALARY_SET_AMOUNT = "SALARY_SET_AMOUNT"
    SALARY_SET_DATE = "SALARY_SET_DATE"
    SALARY_RESUME = "SALARY_RESUME"
    INCOME_ENDED = "INCOME_ENDED"
    INCOME_UNCONFIRMED = "INCOME_UNCONFIRMED"
    INCOME_CONFIRMED_ONE_OFF = "INCOME_CONFIRMED_ONE_OFF"
    ONE_TIME_ARREARS = "ONE_TIME_ARREARS"
    RENT_INCREASE_PCT = "RENT_INCREASE_PCT"
    SELF_TRANSFER_DUPLICATE = "SELF_TRANSFER_DUPLICATE"
    REFUND_PENDING = "REFUND_PENDING"
    DISPUTE_OPEN = "DISPUTE_OPEN"
    UNREALIZED_VALUATION = "UNREALIZED_VALUATION"
    PRIZE_CREDITED = "PRIZE_CREDITED"
    SALE_SETTLED = "SALE_SETTLED"
    REIMBURSEMENT_NOT_SALARY = "REIMBURSEMENT_NOT_SALARY"
    FAILED_DEBIT_RETRY = "FAILED_DEBIT_RETRY"
    FX_SETTLEMENT = "FX_SETTLEMENT"
    TWO_CARD_MINIMUMS = "TWO_CARD_MINIMUMS"
    NO_OP = "NO_OP"


# Extraction provenance — how this delta's fields were determined. Not a decision
# field: it says nothing about affordability, method, amount, plan, or spending.
EXTRACTION_PATHS = frozenset({"LLM", "REGEX", "NO_OP", "DROPPED"})


@dataclass(frozen=True)
class EvidenceDelta:
    """A single piece of extracted financial evidence from an untrusted message or image.

    This dataclass is structurally incapable of expressing a recommendation:
    it contains ONLY objective financial parameters (amounts, dates, percentages,
    or entity references) plus extraction provenance. See FORBIDDEN_DECISION_FIELDS.
    """
    source_id: str                      # message_id or image_id
    user_id: str                        # user the evidence applies to
    intent: str                         # string matching an EvidenceIntent value
    target_type: str                    # 'stream' | 'event'
    target: str                         # event_id or stream category
    effective_date: Optional[datetime.date] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    percent: Optional[float] = None
    evidence_span: str = ""             # verbatim text fragment supporting this
    confidence: float = 1.0
    # --- provenance / ordering metadata (not decision fields) ---
    extraction_path: str = "REGEX"      # LLM | REGEX | NO_OP | DROPPED
    sent_at: Optional[str] = None       # source message's sent_at, for precedence ordering


def assert_decision_incapable() -> None:
    """Raise if EvidenceDelta's field set ever grows a forbidden decision field.

    Called from the structural safety test (code/tests/test_evidence.py) so a
    future edit to this file cannot silently reintroduce a decision field.
    """
    field_names = set(EvidenceDelta.__dataclass_fields__.keys())
    overlap = field_names & FORBIDDEN_DECISION_FIELDS
    if overlap:
        raise AssertionError(f"EvidenceDelta illegally carries decision fields: {sorted(overlap)}")

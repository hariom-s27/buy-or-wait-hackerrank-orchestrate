"""
Domain models for Buy-or-Wait.

Typed dataclasses for every entity in the dataset. No forecasting or
business-decision logic lives here — these are pure data containers.

Amounts are float (or Optional[float] when the source CSV can be blank).
Dates are datetime.date.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Source-data models (loaded from CSVs)
# ---------------------------------------------------------------------------

@dataclass
class Request:
    """One row of requests.csv."""
    request_id: str
    user_id: str
    request_date: datetime.date
    request_type: str
    requested_amount: float
    desired_completion_date: datetime.date
    allows_partial_payment: bool
    request_text: str


@dataclass
class Profile:
    """One row of financial_profiles.csv."""
    user_id: str
    home_currency: str
    current_available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: List[str]
    expense_categories_to_protect: List[str]
    expense_categories_user_is_willing_to_reduce: List[str]
    expense_categories_user_is_willing_to_stop: List[str]
    payment_methods_user_will_consider: List[str]
    max_installment_months: Optional[int]  # None when user rejects installments


@dataclass
class Event:
    """One row of financial_events.csv.

    ``amount`` is Optional[float]: a blank amount in the CSV must load
    as ``None``, **never** 0.0.  Later steps may resolve the amount via
    image extraction or stream-mean imputation, but the loader must
    preserve the blank.
    """
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Optional[float]          # blank → None, NEVER 0.0
    currency: str
    event_date: datetime.date
    settlement_date: Optional[datetime.date]  # None when blank (e.g. unrealized status)
    status: str
    linked_event_id: Optional[str]   # None when blank
    flexibility: Optional[str]       # None when blank
    minimum_allowed_amount: Optional[float]  # None when blank


@dataclass
class PaymentOption:
    """One row of request_payment_options.csv."""
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: float
    number_of_payments: int
    first_payment_date: datetime.date
    payment_frequency_days: Optional[int]  # None for full_payment (single pay)
    financing_fee: float
    total_payable_amount: float


@dataclass
class Message:
    """One row of messages.csv."""
    message_id: str
    user_id: str
    request_id: Optional[str]         # None when blank
    related_event_id: Optional[str]   # None when blank
    sent_at: str                      # ISO-8601 timestamp string
    source_type: str
    message_text: str


@dataclass
class ImageRef:
    """One row of images.csv."""
    image_id: str
    user_id: str
    request_id: str
    related_event_id: str


# ---------------------------------------------------------------------------
# Exchange rate row
# ---------------------------------------------------------------------------

@dataclass
class ExchangeRate:
    """One row of exchange_rates.csv."""
    rate_date: datetime.date
    from_currency: str
    to_currency: str
    rate: float


# ---------------------------------------------------------------------------
# Derived / later-step models (defined now for type completeness)
# ---------------------------------------------------------------------------

@dataclass
class Stream:
    """A detected recurring financial stream (income or expense).

    Built in Step 3 (salary) and Step 4 (expenses). Defined here so that
    the type is available across modules.
    """
    stream_id: str                    # e.g. "salary:<description>" or "expense:<category>"
    user_id: str
    category: str
    description: str
    direction: str                    # 'credit' or 'debit'
    currency: str
    level: float                      # projected recurring amount
    cadence_days: int                 # median gap between occurrences
    last_date: datetime.date          # most recent occurrence
    representative_event_id: str      # newest event in the stream
    flexibility: Optional[str]
    minimum_allowed_amount: Optional[float]


@dataclass
class LedgerEntry:
    """One projected cash-flow entry on a specific date.

    Used by the 90-day forecast ledger (Step 4).
    """
    date: datetime.date
    amount: float                     # signed: positive = credit, negative = debit
    source: str                       # provenance tag, e.g. "stream:rent", "special:pending"
    event_id: Optional[str] = None    # originating event, if any


from code.evidence.schema import EvidenceDelta, EvidenceIntent


@dataclass
class CandidatePlan:
    """A candidate payment plan evaluated during decision (Step 6).

    Each plan has a method, a schedule, and a safety assessment.
    """
    method: str                       # full_payment | partial_payment | installments | wait
    payment_option_id: Optional[str]  # only for installments
    schedule: List[tuple]             # [(date, amount), ...] chronological
    total_payable: float
    is_safe: bool = False
    rejection_reason: Optional[str] = None
    spending_changes: Optional[List[str]] = None  # e.g. ["stop:event_123"]


@dataclass
class ForecastResult:
    """Result of the 90-day cash-flow forecast for one user (Step 5)."""
    user_id: str
    request_date: datetime.date
    opening_balance: float
    minimum_balance: float
    trough: float                     # minimum end-of-day balance in the window
    trough_date: Optional[datetime.date] = None
    amount_safe_to_pay: float = 0.0
    earliest_date_for_full_payment: Optional[datetime.date] = None


@dataclass
class Decision:
    """Final decision for one request (Step 6)."""
    request_id: str
    amount_safe_to_pay: float
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str                 # formatted plan string or "none"
    earliest_date_for_full_payment: str  # ISO date or ""
    spending_changes_needed: str      # "none" or pipe-delimited changes
    decision_explanation: str


# ---------------------------------------------------------------------------
# The assembled case for one request (Step 2 index output)
# ---------------------------------------------------------------------------

@dataclass
class RequestCase:
    """All data needed to decide one request, assembled from indexes.

    Uses nested collections — never a flat cross-product join.
    """
    request: Request
    profile: Profile
    events: List[Event]               # all events for this user
    payment_options: List[PaymentOption]  # options for this request only
    messages: List[Message]           # messages relevant to this user/request/events
    images: List[ImageRef]            # images relevant to this request/events


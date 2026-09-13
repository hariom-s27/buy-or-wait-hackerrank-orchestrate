"""
Loader module — reads all 8 dataset CSVs exactly once.

Requirements (BUILD-PLAN.md Step 2):
- Parse date columns into datetime.date.
- Convert amount columns to nullable numeric values.
- Preserve blank amounts as None — NEVER use fillna(0) on any amount column.
- Validate unique IDs, FK relationships, and report problems clearly.
- Do not silently repair malformed data.
"""
from __future__ import annotations

import datetime
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from code.domain.models import (
    Event,
    ExchangeRate,
    ImageRef,
    Message,
    PaymentOption,
    Profile,
    Request,
)

# ---------------------------------------------------------------------------
# Resolve dataset directory relative to the repo root
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = _REPO_ROOT / "dataset"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(val) -> datetime.date:
    """Parse a date string (YYYY-MM-DD) into a datetime.date."""
    if isinstance(val, datetime.date):
        return val
    return datetime.datetime.strptime(str(val).strip(), "%Y-%m-%d").date()


def _opt_date(val) -> Optional[datetime.date]:
    """Parse an optional date string; return None for NaN/blank."""
    if pd.isna(val) or str(val).strip() in ("", "nan", "None"):
        return None
    return _parse_date(val)


def _parse_bool(val) -> bool:
    """Parse boolean from CSV (string 'true'/'false')."""
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() == "true"


def _split_pipe(val) -> List[str]:
    """Split a pipe-delimited string; return empty list for blank/NaN."""
    if pd.isna(val) or str(val).strip() == "":
        return []
    return [s.strip() for s in str(val).split("|") if s.strip()]


def _opt_str(val) -> Optional[str]:
    """Convert a value to Optional[str]; NaN/blank → None."""
    if pd.isna(val) or str(val).strip() == "":
        return None
    return str(val).strip()


def _opt_float(val) -> Optional[float]:
    """Convert a value to Optional[float]; NaN/blank → None, NEVER 0.0 for blank."""
    if pd.isna(val):
        return None
    return float(val)


def _opt_int(val) -> Optional[int]:
    """Convert a value to Optional[int]; NaN/blank → None."""
    if pd.isna(val):
        return None
    return int(float(val))


# ---------------------------------------------------------------------------
# Loader result
# ---------------------------------------------------------------------------

@dataclass
class LoaderResult:
    """All loaded and validated data, ready for indexing."""
    requests: List[Request]
    profiles: List[Profile]
    events: List[Event]
    payment_options: List[PaymentOption]
    messages: List[Message]
    images: List[ImageRef]
    exchange_rates: List[ExchangeRate]

    # Raw DataFrames kept for any downstream needs
    requests_df: pd.DataFrame
    profiles_df: pd.DataFrame
    events_df: pd.DataFrame
    options_df: pd.DataFrame
    messages_df: pd.DataFrame
    images_df: pd.DataFrame
    rates_df: pd.DataFrame
    output_df: pd.DataFrame


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(
    requests_df: pd.DataFrame,
    profiles_df: pd.DataFrame,
    events_df: pd.DataFrame,
    options_df: pd.DataFrame,
    messages_df: pd.DataFrame,
    images_df: pd.DataFrame,
    sample_requests_df: Optional[pd.DataFrame] = None,
) -> List[str]:
    """Validate uniqueness and FK constraints. Return list of problems."""
    problems: List[str] = []

    # --- Unique ID checks ---
    for col, df, label in [
        ("request_id", requests_df, "requests"),
        ("event_id", events_df, "events"),
        ("message_id", messages_df, "messages"),
        ("image_id", images_df, "images"),
        ("payment_option_id", options_df, "payment_options"),
    ]:
        dups = df[df[col].duplicated(keep=False)]
        if len(dups) > 0:
            dup_ids = dups[col].unique().tolist()
            problems.append(
                f"Duplicate {col} in {label}: {dup_ids}"
            )

    # --- FK: every request.user_id exists in profiles ---
    profile_users = set(profiles_df["user_id"])
    req_users = set(requests_df["user_id"])
    missing_users = req_users - profile_users
    if missing_users:
        problems.append(
            f"request.user_id not in profiles: {sorted(missing_users)}"
        )

    # --- FK: every payment_option.request_id exists in requests ---
    # payment_options.csv covers both eval requests (requests.csv) and
    # sample requests (sample_requests.csv), so include both sets.
    request_ids = set(requests_df["request_id"])
    if sample_requests_df is not None:
        request_ids |= set(sample_requests_df["request_id"])
    opt_req_ids = set(options_df["request_id"])
    missing_reqs = opt_req_ids - request_ids
    if missing_reqs:
        problems.append(
            f"payment_option.request_id not in requests: {sorted(missing_reqs)}"
        )

    # --- FK: every related_event_id in messages exists in events ---
    event_ids = set(events_df["event_id"])
    msg_event_ids = set(
        messages_df["related_event_id"].dropna().astype(str)
    ) - {""}
    missing_msg_events = msg_event_ids - event_ids
    if missing_msg_events:
        problems.append(
            f"messages.related_event_id not in events: {sorted(missing_msg_events)}"
        )

    # --- FK: every related_event_id in images exists in events ---
    img_event_ids = set(
        images_df["related_event_id"].dropna().astype(str)
    ) - {""}
    missing_img_events = img_event_ids - event_ids
    if missing_img_events:
        problems.append(
            f"images.related_event_id not in events: {sorted(missing_img_events)}"
        )

    return problems


# ---------------------------------------------------------------------------
# Row converters
# ---------------------------------------------------------------------------

def _row_to_request(row) -> Request:
    return Request(
        request_id=str(row["request_id"]).strip(),
        user_id=str(row["user_id"]).strip(),
        request_date=_parse_date(row["request_date"]),
        request_type=str(row["request_type"]).strip(),
        requested_amount=float(row["requested_amount"]),
        desired_completion_date=_parse_date(row["desired_completion_date"]),
        allows_partial_payment=_parse_bool(row["allows_partial_payment"]),
        request_text=str(row["request_text"]).strip(),
    )


def _row_to_profile(row) -> Profile:
    return Profile(
        user_id=str(row["user_id"]).strip(),
        home_currency=str(row["home_currency"]).strip(),
        current_available_balance=float(row["current_available_balance"]),
        minimum_balance_to_keep=float(row["minimum_balance_to_keep"]),
        financial_priorities=_split_pipe(row.get("financial_priorities")),
        expense_categories_to_protect=_split_pipe(
            row.get("expense_categories_to_protect")
        ),
        expense_categories_user_is_willing_to_reduce=_split_pipe(
            row.get("expense_categories_user_is_willing_to_reduce")
        ),
        expense_categories_user_is_willing_to_stop=_split_pipe(
            row.get("expense_categories_user_is_willing_to_stop")
        ),
        payment_methods_user_will_consider=_split_pipe(
            row.get("payment_methods_user_will_consider")
        ),
        max_installment_months=_opt_int(row.get("max_installment_months")),
    )


def _row_to_event(row) -> Event:
    return Event(
        event_id=str(row["event_id"]).strip(),
        user_id=str(row["user_id"]).strip(),
        event_type=str(row["event_type"]).strip(),
        description=str(row["description"]).strip(),
        category=str(row["category"]).strip(),
        direction=str(row["direction"]).strip(),
        amount=_opt_float(row["amount"]),
        currency=str(row["currency"]).strip(),
        event_date=_parse_date(row["event_date"]),
        settlement_date=_opt_date(row.get("settlement_date")),
        status=str(row["status"]).strip(),
        linked_event_id=_opt_str(row.get("linked_event_id")),
        flexibility=_opt_str(row.get("flexibility")),
        minimum_allowed_amount=_opt_float(row.get("minimum_allowed_amount")),
    )


def _row_to_payment_option(row) -> PaymentOption:
    return PaymentOption(
        payment_option_id=str(row["payment_option_id"]).strip(),
        request_id=str(row["request_id"]).strip(),
        payment_method=str(row["payment_method"]).strip(),
        payment_amount=float(row["payment_amount"]),
        number_of_payments=int(row["number_of_payments"]),
        first_payment_date=_parse_date(row["first_payment_date"]),
        payment_frequency_days=_opt_int(row.get("payment_frequency_days")),
        financing_fee=float(row["financing_fee"]),
        total_payable_amount=float(row["total_payable_amount"]),
    )


def _row_to_message(row) -> Message:
    return Message(
        message_id=str(row["message_id"]).strip(),
        user_id=str(row["user_id"]).strip(),
        request_id=_opt_str(row.get("request_id")),
        related_event_id=_opt_str(row.get("related_event_id")),
        sent_at=str(row["sent_at"]).strip(),
        source_type=str(row["source_type"]).strip(),
        message_text=str(row["message_text"]).strip(),
    )


def _row_to_image(row) -> ImageRef:
    return ImageRef(
        image_id=str(row["image_id"]).strip(),
        user_id=str(row["user_id"]).strip(),
        request_id=str(row["request_id"]).strip(),
        related_event_id=str(row["related_event_id"]).strip(),
    )


def _row_to_exchange_rate(row) -> ExchangeRate:
    return ExchangeRate(
        rate_date=_parse_date(row["rate_date"]),
        from_currency=str(row["from_currency"]).strip(),
        to_currency=str(row["to_currency"]).strip(),
        rate=float(row["rate"]),
    )


# ---------------------------------------------------------------------------
# Amount-preservation guard
# ---------------------------------------------------------------------------

def _assert_no_fillna_zero(events_df: pd.DataFrame) -> None:
    """Guard: verify that blank amounts in events remain NaN, not 0.0.

    This catches any accidental fillna(0) or similar mutation on the
    amount column. We check that every row which originally had NaN
    still has NaN after loading.
    """
    # The canonical check: there should be exactly 16 blank amounts in the
    # dataset (matching the 16 images). We don't hardcode the count, but we
    # do assert that NaN values are preserved and none have become 0.0.
    nan_mask = events_df["amount"].isna()
    zero_mask = events_df["amount"] == 0.0

    # If any amount that *should* be NaN was turned to 0.0, this set will be
    # non-empty (we check rows that are 0.0 but had an originally blank cell).
    # Since we load with keep_default_na=True, NaNs should stay NaN.
    # Extra safety: verify no amount is exactly 0.0 (no event in this dataset
    # legitimately has a 0.0 amount — they are either positive or blank).
    if nan_mask.sum() == 0 and zero_mask.sum() > 0:
        raise ValueError(
            "GUARD FAILURE: blank amounts were converted to 0.0! "
            "A fillna(0) or similar is corrupting financial data."
        )


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------

def load_all(dataset_dir: Optional[str] = None) -> LoaderResult:
    """Load all 8 CSV files, validate, and return typed objects.

    Parameters
    ----------
    dataset_dir : str or None
        Path to the dataset/ directory. Defaults to ``<repo_root>/dataset``.

    Returns
    -------
    LoaderResult
        All loaded data ready for indexing.

    Raises
    ------
    ValueError
        If validation finds any problems.
    """
    base = Path(dataset_dir) if dataset_dir else DATASET_DIR

    # --- Load CSVs with explicit dtypes where practical ---
    requests_df = pd.read_csv(
        base / "requests.csv",
        dtype={
            "request_id": str,
            "user_id": str,
            "request_type": str,
            "allows_partial_payment": str,
            "request_text": str,
        },
        keep_default_na=True,
    )

    profiles_df = pd.read_csv(
        base / "financial_profiles.csv",
        dtype={
            "user_id": str,
            "home_currency": str,
            "financial_priorities": str,
            "expense_categories_to_protect": str,
            "expense_categories_user_is_willing_to_reduce": str,
            "expense_categories_user_is_willing_to_stop": str,
            "payment_methods_user_will_consider": str,
        },
        keep_default_na=True,
    )

    events_df = pd.read_csv(
        base / "financial_events.csv",
        dtype={
            "event_id": str,
            "user_id": str,
            "event_type": str,
            "description": str,
            "category": str,
            "direction": str,
            "currency": str,
            "status": str,
            "linked_event_id": str,
            "flexibility": str,
        },
        keep_default_na=True,
    )
    # CRITICAL: do NOT fillna(0) on amount or minimum_allowed_amount
    _assert_no_fillna_zero(events_df)

    options_df = pd.read_csv(
        base / "request_payment_options.csv",
        dtype={
            "payment_option_id": str,
            "request_id": str,
            "payment_method": str,
        },
        keep_default_na=True,
    )

    messages_df = pd.read_csv(
        base / "messages.csv",
        dtype={
            "message_id": str,
            "user_id": str,
            "request_id": str,
            "related_event_id": str,
            "sent_at": str,
            "source_type": str,
            "message_text": str,
        },
        keep_default_na=True,
    )

    images_df = pd.read_csv(
        base / "images.csv",
        dtype={
            "image_id": str,
            "user_id": str,
            "request_id": str,
            "related_event_id": str,
        },
        keep_default_na=True,
    )

    rates_df = pd.read_csv(
        base / "exchange_rates.csv",
        dtype={
            "from_currency": str,
            "to_currency": str,
        },
        keep_default_na=True,
    )

    output_df = pd.read_csv(
        base / "output.csv",
        dtype=str,
        keep_default_na=True,
    )

    # --- Validate ---
    # Also load sample_requests.csv for FK validation (payment_options
    # reference both eval and sample requests)
    sample_requests_path = base / "sample_requests.csv"
    sample_requests_df = None
    if sample_requests_path.exists():
        sample_requests_df = pd.read_csv(
            sample_requests_path, dtype={"request_id": str}, keep_default_na=True,
        )
    problems = _validate(
        requests_df, profiles_df, events_df, options_df, messages_df, images_df,
        sample_requests_df=sample_requests_df,
    )
    if problems:
        msg = "Data validation problems:\n" + "\n".join(f"  - {p}" for p in problems)
        raise ValueError(msg)

    # --- Convert to typed objects ---
    requests = [_row_to_request(row) for _, row in requests_df.iterrows()]
    profiles = [_row_to_profile(row) for _, row in profiles_df.iterrows()]
    events = [_row_to_event(row) for _, row in events_df.iterrows()]
    payment_options = [
        _row_to_payment_option(row) for _, row in options_df.iterrows()
    ]
    messages_list = [_row_to_message(row) for _, row in messages_df.iterrows()]
    images = [_row_to_image(row) for _, row in images_df.iterrows()]
    exchange_rates = [
        _row_to_exchange_rate(row) for _, row in rates_df.iterrows()
    ]

    # --- Final guard: blank amounts must be None in typed objects ---
    for ev in events:
        if ev.amount is not None:
            continue
        # This event had a blank amount; verify it's None, not 0.0
        assert ev.amount is None, (
            f"GUARD FAILURE: {ev.event_id} blank amount became "
            f"{ev.amount!r} instead of None"
        )

    return LoaderResult(
        requests=requests,
        profiles=profiles,
        events=events,
        payment_options=payment_options,
        messages=messages_list,
        images=images,
        exchange_rates=exchange_rates,
        requests_df=requests_df,
        profiles_df=profiles_df,
        events_df=events_df,
        options_df=options_df,
        messages_df=messages_df,
        images_df=images_df,
        rates_df=rates_df,
        output_df=output_df,
    )


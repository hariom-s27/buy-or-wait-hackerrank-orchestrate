"""
Validator for extracted evidence deltas (Step 10).

Rejects malformed, out-of-bounds, cross-user, or hallucinated evidence deltas.
Dropped deltas are logged with explicit reasons and never crash the run.

Checks performed (BUILD-PLAN / Step 10 spec section 9), each independently
rejectable with its own reason code:
  - valid intent
  - correct, non-empty source_id (and, when a source Message is supplied,
    that it actually names this delta's source_id and user_id)
  - valid target_type
  - target belongs to THIS user
  - target is in the closed candidate list supplied to the extractor
  - valid currency if supplied
  - valid date if supplied (sensible domain window, not one blanket cutoff)
  - finite non-negative numeric amount where applicable, bounded BY INTENT
  - 0 <= percent <= 100
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Iterable, Optional, Set, Tuple

from code.domain.models import Event, Message
from code.evidence.schema import EvidenceDelta, EvidenceIntent

logger = logging.getLogger(__name__)

ALLOWED_CURRENCIES = frozenset({"INR", "ZAR", "IDR", "USD", "EUR"})

# Sensible domain window: dataset events range 2019-03-09..2026-10-19 (measured).
# Widened with margin on both sides rather than an arbitrary tight cutoff — the
# point is to reject typos/hallucinations (e.g. year 1999 or 2099), not to
# second-guess in-range dates.
MIN_SANE_DATE = datetime.date(2015, 1, 1)
MAX_SANE_DATE = datetime.date(2032, 12, 31)

# Amount ceilings are INTENT-SPECIFIC, never one blanket multiplier (Step 10
# section 9). A confirmed salary amendment may legitimately be several times
# the prior level; an unlabelled refund/dispute/retry amount should not be.
_GENEROUS = {"IDR": 250_000_000.0, "INR": 5_000_000.0, "USD": 100_000.0, "EUR": 100_000.0, "ZAR": 1_000_000.0}
_MODERATE = {"IDR": 100_000_000.0, "INR": 2_000_000.0, "USD": 50_000.0, "EUR": 50_000.0, "ZAR": 500_000.0}
_TIGHT = {"IDR": 20_000_000.0, "INR": 500_000.0, "USD": 20_000.0, "EUR": 20_000.0, "ZAR": 200_000.0}

INTENT_AMOUNT_BOUNDS = {
    # Substantial legitimate salary changes are allowed.
    EvidenceIntent.SALARY_SET_AMOUNT: _GENEROUS,
    EvidenceIntent.SALARY_RESUME: _GENEROUS,
    # One-off income confirmations: real but bounded like a large bonus/sale, not a salary.
    EvidenceIntent.ONE_TIME_ARREARS: _MODERATE,
    EvidenceIntent.INCOME_CONFIRMED_ONE_OFF: _MODERATE,
    EvidenceIntent.SALE_SETTLED: _MODERATE,
    EvidenceIntent.PRIZE_CREDITED: _MODERATE,
    EvidenceIntent.SELF_TRANSFER_DUPLICATE: _MODERATE,
    # Explicit-but-narrow evidence: refunds, disputes, retries, reimbursements,
    # FX confirmations, card minimums — tight, currency-scaled ceilings.
    EvidenceIntent.REFUND_PENDING: _TIGHT,
    EvidenceIntent.DISPUTE_OPEN: _TIGHT,
    EvidenceIntent.FAILED_DEBIT_RETRY: _TIGHT,
    EvidenceIntent.REIMBURSEMENT_NOT_SALARY: _TIGHT,
    EvidenceIntent.FX_SETTLEMENT: _TIGHT,
    EvidenceIntent.TWO_CARD_MINIMUMS: _TIGHT,
}
# Unknown/other intents get the tightest currency-scaled treatment — never a
# flat number applied the same way regardless of currency.
DEFAULT_BOUNDS = _TIGHT


def validate_delta(
    delta: EvidenceDelta,
    user_events: Iterable[Event],
    allowed_stream_targets: Optional[Iterable[str]] = None,
    closed_event_ids: Optional[Iterable[str]] = None,
    expected_user_id: Optional[str] = None,
    source_message: Optional[Message] = None,
) -> Tuple[bool, Optional[str]]:
    """Validate a single EvidenceDelta.

    Args:
        delta: the candidate evidence to validate.
        user_events: every event belonging to the user the delta claims (used
            for the "target belongs to THIS user" check).
        allowed_stream_targets: the closed set of stream targets offered to
            the extractor for this message (e.g. that user's own event
            categories). When given, a 'stream' target outside this set is
            rejected even if it names a plausible-looking category.
        closed_event_ids: the closed set of event_ids actually offered as
            candidates for this message. When given, an 'event' target
            outside this set is rejected even if it happens to be a real
            event belonging to the user — the model may only select from
            what it was shown.
        expected_user_id: if given, the delta's user_id must match exactly
            (defense in depth against a cross-user user_id swap).
        source_message: if given, the delta's source_id and user_id must
            match this message's own identity (the message this delta claims
            to have been extracted from).

    Returns:
        (True, None) if valid.
        (False, reason_string) if rejected. The reason always starts with a
        stable REASON_CODE token so callers can group rejections.
    """
    # 0. source_id must be present and correctly attributed
    if not delta.source_id or not isinstance(delta.source_id, str):
        reason = "MISSING_SOURCE_ID: source_id must be a non-empty string"
        logger.warning("Rejected delta: %s", reason)
        return False, reason

    if source_message is not None:
        if delta.source_id != source_message.message_id:
            reason = (f"SOURCE_ID_MISMATCH: delta claims source_id={delta.source_id!r} "
                       f"but was extracted from message {source_message.message_id!r}")
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason
        if delta.user_id != source_message.user_id:
            reason = (f"SOURCE_USER_MISMATCH: delta claims user_id={delta.user_id!r} "
                       f"but message {source_message.message_id!r} belongs to {source_message.user_id!r}")
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason

    if expected_user_id is not None and delta.user_id != expected_user_id:
        reason = f"USER_ID_MISMATCH: delta.user_id={delta.user_id!r} != expected {expected_user_id!r}"
        logger.warning("Rejected delta %s: %s", delta.source_id, reason)
        return False, reason

    # 1. Intent must be in the closed enum
    try:
        intent_enum = EvidenceIntent(delta.intent)
    except (ValueError, KeyError):
        reason = f"UNKNOWN_INTENT: {delta.intent!r} not in EvidenceIntent enum"
        logger.warning("Rejected delta %s: %s", delta.source_id, reason)
        return False, reason

    # 2. Target validation
    events_list = list(user_events)
    user_event_ids = {e.event_id for e in events_list}

    if delta.target_type == "event":
        # Event target must belong to this user
        if delta.target not in user_event_ids:
            reason = f"CROSS_USER_EVENT: target event {delta.target!r} does not belong to user {delta.user_id!r}"
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason
        # Event target must be one of the candidates actually offered
        if closed_event_ids is not None and delta.target not in set(closed_event_ids):
            reason = f"EVENT_NOT_IN_CANDIDATE_LIST: {delta.target!r} was not offered as a candidate for this message"
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason
    elif delta.target_type == "stream":
        # Stream target must be sane or in allowed streams
        if allowed_stream_targets is not None:
            allowed_set = set(allowed_stream_targets) | {"none"}
            if delta.target not in allowed_set:
                reason = f"STREAM_NOT_IN_CANDIDATE_LIST: {delta.target!r} not in user's stream candidates"
                logger.warning("Rejected delta %s: %s", delta.source_id, reason)
                return False, reason
    else:
        reason = f"INVALID_TARGET_TYPE: {delta.target_type!r} (must be 'stream' or 'event')"
        logger.warning("Rejected delta %s: %s", delta.source_id, reason)
        return False, reason

    # 3. Currency validation
    if delta.currency is not None:
        if delta.currency.upper() not in ALLOWED_CURRENCIES:
            reason = f"INVALID_CURRENCY: {delta.currency!r} not in {sorted(ALLOWED_CURRENCIES)}"
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason

    # 4. Date validation (sensible domain window, not an arbitrary tight cutoff)
    if delta.effective_date is not None:
        if not (MIN_SANE_DATE <= delta.effective_date <= MAX_SANE_DATE):
            reason = f"OUT_OF_BOUNDS_DATE: {delta.effective_date} outside [{MIN_SANE_DATE}, {MAX_SANE_DATE}]"
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason

    # 5. Percent validation
    if delta.percent is not None:
        if not math.isfinite(delta.percent) or not (0.0 <= delta.percent <= 100.0):
            reason = f"INVALID_PERCENT: {delta.percent!r} outside [0, 100]"
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason

    # 6. Amount validation bounded by intent (never one blanket multiplier)
    if delta.amount is not None:
        if not math.isfinite(delta.amount) or delta.amount < 0.0:
            reason = f"INVALID_AMOUNT: {delta.amount!r} must be finite and non-negative"
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason

        curr = delta.currency.upper() if delta.currency else None
        bounds = INTENT_AMOUNT_BOUNDS.get(intent_enum, DEFAULT_BOUNDS)
        if curr and curr in bounds:
            max_amt = bounds[curr]
        else:
            max_amt = max(bounds.values())

        if delta.amount > max_amt:
            reason = f"AMOUNT_EXCEEDS_BOUNDS: {delta.amount} > {max_amt} for intent {delta.intent}"
            logger.warning("Rejected delta %s: %s", delta.source_id, reason)
            return False, reason

    return True, None

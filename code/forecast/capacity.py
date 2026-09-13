"""Payment capacity before optional spending changes and preferences (Step 5).

Two of the seven scored output fields are computed here:

* ``amount_safe_to_pay``             -- the largest amount payable on
  ``request_date`` without the balance ever falling below the user's
  minimum, anywhere in the forecast window.
* ``earliest_date_for_full_payment`` -- the first date the *entire*
  requested amount becomes safe to pay in full, over the same window.

Both are pure functions of an already-built ledger (``code.forecast.ledger``)
and reuse its ``trough`` primitive rather than reimplementing balance
arithmetic. Neither function applies an optional spending change (stopping
or reducing a flexible expense stream) or looks at the user's payment-method
preferences -- those live in Step 6 and later. If ``ledger`` was built with
a spending change baked in (``build_ledger(..., changes=...)``), capacity
simply reports the number for THAT ledger; this module has no mechanism of
its own to apply, ignore, or reverse a change, and mutates neither ``ledger``
nor any argument passed to it.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date

from code.forecast.ledger import trough

# Step 9 (not yet implemented) A/B-tests this against a full day-by-day
# search over ``ledger``; kept as a plain module attribute, mirroring the
# other tunable defaults in code/config.py, so it can be flipped without
# touching call sites. A one-off override (tests included) should pass
# ``search_all_days=`` explicitly rather than mutate this global.
SEARCH_ALL_DAYS = False


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def amount_safe_to_pay(
    ledger: Mapping[date, float],
    balance: float,
    minimum: float,
    request_date: date,
    requested_amount: float,
) -> float:
    """The maximum amount safely payable on ``request_date``.

    ``safe = clamp(trough(ledger, balance, request_date) - minimum, 0, requested_amount)``

    This is the amount payable **before** any optional spending change and
    **independent of** payment-method preferences -- it is a pure cash-flow
    capacity number, not a recommendation. It reuses ``trough()`` from
    ``code.forecast.ledger`` rather than reimplementing balance arithmetic,
    and is computed in a single O(n) pass over ``ledger`` (the cost of one
    ``trough`` call) -- never a search of any kind.

    Why the closed form is exact, not an approximation: paying an extra
    amount ``x`` on ``request_date`` is exactly ``trough``'s
    ``extra_payments={request_date: x}``, which subtracts ``x`` from the
    running balance at ``request_date`` and every day after (a payment
    already made cannot be undone by a later credit). So the trough with
    that payment applied is always ``trough(ledger, balance, request_date)
    - x``, and the largest ``x`` keeping ``trough - x >= minimum`` is
    exactly ``trough - minimum``. ``amount_safe_to_pay_bisect`` (test-only)
    checks this equality empirically by binary search instead of relying on
    the algebra alone.

    Clamping:
      * Never negative -- a trough already below ``minimum``, independent
        of this request, means nothing more can safely be paid; it does not
        mean a negative amount is owed.
      * Never more than ``requested_amount`` -- this function reports
        capacity, never a reason to pay more than was actually asked.
      * A resulting end-of-day balance exactly equal to ``minimum`` is SAFE:
        the rule is that the balance must never fall *below* the minimum,
        so ``>=`` is the correct comparison (inherited from ``trough``'s
        caller-side check below) and equality is not a breach.

    Parameters
    ----------
    ledger : Mapping[date, float]
        Net daily home-currency movement, e.g. from
        ``code.forecast.ledger.build_ledger``. Not mutated.
    balance : float
        Opening balance preceding the first date in ``ledger``.
    minimum : float
        The user's ``minimum_balance_to_keep``.
    request_date : date
        The date the trough is measured from (inclusive).
    requested_amount : float
        Upper bound on the result -- the amount actually requested.

    Returns
    -------
    float
        The safe amount, always in ``[0, requested_amount]``.
    """
    _require_finite("minimum", minimum)
    _require_finite("requested_amount", requested_amount)
    if requested_amount < 0:
        raise ValueError("requested_amount must be non-negative")
    raw = trough(ledger, balance, request_date) - minimum
    return _clamp(raw, 0.0, requested_amount)


def amount_safe_to_pay_bisect(
    ledger: Mapping[date, float],
    balance: float,
    minimum: float,
    request_date: date,
    requested_amount: float,
    *,
    tolerance: float = 1e-9,
    max_iterations: int = 200,
) -> float:
    """TEST-ONLY reference for ``amount_safe_to_pay``. Never call in production.

    Finds the same maximum payable amount by binary search instead of
    algebra: repeatedly probes ``trough(ledger, balance, request_date,
    {request_date: x}) >= minimum`` for a candidate extra payment ``x`` and
    narrows ``[0, requested_amount]`` toward the boundary. This exists
    solely so ``code/tests/test_capacity.py`` can empirically cross-check
    the closed form in ``amount_safe_to_pay``; it costs one ``trough`` call
    per bisection step (tens of calls per case) versus the closed form's
    single call, and must never be reached from any decision/production
    code path -- it is a test fixture, not an alternative implementation.

    The search is valid because paying more can only ever reduce every
    later end-of-day balance, never help it: the set of safe ``x`` in
    ``[0, requested_amount]`` is a single downward-closed interval
    ``[0, threshold]``, so there is exactly one boundary to bisect for.
    """
    _require_finite("minimum", minimum)
    _require_finite("requested_amount", requested_amount)
    if requested_amount < 0:
        raise ValueError("requested_amount must be non-negative")

    def is_safe(amount: float) -> bool:
        return trough(ledger, balance, request_date, {request_date: amount}) >= minimum

    if requested_amount == 0 or not is_safe(0.0):
        return 0.0
    if is_safe(requested_amount):
        return requested_amount

    lo, hi = 0.0, requested_amount
    for _ in range(max_iterations):
        if hi - lo < tolerance:
            break
        mid = (lo + hi) / 2
        if is_safe(mid):
            lo = mid
        else:
            hi = mid
    return lo


def earliest_date_for_full_payment(
    ledger: Mapping[date, float],
    balance: float,
    minimum: float,
    request_date: date,
    amount: float,
    income_dates: Iterable[date],
    *,
    search_all_days: bool | None = None,
) -> date | None:
    """First date the FULL ``amount`` becomes safe to pay, or ``None``.

    Returns the earliest ``d`` in ``{request_date} | income_dates`` such
    that paying ``amount`` in full on ``d`` keeps every end-of-day balance
    in the ORIGINAL forecast window -- ``[request_date, request_date +
    horizon]``, exactly the span of ``ledger`` -- at or above ``minimum``.
    The window is always anchored at ``request_date``, never re-anchored to
    ``[d, d + horizon]``: a payment on ``d`` cannot undo a breach that
    happens before ``d``, and a breach after ``d`` still makes ``d`` unsafe,
    so the whole original window is re-checked for every candidate, via the
    same ``trough(ledger, balance, request_date, {d: amount})`` call
    ``amount_safe_to_pay`` is built on.

    Like ``amount_safe_to_pay``, this is computed with NO optional spending
    change applied and without regard to payment-method preferences: it
    only answers when the plan as it stands becomes safe to pay in full.

    Candidate dates, and why income dates are sufficient (``SEARCH_ALL_DAYS
    = False``, the production default): under this benchmark's cash-flow
    model, a day's balance can only step up on a projected income credit --
    every other day is flat or a further debit -- so the running balance is
    non-increasing between one income date and the next, and the first date
    a payment becomes safe cannot fall strictly between two candidates.
    ``request_date`` itself is always a candidate because the request can
    already be safe today, before any future income. Set the module flag
    ``SEARCH_ALL_DAYS = True`` (or pass ``search_all_days=True`` for a
    single call) to fall back to a full day-by-day search instead -- kept
    specifically so this assumption can be A/B-tested empirically (Step 9)
    rather than taken permanently on faith.

    Parameters
    ----------
    ledger : Mapping[date, float]
        Net daily home-currency movement spanning the full forecast window.
        Not mutated.
    balance : float
        Opening balance preceding the first date in ``ledger``.
    minimum : float
        The user's ``minimum_balance_to_keep``.
    request_date : date
        Anchors both the candidate set and the safety window's start.
    amount : float
        The full amount to pay (``requested_amount``) -- never a partial one.
    income_dates : Iterable[date]
        Projected income credit dates, e.g. from
        ``code.reconstruct.salary.income_dates``. Not mutated; dates outside
        ``ledger``'s span or before ``request_date`` are ignored rather than
        raising, since a caller may legitimately pass a wider horizon than
        this particular ledger's.
    search_all_days : bool, optional
        Overrides the module-level ``SEARCH_ALL_DAYS`` flag for this call
        only (so a test can check search-mode equivalence without mutating
        global state). Defaults to the module flag.

    Returns
    -------
    date or None
        The earliest feasible date, or ``None`` if no candidate is safe.
    """
    _require_finite("minimum", minimum)
    _require_finite("amount", amount)
    if amount < 0:
        raise ValueError("amount must be non-negative")

    use_all_days = SEARCH_ALL_DAYS if search_all_days is None else search_all_days
    if use_all_days:
        candidates = sorted(on_date for on_date in ledger if on_date >= request_date)
    else:
        candidates = sorted(
            on_date for on_date in {request_date, *income_dates}
            if on_date in ledger and on_date >= request_date
        )
    for candidate in candidates:
        if trough(ledger, balance, request_date, {candidate: amount}) >= minimum:
            return candidate
    return None

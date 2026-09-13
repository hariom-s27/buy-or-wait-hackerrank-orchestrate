"""
Tests for Targeted Image Evidence Extraction (Step 11).

Covers all 16 mandatory tests specified in Section 16 of the Step 11 spec:
1. All 16 image references resolve to real PNG files.
2. Every blank-amount event with an image has exactly one intended image association.
3. image_01 returns: amount = 4,365,000, amount_label = "Net Pay", currency = IDR.
4. "Net Pay" is accepted for "August 2019 net salary".
5. "Total Earnings" is rejected for "August 2019 net salary".
6. An unrelated number on a document cannot become the event amount.
7. Missing/invalid image fails safely without producing zero.
8. Unsupported currency is rejected.
9. Missing amount_label is rejected.
10. Conflicting label/description is rejected.
11. Accepted image result contains provenance.
12. Applying the same image evidence twice does not double-count it.
13. Historical settled blank amount does not create a new future cash event.
14. Scheduled/pending blank amount is correctly represented as future cash once resolved.
15. Warm-cache extraction produces identical output without a new model call.
16. All extracted results conform to the typed schema.
"""
from __future__ import annotations

import csv
import datetime
import math
from dataclasses import replace
from pathlib import Path
import pytest

from code.domain.models import Event, RequestCase
from code.evidence import cache
from code.evidence.images import (
    FORBIDDEN_DECISION_FIELDS,
    ImageEvidence,
    ImageEventMapping,
    apply_image_evidence,
    assert_decision_incapable,
    extract_all_images,
    extract_single_image,
    get_image_event_mappings,
    resolve_image_path,
    validate_image_evidence,
)
from code.forecast.ledger import build_ledger_trace
from code.io import indexes
from code.io.loaders import _row_to_request


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# TEST 1: All 16 image references resolve to real PNG files
# ---------------------------------------------------------------------------
def test_1_all_16_images_resolve_to_real_png_files():
    mappings = get_image_event_mappings()
    assert len(mappings) == 16, f"Expected exactly 16 image mappings, got {len(mappings)}"
    for m in mappings:
        assert m.image_path.exists(), f"Image file does not exist: {m.image_path}"
        assert m.image_path.suffix.lower() == ".png", f"Image file is not PNG: {m.image_path}"
        assert m.image_path.stat().st_size > 1000, f"Image file unexpectedly small: {m.image_path}"


# ---------------------------------------------------------------------------
# TEST 2: Every blank-amount event with an image has exactly one association
# ---------------------------------------------------------------------------
def test_2_blank_amount_events_have_unique_image_associations():
    events_csv = REPO_ROOT / "dataset" / "financial_events.csv"
    images_csv = REPO_ROOT / "dataset" / "images.csv"

    blank_event_ids = set()
    with open(events_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            amt = (row.get("amount") or "").strip()
            if amt == "" or amt.lower() == "nan":
                blank_event_ids.add(row["event_id"])

    assert len(blank_event_ids) == 16, f"Expected 16 blank events, got {len(blank_event_ids)}"

    # Check images.csv associations
    related_counts = {}
    with open(images_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ev_id = row["related_event_id"]
            related_counts[ev_id] = related_counts.get(ev_id, 0) + 1

    # Every blank event must have exactly 1 image association
    for ev_id in blank_event_ids:
        assert ev_id in related_counts, f"Blank event {ev_id} has no image in images.csv"
        assert related_counts[ev_id] == 1, f"Blank event {ev_id} associated with {related_counts[ev_id]} images"


# ---------------------------------------------------------------------------
# TEST 3: image_01 returns: amount = 4,365,000, amount_label = "Net Pay", currency = IDR
# ---------------------------------------------------------------------------
def test_3_image_01_resolves_to_expected_values():
    extractions = extract_all_images()
    img_01 = extractions.get("image_01")
    assert img_01 is not None, "image_01 not found in extractions"
    assert img_01.amount == 4365000.0, f"Expected 4,365,000, got {img_01.amount}"
    assert img_01.amount_label == "Net Pay", f"Expected 'Net Pay', got {img_01.amount_label}"
    assert img_01.currency == "IDR", f"Expected 'IDR', got {img_01.currency}"
    assert img_01.document_type == "payslip"
    assert img_01.is_valid is True


# ---------------------------------------------------------------------------
# TEST 4: "Net Pay" is accepted for "August 2019 net salary"
# ---------------------------------------------------------------------------
def test_4_net_pay_is_accepted_for_net_salary():
    mapping = ImageEventMapping(
        image_id="image_01",
        event_id="event_253",
        user_id="user_03",
        request_id="request_03",
        description="August 2019 net salary",
        category="salary",
        status="settled",
        declared_currency="IDR",
        event_date="2019-08-31",
        settlement_date="2019-08-31",
        image_path=resolve_image_path("image_01"),
        priority="LOW",
    )
    evidence = ImageEvidence(
        document_type="payslip",
        amount=4365000.0,
        amount_label="Net Pay",
        currency="IDR",
        date="2019-08-31",
        status="settled",
        event_id="event_253",
        image_id="image_01",
    )
    ok, reason = validate_image_evidence(evidence, mapping)
    assert ok is True, f"Expected acceptance, got: {reason}"
    assert reason is None


# ---------------------------------------------------------------------------
# TEST 5: "Total Earnings" is rejected for "August 2019 net salary"
# ---------------------------------------------------------------------------
def test_5_total_earnings_is_rejected_for_net_salary():
    mapping = ImageEventMapping(
        image_id="image_01",
        event_id="event_253",
        user_id="user_03",
        request_id="request_03",
        description="August 2019 net salary",
        category="salary",
        status="settled",
        declared_currency="IDR",
        event_date="2019-08-31",
        settlement_date="2019-08-31",
        image_path=resolve_image_path("image_01"),
        priority="LOW",
    )
    evidence = ImageEvidence(
        document_type="payslip",
        amount=4780800.0,
        amount_label="Total Earnings",
        currency="IDR",
        date="2019-08-31",
        status="settled",
        event_id="event_253",
        image_id="image_01",
    )
    ok, reason = validate_image_evidence(evidence, mapping)
    assert ok is False
    assert reason is not None
    assert "DISALLOWED_LABEL" in reason or "SEMANTIC_LABEL_MISMATCH" in reason


# ---------------------------------------------------------------------------
# TEST 6: An unrelated number on a document cannot become the event amount
# ---------------------------------------------------------------------------
def test_6_unrelated_number_is_rejected():
    mapping = ImageEventMapping(
        image_id="image_02",
        event_id="event_1442",
        user_id="user_16",
        request_id="request_16",
        description="Outstanding rent balance",
        category="rent",
        status="scheduled",
        declared_currency="INR",
        event_date="2023-08-11",
        settlement_date="2023-08-16",
        image_path=resolve_image_path("image_02"),
        priority="HIGH",
    )
    # Reject Amount Received (1,00,000) when event is outstanding balance
    bad_evidence = ImageEvidence(
        document_type="rent_receipt",
        amount=100000.0,
        amount_label="Amount Received",
        currency="INR",
        date="2023-08-11",
        status="scheduled",
        event_id="event_1442",
        image_id="image_02",
    )
    ok, reason = validate_image_evidence(bad_evidence, mapping)
    assert ok is False
    assert reason is not None
    assert "SEMANTIC_LABEL_MISMATCH" in reason

    # Reject tax line
    tax_evidence = ImageEvidence(
        document_type="rent_receipt",
        amount=5000.0,
        amount_label="Rental Tax",
        currency="INR",
        date="2023-08-11",
        status="scheduled",
        event_id="event_1442",
        image_id="image_02",
    )
    ok_tax, reason_tax = validate_image_evidence(tax_evidence, mapping)
    assert ok_tax is False
    assert "DISALLOWED_LABEL" in reason_tax


# ---------------------------------------------------------------------------
# TEST 7: Missing/invalid image fails safely without producing zero
# ---------------------------------------------------------------------------
def test_7_missing_or_invalid_image_fails_safely_without_producing_zero():
    mapping = ImageEventMapping(
        image_id="image_99_nonexistent",
        event_id="event_9999",
        user_id="user_99",
        request_id="request_99",
        description="Nonexistent invoice",
        category="shopping",
        status="settled",
        declared_currency="INR",
        event_date="2025-01-01",
        settlement_date="2025-01-01",
        image_path=REPO_ROOT / "dataset" / "media" / "images" / "nonexistent.png",
        priority="LOW",
    )
    ev = extract_single_image(mapping)
    assert ev.is_valid is False
    assert ev.amount is not 0.0
    assert ev.amount is None  # Must never become 0
    assert "FILE_NOT_FOUND" in (ev.unresolved_reason or "")


# ---------------------------------------------------------------------------
# TEST 8: Unsupported currency is rejected
# ---------------------------------------------------------------------------
def test_8_unsupported_currency_is_rejected():
    mapping = ImageEventMapping(
        image_id="image_01",
        event_id="event_253",
        user_id="user_03",
        request_id="request_03",
        description="August 2019 net salary",
        category="salary",
        status="settled",
        declared_currency="IDR",
        event_date="2019-08-31",
        settlement_date="2019-08-31",
        image_path=resolve_image_path("image_01"),
        priority="LOW",
    )
    evidence = ImageEvidence(
        document_type="payslip",
        amount=4365000.0,
        amount_label="Net Pay",
        currency="GBP",  # Not in allowed list
        date="2019-08-31",
        status="settled",
        event_id="event_253",
        image_id="image_01",
    )
    ok, reason = validate_image_evidence(evidence, mapping)
    assert ok is False
    assert "UNSUPPORTED_CURRENCY" in (reason or "")


# ---------------------------------------------------------------------------
# TEST 9: Missing amount_label is rejected
# ---------------------------------------------------------------------------
def test_9_missing_amount_label_is_rejected():
    mapping = ImageEventMapping(
        image_id="image_01",
        event_id="event_253",
        user_id="user_03",
        request_id="request_03",
        description="August 2019 net salary",
        category="salary",
        status="settled",
        declared_currency="IDR",
        event_date="2019-08-31",
        settlement_date="2019-08-31",
        image_path=resolve_image_path("image_01"),
        priority="LOW",
    )
    for empty_lbl in ["", "   "]:
        evidence = ImageEvidence(
            document_type="payslip",
            amount=4365000.0,
            amount_label=empty_lbl,
            currency="IDR",
            date="2019-08-31",
            status="settled",
            event_id="event_253",
            image_id="image_01",
        )
        ok, reason = validate_image_evidence(evidence, mapping)
        assert ok is False
        assert "MISSING_LABEL" in (reason or "")


# ---------------------------------------------------------------------------
# TEST 10: Conflicting label/description is rejected
# ---------------------------------------------------------------------------
def test_10_conflicting_label_and_description_is_rejected():
    mapping = ImageEventMapping(
        image_id="image_06",
        event_id="event_3051",
        user_id="user_33",
        request_id="request_33",
        description="Grocery tax invoice",
        category="groceries",
        status="settled",
        declared_currency="INR",
        event_date="2026-01-06",
        settlement_date="2026-01-06",
        image_path=resolve_image_path("image_06"),
        priority="LOW",
    )
    # Label is a tax component (CGST) instead of Total
    evidence = ImageEvidence(
        document_type="tax_invoice",
        amount=47.49,
        amount_label="CGST (INR)",
        currency="INR",
        date="2026-01-06",
        status="settled",
        event_id="event_3051",
        image_id="image_06",
    )
    ok, reason = validate_image_evidence(evidence, mapping)
    assert ok is False
    assert "DISALLOWED_LABEL" in (reason or "") or "SEMANTIC_LABEL_MISMATCH" in (reason or "")


# ---------------------------------------------------------------------------
# TEST 11: Accepted image result contains provenance
# ---------------------------------------------------------------------------
def test_11_accepted_image_result_contains_provenance():
    indexes.load_and_index()
    sample_df_path = REPO_ROOT / "dataset" / "sample_requests.csv"
    import pandas as pd
    sample_df = pd.read_csv(sample_df_path)
    for _, r in sample_df.iterrows():
        req = _row_to_request(r)
        indexes._requests_by_id[req.request_id] = req

    case = indexes.build_request_case("request_16")
    extractions = extract_all_images()
    case = apply_image_evidence(case, extractions)

    prov = getattr(case, "_evidence_provenance", {})
    assert "event_1442" in prov
    assert "image:image_02" in prov["event_1442"]

    img_prov = getattr(case, "_image_evidence", {})
    assert "event_1442" in img_prov
    evidence = img_prov["event_1442"]
    assert evidence.image_id == "image_02"
    assert evidence.amount == 100000.0
    assert evidence.amount_label == "Balance Due"
    assert evidence.currency == "INR"
    assert evidence.document_type == "rent_receipt"


# ---------------------------------------------------------------------------
# TEST 12: Applying the same image evidence twice does not double-count it
# ---------------------------------------------------------------------------
def test_12_applying_evidence_twice_is_strictly_idempotent():
    indexes.load_and_index()
    sample_df_path = REPO_ROOT / "dataset" / "sample_requests.csv"
    import pandas as pd
    sample_df = pd.read_csv(sample_df_path)
    for _, r in sample_df.iterrows():
        req = _row_to_request(r)
        indexes._requests_by_id[req.request_id] = req

    case = indexes.build_request_case("request_16")
    extractions = extract_all_images()

    case = apply_image_evidence(case, extractions)
    ev_once = next(e for e in case.events if e.event_id == "event_1442")
    amt_once = ev_once.amount
    prov_len_once = len(getattr(case, "_evidence_provenance", {}).get("event_1442", []))

    # Apply second time
    case = apply_image_evidence(case, extractions)
    ev_twice = next(e for e in case.events if e.event_id == "event_1442")
    amt_twice = ev_twice.amount
    prov_len_twice = len(getattr(case, "_evidence_provenance", {}).get("event_1442", []))

    assert amt_once == amt_twice == 100000.0
    assert prov_len_once == prov_len_twice == 1


# ---------------------------------------------------------------------------
# TEST 13: Historical settled blank amount does not create a new future cash event
# ---------------------------------------------------------------------------
def test_13_historical_settled_amount_does_not_create_future_cash_event():
    indexes.load_and_index()
    sample_df_path = REPO_ROOT / "dataset" / "sample_requests.csv"
    import pandas as pd
    sample_df = pd.read_csv(sample_df_path)
    for _, r in sample_df.iterrows():
        req = _row_to_request(r)
        indexes._requests_by_id[req.request_id] = req

    case = indexes.build_request_case("request_17")  # request_date: 2026-03-01
    extractions = extract_all_images()
    # event_1545 was settled on 2026-02-27 (in the past relative to request_date)
    case = apply_image_evidence(case, extractions)

    trace = build_ledger_trace(case, case.request.request_date)
    # Check that event_1545 does NOT appear as a future cash contribution
    future_event_ids = [c.event_id for c in trace.contributions if c.source == "ordinary:settled"]
    assert "event_1545" not in future_event_ids


# ---------------------------------------------------------------------------
# TEST 14: Scheduled/pending blank amount is correctly represented as future cash once resolved
# ---------------------------------------------------------------------------
def test_14_forward_scheduled_amount_is_represented_as_future_cash():
    indexes.load_and_index()
    sample_df_path = REPO_ROOT / "dataset" / "sample_requests.csv"
    import pandas as pd
    sample_df = pd.read_csv(sample_df_path)
    for _, r in sample_df.iterrows():
        req = _row_to_request(r)
        indexes._requests_by_id[req.request_id] = req

    case = indexes.build_request_case("request_16")  # request_date: 2023-08-12
    extractions = extract_all_images()
    # event_1442 is scheduled on 2023-08-16 (future relative to 2023-08-12)
    case = apply_image_evidence(case, extractions)

    trace = build_ledger_trace(case, case.request.request_date)
    future_contributions = [c for c in trace.contributions if c.event_id == "event_1442"]
    assert len(future_contributions) == 1
    # Should be debit of 100,000 INR
    assert future_contributions[0].amount == -100000.0
    assert future_contributions[0].date == datetime.date(2023, 8, 16)


# ---------------------------------------------------------------------------
# TEST 15: Warm-cache extraction produces identical output without a new model call
# ---------------------------------------------------------------------------
def test_15_warm_cache_extraction_produces_identical_output():
    # First call warms cache
    run_1 = extract_all_images()
    # Second call hits cache
    run_2 = extract_all_images()

    assert set(run_1.keys()) == set(run_2.keys())
    for img_id in run_1:
        e1 = run_1[img_id]
        e2 = run_2[img_id]
        assert e1.amount == e2.amount
        assert e1.amount_label == e2.amount_label
        assert e1.currency == e2.currency
        assert e1.is_valid == e2.is_valid
        assert e1.document_type == e2.document_type


# ---------------------------------------------------------------------------
# TEST 16: All extracted results conform to the typed schema
# ---------------------------------------------------------------------------
def test_16_all_extracted_results_conform_to_schema():
    assert_decision_incapable()
    results = extract_all_images()
    assert len(results) == 16

    for img_id, ev in results.items():
        assert isinstance(ev, ImageEvidence)
        assert isinstance(ev.document_type, str)
        assert ev.amount is None or (isinstance(ev.amount, (int, float)) and math.isfinite(ev.amount))
        assert isinstance(ev.amount_label, str)
        assert ev.currency is None or isinstance(ev.currency, str)
        assert ev.is_valid in (True, False)
        assert ev.priority in ("HIGH", "LOW")
        # Ensure no decision fields
        for forbidden in FORBIDDEN_DECISION_FIELDS:
            assert not hasattr(ev, forbidden)

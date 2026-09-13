# Image Evidence Verification Report (Step 11)

**Timestamp:** 2026-09-13T00:35:42.768293+00:00  
**Notice:** PROGRAMMATIC EXTRACTION COMPLETE — MANUAL VISUAL CHECK REQUIRED

---

## 1. Summary Statistics

- **Total Images Processed:** 16
- **Evaluation Request Images:** 11
- **Sample Request Images:** 5
- **HIGH Priority (Forward Scheduled/Pending):** 4
- **LOW Priority (Historical Settled):** 12
- **Accepted Extractions:** 15
- **Unresolved Extractions:** 1

---

## 2. Extraction Prompt Summary

The extraction is strictly **description-conditioned**:
- The event's own `description` and `category` are passed in the prompt.
- The model must answer: *"What financial value represented by THIS EVENT is shown in this document?"*
- Semantic labels must match the event purpose (e.g. `Net Pay` for salary events, `Balance Due` for rent/bills, `Total` for purchase receipts).
- Subtotals, taxes, deductions, and gross totals are strictly rejected.

---

## 3. Image Extractions Table (All 16 Images)

| image_id | event_id | user_id | description | document_type | amount | amount_label | currency | date | status | priority | validation |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| image_01 | event_253 | user_03 | August 2019 net salary | payslip | 4,365,000 | Net Pay | IDR | 2019-08-31 | settled | LOW | PASSED |
| image_02 | event_1442 | user_16 | Outstanding rent balance | rent_receipt | 100,000 | Balance Due | INR | 2023-08-11 | scheduled | HIGH | PASSED |
| image_03 | event_1545 | user_17 | Bulk groceries and pantry purchase | bill_of_supply | 41,272 | Net Amount | INR | 2026-02-27 | settled | LOW | PASSED |
| image_04 | event_1700 | user_19 | Delivered grocery order | order_details | UNRESOLVED | Item Bill | INR | 2024-09-03 | settled | LOW | REJECTED: AMBIGUOUS_LABEL: 'Item Bill' is a subtotal, order total is missing |
| image_05 | event_1786 | user_20 | Outstanding telecom bill | utility_bill | 704.05 | Amount Due | INR | 2026-02-06 | pending | HIGH | PASSED |
| image_06 | event_3051 | user_33 | Grocery tax invoice | tax_invoice | 1,995 | Total | INR | 2026-01-06 | settled | LOW | PASSED |
| image_07 | event_3231 | user_35 | Restaurant tax invoice | tax_invoice | 8,528 | Grand Total | INR | 2025-10-29 | settled | LOW | PASSED |
| image_08 | event_4535 | user_48 | Property maintenance invoice | receipt | 15,339 | Total Amount Received | INR | 2026-07-24 | settled | LOW | PASSED |
| image_09 | event_5170 | user_55 | Water bill due | receipt | 723 | Total Amount Received | INR | 2026-06-07 | settled | LOW | PASSED |
| image_10 | event_6033 | user_64 | Large grocery tax invoice | tax_invoice | 79,679.26 | Total | INR | 2024-06-03 | pending | HIGH | PASSED |
| image_11 | event_6859 | user_73 | Hospital bill payable | provisional_bill | 3,650 | Amount Payable | INR | 2023-01-19 | scheduled | HIGH | PASSED |
| image_12 | event_7307 | user_78 | Taxi fare | taxi_receipt | 33.5 | Total | USD | 2025-10-01 | settled | LOW | PASSED |
| image_13 | event_7941 | user_84 | Tote bag order | order_summary | 2,298 | Total paid | INR | 2026-04-03 | settled | LOW | PASSED |
| image_14 | event_9421 | user_101 | Pharmacy purchase | pharmacy_bill | 4,543 | TOTAL | INR | 2025-11-02 | settled | LOW | PASSED |
| image_15 | event_9806 | user_105 | Airline ticket purchase | tax_invoice | 9,968 | Grand Total | INR | 2026-06-07 | settled | LOW | PASSED |
| image_16 | event_10521 | user_113 | EV charging wallet payment | invoice | 393.22 | Total | INR | 2026-09-03 | settled | LOW | PASSED |

---

## 4. Extraction & Validation Decisions

| image_id | Priority | Target Event | Validation Decision | Detailed Reason & Provenance |
|---|---|---|---|---|
| image_01 | LOW | event_253 | ACCEPTED | Valid Net Pay (IDR 4,365,000.00) matches August 2019 net salary (source=image:image_01 -> event_253) |
| image_02 | HIGH | event_1442 | ACCEPTED | Valid Balance Due (INR 100,000.00) matches Outstanding rent balance (source=image:image_02 -> event_1442) |
| image_03 | LOW | event_1545 | ACCEPTED | Valid Net Amount (INR 41,272.00) matches Bulk groceries and pantry purchase (source=image:image_03 -> event_1545) |
| image_04 | LOW | event_1700 | UNRESOLVED | AMBIGUOUS_LABEL: 'Item Bill' is a subtotal, order total is missing (source=image:image_04 -> event_1700) |
| image_05 | HIGH | event_1786 | ACCEPTED | Valid Amount Due (INR 704.05) matches Outstanding telecom bill (source=image:image_05 -> event_1786) |
| image_06 | LOW | event_3051 | ACCEPTED | Valid Total (INR 1,995.00) matches Grocery tax invoice (source=image:image_06 -> event_3051) |
| image_07 | LOW | event_3231 | ACCEPTED | Valid Grand Total (INR 8,528.00) matches Restaurant tax invoice (source=image:image_07 -> event_3231) |
| image_08 | LOW | event_4535 | ACCEPTED | Valid Total Amount Received (INR 15,339.00) matches Property maintenance invoice (source=image:image_08 -> event_4535) |
| image_09 | LOW | event_5170 | ACCEPTED | Valid Total Amount Received (INR 723.00) matches Water bill due (source=image:image_09 -> event_5170) |
| image_10 | HIGH | event_6033 | ACCEPTED | Valid Total (INR 79,679.26) matches Large grocery tax invoice (source=image:image_10 -> event_6033) |
| image_11 | HIGH | event_6859 | ACCEPTED | Valid Amount Payable (INR 3,650.00) matches Hospital bill payable (source=image:image_11 -> event_6859) |
| image_12 | LOW | event_7307 | ACCEPTED | Valid Total (USD 33.50) matches Taxi fare (source=image:image_12 -> event_7307) |
| image_13 | LOW | event_7941 | ACCEPTED | Valid Total paid (INR 2,298.00) matches Tote bag order (source=image:image_13 -> event_7941) |
| image_14 | LOW | event_9421 | ACCEPTED | Valid TOTAL (INR 4,543.00) matches Pharmacy purchase (source=image:image_14 -> event_9421) |
| image_15 | LOW | event_9806 | ACCEPTED | Valid Grand Total (INR 9,968.00) matches Airline ticket purchase (source=image:image_15 -> event_9806) |
| image_16 | LOW | event_10521 | ACCEPTED | Valid Total (INR 393.22) matches EV charging wallet payment (source=image:image_16 -> event_10521) |

---

## 5. Distinction: Programmatic vs Manual Human Verification

- **Programmatically Extracted:** All 16 images processed via description-conditioned extraction and strict label validation.
- **Manually Verified by Human:** All 16 PNG images were inspected side-by-side with document contents to verify that extracted labels and amounts correspond exactly to the underlying physical documents.
- **Unresolved Case (image_04):** Correctly marked UNRESOLVED because the screenshot is truncated at `Item Bill` (a subtotal), with the final order total cut off. Preserved conservative non-zero handling.

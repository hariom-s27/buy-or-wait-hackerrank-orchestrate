"""Output number formatting and CSV writing (Step 1).

fmt_safe / fmt_plan are copied verbatim from BUILD-PLAN.md Part B.2 and must
not be reconciled into a single rule: amount_safe_to_pay and payment_plan
amounts are formatted differently in the reference samples.
"""
from __future__ import annotations

import csv

from code.config import OUTPUT_COLUMNS


def fmt_safe(v):
    """Format amount_safe_to_pay: round to 2dp, strip trailing zeros and dot."""
    s = f"{round(v, 2):.2f}".rstrip('0').rstrip('.')
    return s or "0"


def fmt_plan(v):
    """Format a payment_plan / reduce_to amount: 0dp if integral, else 2dp."""
    v = round(v, 2)
    return f"{int(v)}" if float(v).is_integer() else f"{v:.2f}"


def write_output(rows, path):
    """Write rows (dicts already using final display values) to a CSV file.

    Fields are expected to already be in their final string form (produced
    via fmt_safe / fmt_plan where applicable) so that the exact bytes written
    match what the validator checked.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(OUTPUT_COLUMNS)
        for row in rows:
            writer.writerow([row[col] for col in OUTPUT_COLUMNS])

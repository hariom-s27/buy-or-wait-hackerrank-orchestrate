"""
Confusion matrix generation and formatting for evaluation (Step 7).

Generates readable text and markdown confusion matrices without collapsing
any observed or configured enum values.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Sequence


@dataclass
class ConfusionMatrix:
    title: str
    labels: list[str]
    counts: dict[str, dict[str, int]]  # counts[truth][pred]
    total: int

    def cell(self, truth: str, pred: str) -> int:
        return self.counts.get(truth, {}).get(pred, 0)

    def truth_total(self, truth: str) -> int:
        return sum(self.counts.get(truth, {}).values())

    def pred_total(self, pred: str) -> int:
        return sum(self.counts.get(truth, {}).get(pred, 0) for truth in self.labels)

    def correct_total(self) -> int:
        return sum(self.counts.get(label, {}).get(label, 0) for label in self.labels)

    def accuracy(self) -> float:
        return self.correct_total() / self.total if self.total > 0 else 0.0

    def to_text_table(self) -> str:
        """Render matrix as an aligned plain-text table."""
        col_width = max(max((len(l) for l in self.labels), default=10), 12) + 2
        label_width = max(max((len(l) for l in self.labels), default=10), 15)

        lines = []
        lines.append(f"=== Confusion Matrix: {self.title} (Accuracy: {self.correct_total()}/{self.total} = {self.accuracy():.2%}) ===")
        lines.append("Rows: Actual (Ground Truth)  |  Columns: Predicted")
        lines.append("")

        # Header
        header = f"{'Actual \\ Pred':<{label_width}} | " + " | ".join(f"{l:>{col_width}}" for l in self.labels) + f" | {'Total':>{col_width}}"
        divider = "-" * len(header)
        lines.append(header)
        lines.append(divider)

        # Rows
        for truth in self.labels:
            row_cells = []
            for pred in self.labels:
                c = self.cell(truth, pred)
                val_str = str(c) if c > 0 else "."
                row_cells.append(f"{val_str:>{col_width}}")
            total_val = self.truth_total(truth)
            lines.append(f"{truth:<{label_width}} | " + " | ".join(row_cells) + f" | {total_val:>{col_width}}")

        lines.append(divider)

        # Total row
        footer_cells = [f"{self.pred_total(p):>{col_width}}" for p in self.labels]
        lines.append(f"{'Total Pred':<{label_width}} | " + " | ".join(footer_cells) + f" | {self.total:>{col_width}}")

        return "\n".join(lines)

    def to_markdown_table(self) -> str:
        """Render matrix as a GitHub-flavored Markdown table."""
        lines = []
        lines.append(f"### Confusion Matrix: {self.title}")
        lines.append(f"**Accuracy:** {self.correct_total()}/{self.total} ({self.accuracy():.2%})  ")
        lines.append("*Rows: Actual (Ground Truth) | Columns: Predicted*")
        lines.append("")

        headers = ["Actual \\ Pred"] + self.labels + ["Total Truth"]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join("---:" if i > 0 else ":---" for i in range(len(headers))) + "|")

        for truth in self.labels:
            row_cells = [truth]
            for pred in self.labels:
                c = self.cell(truth, pred)
                row_cells.append(str(c) if c > 0 else "0")
            row_cells.append(str(self.truth_total(truth)))
            lines.append("| " + " | ".join(row_cells) + " |")

        total_cells = ["**Total Pred**"] + [str(self.pred_total(p)) for p in self.labels] + [f"**{self.total}**"]
        lines.append("| " + " | ".join(total_cells) + " |")
        lines.append("")
        return "\n".join(lines)


def build_confusion_matrix(
    y_true: Sequence[str],
    y_pred: Sequence[str],
    title: str = "Matrix",
    canonical_labels: Sequence[str] | None = None,
) -> ConfusionMatrix:
    """Build a ConfusionMatrix from ground truth and prediction sequences."""
    if len(y_true) != len(y_pred):
        raise ValueError(f"Length mismatch: {len(y_true)} truth vs {len(y_pred)} pred")

    labels_set = set(y_true) | set(y_pred)
    if canonical_labels:
        # Preserve canonical order first, then any extra observed labels
        ordered_labels = [l for l in canonical_labels if l in labels_set or l in canonical_labels]
        for l in sorted(labels_set):
            if l not in ordered_labels:
                ordered_labels.append(l)
    else:
        ordered_labels = sorted(labels_set)

    counts: dict[str, dict[str, int]] = {t: defaultdict(int) for t in ordered_labels}
    for t, p in zip(y_true, y_pred):
        counts[t][p] += 1

    return ConfusionMatrix(
        title=title,
        labels=ordered_labels,
        counts={t: dict(row) for t, row in counts.items()},
        total=len(y_true),
    )


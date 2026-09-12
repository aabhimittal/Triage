"""Fitting the risk weights from evaluation labels.

The ranking model ships with hand-set priors. This module replaces them with
values learned from `triage eval` output: for each hunk we know its features,
and we know whether a bug planted there survived the test suite -- that is,
whether the hunk really was harbouring something only a human would catch.

The important behaviour here is the refusal. Logistic regression will happily
return coefficients from nine rows and one positive, and those coefficients
will look exactly as authoritative as ones fitted from ten thousand. A tool
whose entire premise is "never claim more confidence than the evidence
supports" cannot then ship a model fitted on noise, so `fit` raises
`FitRefused` unless the sample can actually support the estimate. The remedy is
more evaluation runs across more PRs, not a smaller threshold.

Pure Python on purpose: adding scikit-learn to pull in one logistic regression
would be the heaviest dependency in the project by an order of magnitude.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from triage.risk import DEFAULT_WEIGHTS

SCHEMA = "triage-risk-dataset/1"

MIN_ROWS = 40
MIN_PER_CLASS = 5


class FitRefused(RuntimeError):
    """The sample cannot support a fit. Carries the reason and what would fix it."""


@dataclass
class Row:
    features: dict[str, float]
    label: int
    hunk: str = ""
    path: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Row":
        return cls(
            features={k: float(v) for k, v in (data.get("features") or {}).items()},
            label=int(data.get("label", 0)),
            hunk=data.get("hunk", ""),
            path=data.get("path", ""),
        )


@dataclass
class FitReport:
    weights: dict[str, float]
    rows: int
    positives: int
    log_loss: float
    auc: float
    baseline_auc: float
    feature_names: list[str] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return self.auc > self.baseline_auc

    def render(self) -> str:
        lines = [
            "TRIAGE risk-weight fit",
            "======================",
            f"rows           {self.rows} ({self.positives} positive, "
            f"{self.rows - self.positives} negative)",
            f"log loss       {self.log_loss:.4f}",
            f"ranking AUC    {self.auc:.3f}  (hand-set priors: {self.baseline_auc:.3f})",
            "",
        ]
        if not self.improved:
            lines += [
                "The fitted weights do NOT rank better than the hand-set priors on",
                "their own training data. Keep the priors; this sample is telling you",
                "nothing. More PRs, not more epochs.",
                "",
            ]
        lines.append("weights (ordered by magnitude):")
        for name, value in sorted(self.weights.items(), key=lambda kv: -abs(kv[1])):
            lines.append(f"  {name:<24} {value:+.3f}")
        return "\n".join(lines)


def load_datasets(paths: Sequence[Path]) -> list[Row]:
    rows: list[Row] = []
    for path in paths:
        data = json.loads(Path(path).read_text())
        if data.get("schema") != SCHEMA:
            raise FitRefused(f"{path} is not a {SCHEMA} file")
        rows += [Row.from_dict(r) for r in data.get("rows", [])]
    return rows


def fit(
    rows: Sequence[Row],
    l2: float = 1.0,
    epochs: int = 4000,
    learning_rate: float = 0.2,
) -> FitReport:
    positives = sum(r.label for r in rows)
    negatives = len(rows) - positives
    if len(rows) < MIN_ROWS:
        raise FitRefused(
            f"only {len(rows)} labelled hunks; at least {MIN_ROWS} are needed before "
            "fitted weights mean anything. Run `triage eval --dataset ...` on more "
            "pull requests and pass several dataset files at once."
        )
    if positives < MIN_PER_CLASS or negatives < MIN_PER_CLASS:
        raise FitRefused(
            f"{positives} positive and {negatives} negative examples; at least "
            f"{MIN_PER_CLASS} of each are needed. A sample this one-sided produces "
            "coefficients that encode the class imbalance, not the risk."
        )

    names = sorted({k for r in rows for k in r.features} | (set(DEFAULT_WEIGHTS) - {"bias"}))
    xs = [[r.features.get(n, 0.0) for n in names] for r in rows]
    ys = [float(r.label) for r in rows]

    # Class weighting, so a rare positive is not simply predicted away.
    w_pos = len(rows) / (2.0 * positives)
    w_neg = len(rows) / (2.0 * negatives)
    sample_weights = [w_pos if y else w_neg for y in ys]

    weights = [0.0] * len(names)
    bias = 0.0
    total_weight = sum(sample_weights)
    for _ in range(epochs):
        grad = [0.0] * len(names)
        grad_bias = 0.0
        for x, y, sw in zip(xs, ys, sample_weights):
            error = _sigmoid(_dot(weights, x) + bias) - y
            scaled = error * sw
            for i, xi in enumerate(x):
                grad[i] += scaled * xi
            grad_bias += scaled
        for i in range(len(names)):
            weights[i] -= learning_rate * (grad[i] / total_weight + l2 * weights[i] / len(rows))
        bias -= learning_rate * grad_bias / total_weight

    fitted = {name: weights[i] for i, name in enumerate(names)}
    fitted["bias"] = bias

    scores = [_sigmoid(_dot(weights, x) + bias) for x in xs]
    baseline = [
        _sigmoid(
            DEFAULT_WEIGHTS.get("bias", 0.0)
            + sum(DEFAULT_WEIGHTS.get(n, 0.0) * r.features.get(n, 0.0) for n in names)
        )
        for r in rows
    ]
    return FitReport(
        weights=fitted,
        rows=len(rows),
        positives=positives,
        log_loss=_log_loss(scores, ys, sample_weights),
        auc=auc(scores, ys),
        baseline_auc=auc(baseline, ys),
        feature_names=names,
    )


def auc(scores: Iterable[float], labels: Iterable[float]) -> float:
    """Probability a random positive outranks a random negative. Ties count half."""
    pairs = list(zip(scores, labels))
    pos = [s for s, y in pairs if y]
    neg = [s for s, y in pairs if not y]
    if not pos or not neg:
        return 0.5
    wins = sum(
        1.0 if p > n else 0.5 if p == n else 0.0
        for p in pos
        for n in neg
    )
    return wins / (len(pos) * len(neg))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def _dot(weights: Sequence[float], x: Sequence[float]) -> float:
    return sum(w * xi for w, xi in zip(weights, x))


def _log_loss(scores: Sequence[float], ys: Sequence[float], sw: Sequence[float]) -> float:
    eps = 1e-12
    total = sum(
        -w * (y * math.log(max(s, eps)) + (1 - y) * math.log(max(1 - s, eps)))
        for s, y, w in zip(scores, ys, sw)
    )
    return total / sum(sw)

"""The risk-weight fitter, and above all its refusals."""

import json
import random

import pytest

from triage.fit import MIN_PER_CLASS, MIN_ROWS, FitRefused, Row, auc, fit, load_datasets


def synthetic(n: int, seed: int = 0) -> list[Row]:
    """Rows where risk really is driven by uncovered lines and live mutants."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        uncovered, survival, size = rng.random(), rng.random(), rng.random()
        z = -2.0 + 3.0 * uncovered + 2.0 * survival
        probability = 1.0 / (1.0 + pow(2.718281828, -z))
        rows.append(Row(
            {"uncovered_frac": uncovered, "mutant_survival": survival, "size": size},
            int(rng.random() < probability),
        ))
    return rows


def test_refuses_a_sample_too_small_to_mean_anything():
    with pytest.raises(FitRefused, match=f"at least {MIN_ROWS}"):
        fit(synthetic(MIN_ROWS - 1))


def test_refuses_a_one_sided_sample():
    rows = [Row({"uncovered_frac": 1.0}, 1) for _ in range(MIN_ROWS + 10)]
    rows += [Row({"uncovered_frac": 0.0}, 0) for _ in range(MIN_PER_CLASS - 1)]
    with pytest.raises(FitRefused, match="negative examples"):
        fit(rows)


def test_recovers_the_direction_of_the_real_signal():
    report = fit(synthetic(300))
    assert report.weights["uncovered_frac"] > report.weights["size"]
    assert report.weights["mutant_survival"] > report.weights["size"]
    assert report.auc > 0.65


def test_unseen_features_keep_their_prior_names_at_zero():
    report = fit(synthetic(120))
    # Features present in the prior but absent from the data must still appear,
    # so a fitted file is a drop-in replacement rather than a partial one.
    assert "sensitive" in report.weights
    assert report.weights["sensitive"] == pytest.approx(0.0, abs=1e-6)


def test_auc_is_a_ranking_measure():
    assert auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0
    assert auc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0
    assert auc([0.5, 0.5], [1, 0]) == 0.5


def test_datasets_must_declare_the_schema(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"rows": []}))
    with pytest.raises(FitRefused, match="not a triage-risk-dataset"):
        load_datasets([path])


def test_round_trips_a_written_dataset(tmp_path):
    path = tmp_path / "ds.json"
    path.write_text(json.dumps({
        "schema": "triage-risk-dataset/1",
        "rows": [{"hunk": "a1", "path": "m.py", "features": {"size": 0.5}, "label": 1}],
    }))
    (row,) = load_datasets([path])
    assert row.label == 1 and row.features == {"size": 0.5}

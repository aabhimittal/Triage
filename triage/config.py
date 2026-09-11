"""Configuration, loaded from ``.triage.toml`` at the repo root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # --- test execution -------------------------------------------------
    test_command: list[str] = field(default_factory=lambda: ["pytest", "-q", "-p", "no:randomly"])
    source_roots: list[str] = field(default_factory=lambda: ["."])
    test_paths: list[str] = field(default_factory=lambda: ["tests"])

    # --- thresholds -----------------------------------------------------
    # A hunk must have *every* executable added line covered. Anything less is
    # residual: partial coverage is exactly where bugs hide.
    coverage_threshold: float = 1.0
    # Mutation score on the diff. 0.8 means: of the mutants we could plant in
    # these lines, at least 80% were killed by the existing tests.
    mutation_threshold: float = 0.8
    # Fewer than this many viable mutants on a hunk => the score is noise, so
    # we abstain rather than pass on a 1/1 sample.
    min_mutants: int = 2

    # --- budgets (the systems half of the problem) ----------------------
    mutation_budget_seconds: float = 300.0
    max_mutants_per_hunk: int = 12
    mutant_timeout_seconds: float = 30.0
    equivalence_cases: int = 64
    equivalence_timeout_seconds: float = 5.0

    # --- scope ----------------------------------------------------------
    exclude_globs: list[str] = field(
        default_factory=lambda: [
            "**/migrations/**", "**/node_modules/**", "**/.venv/**",
            "**/build/**", "**/dist/**", "**/__pycache__/**",
        ]
    )
    risk_weights_path: str | None = None

    @classmethod
    def load(cls, repo: Path) -> "Config":
        path = Path(repo) / ".triage.toml"
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        section = data.get("triage", data)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in section.items() if k in known})

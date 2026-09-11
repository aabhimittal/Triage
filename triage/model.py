"""Core data types shared by every stage of the pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    """Outcome of a single check on a single hunk.

    ``ABSTAIN`` is deliberately distinct from ``FAIL``. A check that could not
    run (no annotations, impure function, budget exhausted) tells us nothing,
    and "nothing" must never be laundered into "safe". Both FAIL and ABSTAIN
    push a hunk into the residual set; only the *explanation* differs.
    """

    PASS = "pass"
    FAIL = "fail"
    ABSTAIN = "abstain"
    SKIP = "skip"  # check does not apply to this hunk at all

    @property
    def is_evidence_of_safety(self) -> bool:
        return self is Status.PASS


class Verdict(str, Enum):
    VERIFIED = "verified"    # machine-proved to the depth the checks allow
    RESIDUAL = "residual"    # needs a human
    EXEMPT = "exempt"        # no executable content (docs, comments, blanks)


class Label(str, Enum):
    BEHAVIORAL = "behavioral"
    REFACTOR = "refactor"
    TEST = "test"
    DOCS = "docs"
    CONFIG = "config"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class Hunk:
    """One contiguous changed region of one file, in *new file* coordinates."""

    path: str
    header: str
    new_start: int
    new_end: int
    added_lines: dict[int, str]       # new-file lineno -> text
    removed_lines: dict[int, str]     # old-file lineno -> text
    old_start: int = 0
    old_end: int = 0
    label: Label = Label.BEHAVIORAL
    is_new_file: bool = False
    is_deleted_file: bool = False

    @property
    def id(self) -> str:
        raw = f"{self.path}:{self.new_start}-{self.new_end}:{sorted(self.added_lines)}"
        return hashlib.sha1(raw.encode()).hexdigest()[:8]

    @property
    def added_linenos(self) -> list[int]:
        return sorted(self.added_lines)

    @property
    def size(self) -> int:
        return len(self.added_lines) + len(self.removed_lines)

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.path}:{self.new_start}-{self.new_end} ({self.label.value})"


@dataclass
class HunkVerdict:
    hunk: Hunk
    verdict: Verdict
    checks: list[CheckResult] = field(default_factory=list)
    risk: float = 0.0
    risk_features: dict[str, float] = field(default_factory=dict)

    @property
    def reasons(self) -> list[str]:
        """Why a human is being asked to look. Empty when VERIFIED."""
        return [
            f"{c.name}: {c.detail}"
            for c in self.checks
            if c.status in (Status.FAIL, Status.ABSTAIN)
        ]

    def check(self, name: str) -> CheckResult | None:
        for c in self.checks:
            if c.name == name:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.hunk.id,
            "path": self.hunk.path,
            "lines": [self.hunk.new_start, self.hunk.new_end],
            "label": self.hunk.label.value,
            "verdict": self.verdict.value,
            "risk": round(self.risk, 4),
            "risk_features": {k: round(v, 4) for k, v in self.risk_features.items()},
            "checks": [c.to_dict() for c in self.checks],
        }

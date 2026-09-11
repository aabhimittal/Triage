"""A wall-clock budget shared across every expensive check in a run.

Mutation testing is the only part of TRIAGE whose cost is unbounded in
principle, so it gets an explicit allowance. Running out is not a failure of
the code under review; it is a failure of *our* evidence, so it produces an
ABSTAIN and the hunk goes to a human.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Budget:
    total_seconds: float
    spent: float = 0.0
    _started: float = field(default_factory=time.monotonic)

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0

    def charge(self, seconds: float) -> None:
        self.spent += seconds

    def slice_for(self, cap: float) -> float:
        return max(0.0, min(cap, self.remaining))

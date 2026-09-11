"""TRIAGE: shrink what humans must review.

TRIAGE treats reviewer attention as a scarce resource. It proves whatever it
can about a pull request automatically and forwards only the *unproven
remainder* to a human.

The load-bearing invariant of the whole system:

    A hunk is VERIFIED only if every applicable check actively passed.
    An inconclusive check is never evidence of safety.

See docs/DESIGN.md for the rationale and the known failure modes.
"""

__version__ = "0.1.0"

from triage.model import CheckResult, Hunk, HunkVerdict, Status, Verdict

__all__ = ["CheckResult", "Hunk", "HunkVerdict", "Status", "Verdict", "__version__"]

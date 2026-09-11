"""Measuring the thing that actually matters: review saved vs bugs escaped.

The experiment, in one paragraph. Take a real PR. Plant a bug in it. If the
test suite catches the bug, CI already had it covered and it was never going to
reach a reviewer -- discard it. Keep only the bugs that *survive the suite*,
because those are precisely the bugs whose last line of defence is a human
reading the diff. Now ask TRIAGE's question of each one: did the hunk
containing it end up in the residual set (routed to a human, good) or in the
verified set (stamped safe, an escape)?

That yields two numbers per run:

    review burden = residual lines / (verified + residual) lines
    escape rate   = escaped bugs / surviving bugs

and sweeping the risk threshold traces the curve between them. The product
claim -- "review 19% of the lines, miss almost nothing" -- is one point on that
curve, and this module is what lets anyone check whether the point is real.

The honest caveat, stated here rather than buried: mutants are a *proxy* for
real defects. They over-represent local, mechanical errors (off-by-one, flipped
comparison) and entirely miss the errors that make real code reviews worth
having -- a wrong requirement, a missing case nobody thought of, a race. A good
escape rate here is necessary for the claim and nowhere near sufficient.
"""

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass, field
from pathlib import Path

from triage.budget import Budget
from triage.config import Config
from triage.model import Label, Verdict
from triage.mutate import generate_mutants
from triage.pipeline import TriageReport, run
from triage.runner import collect_coverage, run_tests
from triage.workspace import patched_file, scratch_copy


@dataclass
class InjectedBug:
    path: str
    line: int
    mutation: str
    hunk_id: str
    verdict: str
    risk: float
    in_test_code: bool = False

    @property
    def escaped(self) -> bool:
        """The bug reached no one: not caught by CI, not routed to a human."""
        return self.verdict == Verdict.VERIFIED.value


@dataclass
class CurvePoint:
    threshold: float
    review_lines: int
    total_lines: int
    escapes: int
    bugs: int

    @property
    def review_burden(self) -> float:
        return self.review_lines / self.total_lines if self.total_lines else 0.0

    @property
    def escape_rate(self) -> float:
        return self.escapes / self.bugs if self.bugs else 0.0


@dataclass
class EvalResult:
    report: TriageReport
    bugs: list[InjectedBug] = field(default_factory=list)
    caught_by_ci: int = 0
    curve: list[CurvePoint] = field(default_factory=list)
    attempted: set[str] = field(default_factory=set)

    @property
    def product_bugs(self) -> list[InjectedBug]:
        return [b for b in self.bugs if not b.in_test_code]

    @property
    def test_bugs(self) -> list[InjectedBug]:
        """Mutants planted in test code.

        Kept apart from the headline, and never dropped. A mutant that deletes
        an assertion survives the suite *by construction* -- no test suite can
        notice its own degradation -- so pooling these with product-code bugs
        produces an escape rate that measures an impossibility rather than a
        miss. Reported on their own line because the underlying risk is real:
        TRIAGE certifies that a new test detects this change, not that nobody
        will weaken it next week.
        """
        return [b for b in self.bugs if b.in_test_code]

    @property
    def escapes(self) -> int:
        return sum(1 for b in self.product_bugs if b.escaped)

    @property
    def escape_rate(self) -> float:
        product = self.product_bugs
        return self.escapes / len(product) if product else 0.0

    @property
    def test_escapes(self) -> int:
        return sum(1 for b in self.test_bugs if b.escaped)

    def to_dataset(self) -> dict:
        """Labelled rows for `triage fit`.

        Only hunks we actually planted a bug in appear. A hunk we never probed
        is not a negative example -- it is no example at all, and silently
        treating it as "no bug here" would train the model to trust exactly the
        code we failed to examine.
        """
        harbouring = {b.hunk_id for b in self.bugs}
        rows = []
        for verdict in self.report.verdicts:
            if verdict.hunk.id not in self.attempted:
                continue
            rows.append({
                "hunk": verdict.hunk.id,
                "path": verdict.hunk.path,
                "features": verdict.risk_features,
                "label": int(verdict.hunk.id in harbouring),
                "verdict": verdict.verdict.value,
            })
        return {
            "schema": "triage-risk-dataset/1",
            "repo": self.report.repo,
            "base": self.report.base,
            "head": self.report.head,
            "rows": rows,
        }

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["threshold", "review_lines", "total_lines", "review_burden",
                         "escapes", "bugs", "escape_rate"])
        for p in self.curve:
            writer.writerow([f"{p.threshold:.3f}", p.review_lines, p.total_lines,
                             f"{p.review_burden:.4f}", p.escapes, p.bugs,
                             f"{p.escape_rate:.4f}"])
        writer.writerow([])
        writer.writerow(["path", "line", "mutation", "hunk", "verdict", "risk", "escaped"])
        for b in self.bugs:
            writer.writerow([b.path, b.line, b.mutation, b.hunk_id, b.verdict,
                             f"{b.risk:.3f}", int(b.escaped)])
        return buf.getvalue()


def evaluate(
    repo: Path,
    base: str,
    head: str = "HEAD",
    cfg: Config | None = None,
    n_bugs: int = 25,
    seed: int = 0,
) -> EvalResult:
    repo = Path(repo).resolve()
    cfg = cfg or Config.load(repo)
    report = run(repo, base, head, cfg)
    result = EvalResult(report=report)
    if not report.suite_green:
        return result

    rng = random.Random(seed)
    by_hunk = {v.hunk.id: v for v in report.verdicts}

    with scratch_copy(repo) as workdir:
        index = collect_coverage(workdir, cfg)
        candidates = []
        for hv in report.verdicts:
            if hv.verdict is Verdict.EXEMPT or not hv.hunk.path.endswith(".py"):
                continue
            file_path = workdir / hv.hunk.path
            if not file_path.exists():
                continue
            executable = index.executable(hv.hunk.path, hv.hunk.added_linenos)
            if not executable:
                continue
            for mutant in generate_mutants(
                hv.hunk.path, file_path.read_text(), set(executable), limit=8
            ):
                candidates.append((hv.hunk.id, mutant))
        rng.shuffle(candidates)

        budget = Budget(cfg.mutation_budget_seconds * 4)
        for hunk_id, mutant in candidates:
            if len(result.bugs) >= n_bugs or budget.exhausted:
                break
            file_path = workdir / mutant.path
            with patched_file(file_path, mutant.source):
                run_result = run_tests(workdir, cfg, timeout=cfg.mutant_timeout_seconds * 4)
            budget.charge(run_result.duration)
            result.attempted.add(hunk_id)
            if run_result.ok:
                hv = by_hunk[hunk_id]
                result.bugs.append(InjectedBug(
                    path=mutant.path, line=mutant.lineno, mutation=mutant.label,
                    hunk_id=hunk_id, verdict=hv.verdict.value, risk=hv.risk,
                    in_test_code=hv.hunk.label is Label.TEST,
                ))
            else:
                result.caught_by_ci += 1

    result.curve = _curve(report, result.product_bugs)
    return result


def _curve(report: TriageReport, bugs: list[InjectedBug]) -> list[CurvePoint]:
    """Sweep the risk threshold: below tau, even verified hunks go to a human.

    At tau = 1.0 the reviewer sees only the residual set (TRIAGE's default). As
    tau falls, verified hunks are added back in risk order, buying down the
    escape rate with reviewer attention. The shape of that trade is the product.
    """
    judged = [v for v in report.verdicts if v.verdict is not Verdict.EXEMPT]
    total_lines = sum(len(v.hunk.added_lines) for v in judged) or 1
    thresholds = sorted({round(v.risk, 3) for v in judged} | {0.0, 1.0}, reverse=True)

    points: list[CurvePoint] = []
    for tau in thresholds:
        reviewed_ids = {
            v.hunk.id for v in judged
            if v.verdict is Verdict.RESIDUAL or v.risk >= tau
        }
        review_lines = sum(
            len(v.hunk.added_lines) for v in judged if v.hunk.id in reviewed_ids
        )
        escapes = sum(1 for b in bugs if b.hunk_id not in reviewed_ids)
        points.append(CurvePoint(tau, review_lines, total_lines, escapes, len(bugs)))
    return points


def format_curve(result: EvalResult) -> str:
    s = result.report.stats
    lines = [
        "TRIAGE evaluation",
        "=================",
        f"diff                 {s.changed_lines} added lines across {s.hunks} hunks",
        f"verified / residual  {s.verified_lines} / {s.residual_lines} lines "
        f"({100 * s.verified_fraction:.0f}% machine-verified)",
        f"bugs injected        {result.caught_by_ci + len(result.bugs)}",
        f"  caught by CI       {result.caught_by_ci} (the suite failed; never a review problem)",
        f"  survived the suite {len(result.bugs)} (a human was the only defence left)",
        f"  hunks probed       {len(result.attempted)} (only these can be labelled for fitting)",
        "",
        f"in product code      {len(result.product_bugs)} surviving bugs",
        f"  ESCAPED TRIAGE     {result.escapes} "
        f"({100 * result.escape_rate:.1f}% landed in a verified hunk)",
        f"in test code         {len(result.test_bugs)} surviving mutants, "
        f"{result.test_escapes} in verified hunks",
        "                     (a deleted assertion cannot be noticed by the suite",
        "                      that contains it, so these survive by construction;",
        "                      counted separately, not discounted)",
        "",
        "risk threshold sweep (tau=1.00 is the default: review the residual set only)",
        f"{'tau':>6}  {'review burden':>14}  {'escape rate':>12}",
    ]
    for p in result.curve:
        bar = "#" * int(round(20 * p.review_burden))
        lines.append(
            f"{p.threshold:>6.2f}  {100 * p.review_burden:>12.0f}%  "
            f"{100 * p.escape_rate:>11.0f}%  {bar}"
        )
    if result.escapes:
        lines += ["", "escaped bugs (these are the failures that matter):"]
        for b in result.bugs:
            if b.escaped:
                lines.append(f"  {b.path}:{b.line}  {b.mutation}  (hunk {b.hunk_id})")
    return "\n".join(lines)

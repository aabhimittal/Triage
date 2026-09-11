"""Rendering a run as a PR annotation, a terminal summary, or JSON.

The headline number is deliberately conservative: the denominator is
verified + residual lines, and lines we merely declined to judge never inflate
it. A tool that shrinks review queues is only useful if its claims are boring
and true.
"""

from __future__ import annotations

import json

from triage.model import HunkVerdict, Status, Verdict
from triage.pipeline import TriageReport

_LIMITS = """\
<details><summary>What "machine-verified" does and does not mean</summary>

**Verified** here means: every line in the hunk is executed by the test suite,
and mutating those lines makes the suite fail. For hunks labelled *refactor* it
can also mean: the old and new implementations returned identical results on
generated inputs. For test code it means: the test fails when the definitions
it references are reverted, so it constrains something this change did.

It does **not** mean the code is correct. A hunk can be verified and still be
the wrong feature, a bad interface, or a performance cliff. TRIAGE narrows the
queue; it does not raise the ceiling.

Specifically out of reach: anything whose effects are not observable through
return values and test outcomes (I/O, database writes, logging), concurrency
bugs that need a schedule to surface, non-Python files (there is no analyser
for them here, and they are listed as residual saying exactly that), and
anything the test suite itself gets wrong. An edited -- as opposed to newly
added -- test is always residual, because failing against the old code does not
rule out an assertion having been weakened. Hunks we could not judge are listed
as residual with the reason, never silently counted as safe.
</details>"""


def to_json(report: TriageReport, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), indent=indent)


def to_markdown(report: TriageReport, max_residual: int = 25) -> str:
    s = report.stats
    if not report.suite_green:
        return (
            "## TRIAGE: cannot verify anything\n\n"
            "The test suite does not pass at this commit, so every test-based "
            "claim would be meaningless. All "
            f"{s.changed_lines} changed lines are residual until CI is green.\n\n"
            f"```\n{report.suite_output[-1500:]}\n```\n"
        )

    residual = report.residual[:max_residual]
    pct = round(100 * s.verified_fraction)
    judged = s.verified_lines + s.residual_lines
    lines = [
        f"## TRIAGE: review {s.residual_lines} of {judged} changed line(s)",
        "",
        f"{pct}% of the changed executable lines are constrained by the test "
        "suite and need no human on the correctness axis. That percentage is a "
        "property of **this repository's tests**, not of the change and not of "
        "TRIAGE: the same tool reports 97% on a disciplined diff and 3% on a "
        "greenfield one. Read the residual list, not the number.",
        "",
        f"| verified | residual | exempt | hunks | mutants | wall |",
        f"|---:|---:|---:|---:|---:|---:|",
        f"| {s.verified_lines} | {s.residual_lines} | {s.exempt_lines} | "
        f"{s.verified_hunks}/{s.hunks} | {s.mutants_evaluated} | {s.wall_seconds:.0f}s |",
        "",
    ]
    if s.impact_selection_factor > 1:
        lines += [
            f"_Test-impact selection ran {s.mean_tests_per_mutant:.1f} of "
            f"{s.tests_in_suite} tests per mutant "
            f"({s.impact_selection_factor:.0f}x less work than a full-suite run)._",
            "",
        ]

    if not residual:
        lines += ["Nothing left for a human on the correctness axis.", ""]
    else:
        lines += [f"### Review these, highest risk first", ""]
        for i, hv in enumerate(residual, 1):
            lines += _residual_block(i, hv)
        hidden = len(report.residual) - len(residual)
        if hidden > 0:
            lines.append(f"_...and {hidden} more residual hunk(s); see the JSON report._\n")

    verified = report.verified
    if verified:
        lines += [
            "<details><summary>"
            f"{len(verified)} hunk(s) cleared automatically</summary>", "",
        ]
        for hv in verified:
            proof = "; ".join(
                c.detail for c in hv.checks if c.status is Status.PASS
            )
            lines.append(f"- `{hv.hunk.path}:{hv.hunk.new_start}-{hv.hunk.new_end}` — {proof}")
        lines += ["", "</details>", ""]

    lines.append(_LIMITS)
    return "\n".join(lines)


def _residual_block(i: int, hv: HunkVerdict) -> list[str]:
    h = hv.hunk
    out = [
        f"**{i}. `{h.path}:{h.new_start}-{h.new_end}`** · risk {hv.risk:.2f} · "
        f"{h.label.value} · {len(h.added_lines)} line(s)",
    ]
    for reason in hv.reasons:
        out.append(f"   - {reason}")
    mut = hv.check("constrained")
    if mut and mut.evidence.get("survived"):
        out.append("   - surviving mutations (nothing in the suite objects):")
        for s in mut.evidence["survived"][:4]:
            out.append(f"     - line {s['line']}: {s['mutation']}")
    top = sorted(hv.risk_features.items(), key=lambda kv: -kv[1])[:3]
    drivers = ", ".join(f"{k}={v:.2f}" for k, v in top if v > 0)
    if drivers:
        out.append(f"   - risk drivers: {drivers}")
    out.append("")
    return out


def to_text(report: TriageReport) -> str:
    s = report.stats
    if not report.suite_green:
        return "SUITE RED — no verification possible. Fix CI first."
    head = (
        f"TRIAGE review {s.residual_lines} of "
        f"{s.verified_lines + s.residual_lines} changed lines  |  "
        f"{round(100 * s.verified_fraction)}% test-constrained "
        f"(a fact about this suite, not about the change)  |  "
        f"{s.exempt_lines} exempt  |  {s.wall_seconds:.1f}s"
    )
    rows = [head, "-" * len(head)]
    for hv in report.residual:
        rows.append(
            f"  [{hv.risk:.2f}] {hv.hunk.path}:{hv.hunk.new_start}-{hv.hunk.new_end} "
            f"({hv.hunk.label.value})"
        )
        for reason in hv.reasons:
            rows.append(f"        - {reason}")
    if not report.residual:
        rows.append("  (residual set is empty)")
    return "\n".join(rows)

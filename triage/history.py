"""Did TRIAGE catch the bugs that actually happened?

Every other measurement in this project uses mutants as a stand-in for defects.
Mutants are convenient and they are *not* bugs: they over-represent local
mechanical slips and completely miss wrong requirements, missing cases and
races. A 0% escape rate on mutants is evidence the machinery works, not
evidence about real defects.

This module replaces the proxy with the real thing, using the repository's own
history as the corpus. The procedure is the SZZ algorithm (Sliwerski,
Zimmermann and Zeller, 2005):

    1. find commits whose message says they fix a bug;
    2. for each, take the lines that fix *changed or deleted* -- those lines
       were the bug;
    3. `git blame` them at the fix's parent to find the commit that introduced
       them (the bug-inducing commit);
    4. replay that commit as if it were a pull request, and ask whether TRIAGE
       put the offending lines in front of a human.

A caught bug is one where the buggy lines landed in the residual set. A miss is
one where TRIAGE said "verified" about a hunk that we now know, from the
project's own history, contained a defect. Misses are the number that matters,
and they are listed individually rather than summarised.

What this measurement cannot do, stated plainly because the result is worthless
without it:

  * **SZZ is noisy.** Blame attributes a line to whoever last touched it, so a
    reformat or a move can be fingered instead of the author of the logic. The
    literature puts SZZ precision somewhere around 50-80% depending on the
    refinement used; this is the plain version with only the obvious filters.
  * **Commit messages are a weak oracle** for what was a bug fix.
  * **The suite is the one at that commit**, which may not run in today's
    environment; those commits are skipped and counted, never silently dropped.
  * **Survivorship.** Bugs that were found and fixed are the ones that got
    caught *somehow*. Bugs still sitting in the code are invisible here.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from triage import gitutil
from triage.budget import Budget
from triage.config import Config
from triage.model import Verdict
from triage.pipeline import TriageReport, run

FIX_PATTERN = re.compile(
    r"\b(fix(e[sd])?|bug|regression|hotfix|broken|crash|incorrect|wrong)\b", re.I
)
_REVERT = re.compile(r"^revert\b", re.I)
_BLAME_HEADER = re.compile(r"^([0-9a-f]{40})\s+(\d+)\s+(\d+)")
_NULL_SHA = "0" * 40


@dataclass
class InducingCommit:
    sha: str
    subject: str
    fix_sha: str
    fix_subject: str
    lines: dict[str, set[int]] = field(default_factory=dict)

    @property
    def line_count(self) -> int:
        return sum(len(v) for v in self.lines.values())


@dataclass
class BugOutcome:
    inducing: InducingCommit
    caught: bool | None          # None means we could not evaluate it
    reason: str = ""
    residual_lines: int = 0
    judged_lines: int = 0
    hunks: list[str] = field(default_factory=list)

    @property
    def review_burden(self) -> float:
        return self.residual_lines / self.judged_lines if self.judged_lines else 0.0


@dataclass
class HistoryReport:
    repo: str
    commits_scanned: int = 0
    fix_commits: int = 0
    candidates: int = 0
    outcomes: list[BugOutcome] = field(default_factory=list)

    @property
    def evaluated(self) -> list[BugOutcome]:
        return [o for o in self.outcomes if o.caught is not None]

    @property
    def caught(self) -> list[BugOutcome]:
        return [o for o in self.outcomes if o.caught is True]

    @property
    def missed(self) -> list[BugOutcome]:
        return [o for o in self.outcomes if o.caught is False]

    @property
    def skipped(self) -> list[BugOutcome]:
        return [o for o in self.outcomes if o.caught is None]

    @property
    def catch_rate(self) -> float:
        judged = self.evaluated
        return len(self.caught) / len(judged) if judged else 0.0

    @property
    def mean_burden(self) -> float:
        judged = self.evaluated
        return sum(o.review_burden for o in judged) / len(judged) if judged else 0.0

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "commits_scanned": self.commits_scanned,
            "fix_commits": self.fix_commits,
            "candidates": self.candidates,
            "evaluated": len(self.evaluated),
            "caught": len(self.caught),
            "missed": len(self.missed),
            "catch_rate": round(self.catch_rate, 4),
            "mean_review_burden": round(self.mean_burden, 4),
            "bugs": [
                {
                    "inducing": o.inducing.sha[:12],
                    "inducing_subject": o.inducing.subject,
                    "fix": o.inducing.fix_sha[:12],
                    "fix_subject": o.inducing.fix_subject,
                    "files": {k: sorted(v) for k, v in o.inducing.lines.items()},
                    "caught": o.caught,
                    "reason": o.reason,
                    "residual_lines": o.residual_lines,
                    "judged_lines": o.judged_lines,
                }
                for o in self.outcomes
            ],
        }


def find_fix_commits(
    repo: Path, limit: int = 300, pattern: re.Pattern[str] = FIX_PATTERN
) -> list[tuple[str, str]]:
    """Commits whose subject claims to fix something. Merges excluded."""
    out = gitutil.git(
        repo, "log", f"-{limit}", "--no-merges", "--format=%H%x00%s", check=False
    )
    found: list[tuple[str, str]] = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\x00")
        if not sha:
            continue
        if pattern.search(subject) or _REVERT.match(subject.strip()):
            found.append((sha, subject))
    return found


def fixed_lines(repo: Path, fix_sha: str) -> dict[str, set[int]]:
    """Lines the fix removed or replaced, in the coordinates of its parent.

    Added lines are ignored on purpose: a line the fix *added* did not exist in
    the buggy version, so blame has nothing to attribute it to. The lines a fix
    deletes or rewrites are the ones that were wrong.
    """
    patch = gitutil.git(
        repo, "show", "--unified=0", "--format=", "--no-color", fix_sha, check=False
    )
    out: dict[str, set[int]] = {}
    path = ""
    old_lineno = 0
    for line in patch.splitlines():
        if line.startswith("--- "):
            raw = line[4:].strip()
            path = "" if raw == "/dev/null" else raw[2:] if raw.startswith("a/") else raw
        elif line.startswith("@@"):
            m = re.match(r"^@@ -(\d+)", line)
            old_lineno = int(m.group(1)) if m else 0
        elif line.startswith("-") and not line.startswith("---"):
            if path and line[1:].strip():  # ignore blank-line churn
                out.setdefault(path, set()).add(old_lineno)
            old_lineno += 1
        elif not line.startswith("+"):
            old_lineno += 1
    return out


def blame_origin(repo: Path, rev: str, path: str, lineno: int) -> tuple[str, int] | None:
    """(commit, line number in that commit) for one line, or None."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "blame", "--porcelain", "-L", f"{lineno},{lineno}",
         rev, "--", path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    m = _BLAME_HEADER.match(proc.stdout.splitlines()[0])
    if not m or m.group(1) == _NULL_SHA:
        return None
    return m.group(1), int(m.group(2))


def inducing_commits(repo: Path, fix_sha: str, fix_subject: str) -> list[InducingCommit]:
    """Blame every line a fix touched back to the commit that wrote it."""
    by_sha: dict[str, InducingCommit] = {}
    for path, linenos in fixed_lines(repo, fix_sha).items():
        for lineno in sorted(linenos):
            origin = blame_origin(repo, f"{fix_sha}^", path, lineno)
            if origin is None:
                continue
            sha, original_line = origin
            if sha == fix_sha:
                continue
            entry = by_sha.get(sha)
            if entry is None:
                subject = gitutil.git(
                    repo, "log", "-1", "--format=%s", sha, check=False
                ).strip()
                entry = InducingCommit(sha, subject, fix_sha, fix_subject)
                by_sha[sha] = entry
            entry.lines.setdefault(path, set()).add(original_line)
    return list(by_sha.values())


def replay(repo: Path, inducing: InducingCommit, cfg: Config) -> BugOutcome:
    """Run TRIAGE on the bug-inducing commit as if it were a pull request."""
    if not _has_parent(repo, inducing.sha):
        return BugOutcome(inducing, None, "root commit has no parent to diff against")

    with _worktree(repo, inducing.sha) as tree:
        if tree is None:
            return BugOutcome(inducing, None, "could not create a worktree at that commit")
        try:
            report = run(tree, f"{inducing.sha}^", inducing.sha, cfg)
        except Exception as exc:  # noqa: BLE001 - an old commit can fail in many ways
            return BugOutcome(inducing, None, f"replay failed: {exc!r}")

    if not report.suite_green:
        return BugOutcome(
            inducing, None,
            "the suite at that commit does not pass in today's environment, so "
            "no verdict there would mean anything",
        )
    return _judge(inducing, report)


def _judge(inducing: InducingCommit, report: TriageReport) -> BugOutcome:
    judged = [v for v in report.verdicts if v.verdict is not Verdict.EXEMPT]
    residual_lines = sum(
        len(v.hunk.added_lines) for v in judged if v.verdict is Verdict.RESIDUAL
    )
    judged_lines = sum(len(v.hunk.added_lines) for v in judged)

    covering = [
        v for v in report.verdicts
        if any(
            lineno in v.hunk.added_lines
            for lineno in inducing.lines.get(v.hunk.path, ())
        )
    ]
    if not covering:
        return BugOutcome(
            inducing, None,
            "the blamed lines are not in this commit's diff (blame followed a "
            "move or a reformat), so there is nothing to have judged",
            residual_lines, judged_lines,
        )

    residual = [v for v in covering if v.verdict is Verdict.RESIDUAL]
    hunks = [f"{v.hunk.path}:{v.hunk.new_start}-{v.hunk.new_end}" for v in covering]
    if residual:
        reasons = "; ".join(residual[0].reasons[:2]) or "routed for review"
        return BugOutcome(inducing, True, reasons, residual_lines, judged_lines, hunks)
    verdicts = ", ".join(sorted({v.verdict.value for v in covering}))
    return BugOutcome(
        inducing, False, f"the hunk holding the bug was marked {verdicts}",
        residual_lines, judged_lines, hunks,
    )


def study(
    repo: Path,
    cfg: Config,
    scan: int = 300,
    max_bugs: int = 5,
    budget_seconds: float = 1800.0,
) -> HistoryReport:
    repo = Path(repo).resolve()
    report = HistoryReport(repo=str(repo))
    pattern = re.compile(cfg.fix_pattern, re.I) if cfg.fix_pattern else FIX_PATTERN
    fixes = find_fix_commits(repo, scan, pattern)
    report.commits_scanned = scan
    report.fix_commits = len(fixes)

    candidates: list[InducingCommit] = []
    seen: set[str] = set()
    for fix_sha, subject in fixes:
        for inducing in inducing_commits(repo, fix_sha, subject):
            if inducing.sha in seen:
                continue
            seen.add(inducing.sha)
            candidates.append(inducing)
    report.candidates = len(candidates)

    # Most-implicated commits first: a commit whose lines several fixes had to
    # touch is the most likely to be a genuine defect rather than blame noise.
    candidates.sort(key=lambda c: -c.line_count)

    budget = Budget(budget_seconds)
    for inducing in candidates[:max_bugs]:
        if budget.exhausted:
            report.outcomes.append(
                BugOutcome(inducing, None, "time budget for the study was spent")
            )
            continue
        started = time.monotonic()
        report.outcomes.append(replay(repo, inducing, cfg))
        budget.charge(time.monotonic() - started)
    return report


def _has_parent(repo: Path, sha: str) -> bool:
    out = gitutil.git(repo, "rev-list", "--parents", "-n", "1", sha, check=False)
    return len(out.split()) > 1


class _worktree:
    """A detached worktree at a revision, removed on exit."""

    def __init__(self, repo: Path, sha: str):
        self.repo = repo
        self.sha = sha
        self.path: Path | None = None

    def __enter__(self) -> Path | None:
        import tempfile
        base = Path(tempfile.mkdtemp(prefix="triage-history-"))
        target = base / "tree"
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "--detach",
             str(target), self.sha],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return None
        self.path = target
        return target

    def __exit__(self, *exc) -> bool:
        if self.path is not None:
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force",
                 str(self.path)],
                capture_output=True, text=True,
            )
        return False


def format_report(report: HistoryReport) -> str:
    lines = [
        "TRIAGE historical bug study",
        "===========================",
        f"commits scanned      {report.commits_scanned}",
        f"bug-fix commits      {report.fix_commits}",
        f"bug-inducing commits {report.candidates} (blamed from the lines those fixes changed)",
        f"replayed             {len(report.evaluated)}",
        "",
        f"  CAUGHT             {len(report.caught)}  the buggy lines were routed to a human",
        f"  MISSED             {len(report.missed)}  the buggy lines sat in a verified hunk",
    ]
    if report.evaluated:
        lines += [
            f"  catch rate         {100 * report.catch_rate:.0f}%",
            f"  review burden      {100 * report.mean_burden:.0f}% of changed lines on those commits",
        ]
    if report.missed:
        lines += ["", "MISSES (the only number that matters):"]
        for outcome in report.missed:
            files = ", ".join(
                f"{p}:{min(v)}" for p, v in sorted(outcome.inducing.lines.items())
            )
            lines += [
                f"  {outcome.inducing.sha[:10]} {outcome.inducing.subject[:60]}",
                f"    bug at {files}; {outcome.reason}",
                f"    later fixed by {outcome.inducing.fix_sha[:10]} "
                f"{outcome.inducing.fix_subject[:60]}",
            ]
    if report.skipped:
        lines += ["", f"not evaluated ({len(report.skipped)}):"]
        counts: dict[str, int] = {}
        for outcome in report.skipped:
            key = outcome.reason.split(",")[0][:70]
            counts[key] = counts.get(key, 0) + 1
        for reason, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:>3}  {reason}")
    lines += [
        "",
        "Caveats: SZZ blame attributes a line to whoever last touched it, so a",
        "reformat can be fingered instead of the author of the logic; commit",
        "messages are a weak oracle for what was a bug fix; and only bugs that",
        "were eventually found and fixed appear here at all.",
    ]
    return "\n".join(lines)

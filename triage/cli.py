"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from triage import report as render
from triage.config import Config
from triage.pipeline import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage",
        description="Prove what can be proved about a PR; send only the rest to a human.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="triage a diff")
    run_cmd.add_argument("--repo", default=".", type=Path)
    run_cmd.add_argument("--base", default="main", help="base ref (merge base is used)")
    run_cmd.add_argument("--head", default="HEAD")
    run_cmd.add_argument("--diff-file", type=Path, help="read a unified diff instead of calling git")
    run_cmd.add_argument("--format", choices=["md", "text", "json"], default="text")
    run_cmd.add_argument("--out", type=Path, help="write the report here instead of stdout")
    run_cmd.add_argument("--budget", type=float, help="mutation budget in seconds")
    run_cmd.add_argument("--max-mutants", type=int, help="cap mutants per hunk")
    run_cmd.add_argument(
        "--fail-on-residual", action="store_true",
        help="exit 1 when anything still needs a human (for CI gating)",
    )

    eval_cmd = sub.add_parser(
        "eval", help="inject surviving bugs into the diff and measure escape rate"
    )
    eval_cmd.add_argument("--repo", default=".", type=Path)
    eval_cmd.add_argument("--base", default="main")
    eval_cmd.add_argument("--head", default="HEAD")
    eval_cmd.add_argument("--bugs", type=int, default=25, help="max bugs to inject")
    eval_cmd.add_argument("--seed", type=int, default=0)
    eval_cmd.add_argument("--out", type=Path, help="write the curve as CSV")
    eval_cmd.add_argument(
        "--dataset", type=Path,
        help="write labelled hunks here, for `triage fit`",
    )

    bugs_cmd = sub.add_parser(
        "bugs",
        help="replay this repo's own historical bugs and measure the catch rate",
    )
    bugs_cmd.add_argument("--repo", default=".", type=Path)
    bugs_cmd.add_argument("--scan", type=int, default=300, help="commits to search for fixes")
    bugs_cmd.add_argument("--max-bugs", type=int, default=5, help="bug-inducing commits to replay")
    bugs_cmd.add_argument("--budget", type=float, default=1800.0, help="study budget, seconds")
    bugs_cmd.add_argument("--format", choices=["text", "json"], default="text")
    bugs_cmd.add_argument("--out", type=Path)

    fit_cmd = sub.add_parser(
        "fit", help="fit risk-ranking weights from one or more eval datasets"
    )
    fit_cmd.add_argument("--dataset", type=Path, nargs="+", required=True)
    fit_cmd.add_argument("--out", type=Path, help="write fitted weights as JSON")
    fit_cmd.add_argument("--l2", type=float, default=1.0, help="ridge penalty")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "fit":  # the only command that works without a repo
        return _fit(args)

    repo = Path(args.repo).resolve()
    cfg = Config.load(repo)

    if args.command == "run":
        if args.budget is not None:
            cfg.mutation_budget_seconds = args.budget
        if args.max_mutants is not None:
            cfg.max_mutants_per_hunk = args.max_mutants
        diff_text = args.diff_file.read_text() if args.diff_file else None
        result = run(repo, args.base, args.head, cfg, diff_text=diff_text)
        text = {
            "md": render.to_markdown,
            "json": render.to_json,
            "text": render.to_text,
        }[args.format](result)
        if args.out:
            args.out.write_text(text)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(text)
        if args.fail_on_residual and result.residual:
            return 1
        return 0 if result.suite_green else 2

    if args.command == "bugs":
        from triage.history import format_report, study

        outcome = study(
            repo, cfg, scan=args.scan, max_bugs=args.max_bugs,
            budget_seconds=args.budget,
        )
        text = (
            json.dumps(outcome.to_dict(), indent=2)
            if args.format == "json" else format_report(outcome)
        )
        if args.out:
            args.out.write_text(text)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(text)
        return 0

    if args.command == "eval":
        from triage.evaluate import evaluate, format_curve

        outcome = evaluate(repo, args.base, args.head, cfg, n_bugs=args.bugs, seed=args.seed)
        print(format_curve(outcome))
        if args.out:
            args.out.write_text(outcome.to_csv())
            print(f"wrote {args.out}", file=sys.stderr)
        if args.dataset:
            args.dataset.write_text(json.dumps(outcome.to_dataset(), indent=2))
            print(f"wrote {args.dataset}", file=sys.stderr)
        return 0

    return 2


def _fit(args) -> int:
    from triage.fit import FitRefused, fit, load_datasets

    try:
        report = fit(load_datasets(args.dataset), l2=args.l2)
    except FitRefused as exc:
        print(f"refusing to fit: {exc}", file=sys.stderr)
        return 3
    print(report.render())
    if args.out:
        args.out.write_text(json.dumps(report.weights, indent=2))
        print(f"wrote {args.out}", file=sys.stderr)
        print(
            f'point .triage.toml at it with risk_weights_path = "{args.out}"',
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

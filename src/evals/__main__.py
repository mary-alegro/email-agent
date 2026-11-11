"""Run the eval harness against the real LLM.

    uv run python -m src.evals                      # all cases, once
    uv run python -m src.evals --repeats 3          # measure consistency
    uv run python -m src.evals --cases fraud        # cases whose id or tags contain "fraud"
    uv run python -m src.evals --provider openai --model <name>

Results go to eval-results/<timestamp>-<model>.{json,md}. The exit code is 1 if the
safety gate fails (any wrong write or false accept), 2 if a run errored, else 0.
Each run makes real LLM calls and costs money.
"""

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the email agent on labeled cases.")
    parser.add_argument("--repeats", type=int, default=1, help="runs per case (default 1)")
    parser.add_argument("--cases", help="only cases whose id or tags contain this text")
    parser.add_argument("--provider", choices=["anthropic", "openai"], help="override LLM_PROVIDER")
    parser.add_argument("--model", help="override LLM_MODEL")
    parser.add_argument("--effort", help="override LLM_EFFORT")
    parser.add_argument("--out", default="eval-results", help="output directory")
    args = parser.parse_args()

    # Configure through the environment *before* importing the app: a throwaway database
    # (never data/app.db), quiet logs, and any model overrides.
    os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='sc-evals-')}/eval.db"
    # Always the in-process ERP, whatever .env says: a remote ERP_MCP_URL would read and
    # write some other database than the throwaway one each run resets and seeds.
    os.environ["ERP_MCP_URL"] = ""
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    for name, value in (
        ("LLM_PROVIDER", args.provider),
        ("LLM_MODEL", args.model),
        ("LLM_EFFORT", args.effort),
    ):
        if value:
            os.environ[name] = value

    from src.config import settings
    from src.evals.dataset import load_cases
    from src.evals.metrics import summarize
    from src.evals.report import format_cost, gate_failures, write_report
    from src.evals.runner import run_cases
    from src.utils.logging import setup_logging

    setup_logging(settings.log_level, settings.log_format)
    cases = load_cases(args.cases)
    if not cases:
        print(f"No cases match {args.cases!r}", file=sys.stderr)
        return 2

    config = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provider": settings.llm_provider,
        "model": settings.llm_model or "default",
        "effort": settings.llm_effort or "default",
        "repeats": args.repeats,
        "cases_filter": args.cases,
    }
    print(
        f"Running {len(cases)} cases x {args.repeats} on {config['provider']} ({config['model']})"
    )
    scores = asyncio.run(run_cases(cases, repeats=args.repeats))
    summary = summarize(scores)
    json_path, md_path = write_report(scores, summary, config, Path(args.out))

    safety, c = summary["safety"], summary["commitments"]
    print(
        f"\nWrong writes: {safety['wrong_writes']} · false accepts: {safety['false_accepts']} · "
        f"missed writes: {safety['missed_writes']}\n"
        f"Commitments P/R/F1: {c['precision']:.0%}/{c['recall']:.0%}/{c['f1']:.0%} · "
        f"runs fully correct: {summary['runs_passed']}/{summary['runs']} · "
        f"cost: {format_cost(summary['cost_usd'])}\n"
        f"Report: {md_path}\nDetails: {json_path}"
    )
    if failures := gate_failures(summary):
        print("SAFETY GATE FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 2 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""Eval reports: a JSON file with every detail and a Markdown summary for humans."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.evals.metrics import CaseScore


def write_report(
    scores: list[CaseScore], summary: dict[str, Any], config: dict[str, Any], out_dir: Path
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    model = (summary["models"] or [config.get("model") or "unknown"])[0]
    base = out_dir / f"{stamp}-{model}"

    json_path = base.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {"config": config, "summary": summary, "runs": [_run(s) for s in scores]},
            indent=2,
            default=str,
        )
    )
    md_path = base.with_suffix(".md")
    md_path.write_text(render_markdown(scores, summary, config))
    return json_path, md_path


def gate_failures(summary: dict[str, Any]) -> list[str]:
    """Safety gate: any wrong write or false accept fails the eval."""
    safety = summary["safety"]
    return [
        f"{name} = {safety[name]}" for name in ("wrong_writes", "false_accepts") if safety[name]
    ]


def render_markdown(
    scores: list[CaseScore], summary: dict[str, Any], config: dict[str, Any]
) -> str:
    s, c, safety = summary, summary["commitments"], summary["safety"]
    gate = gate_failures(summary)
    lines = [
        f"# Eval report: {', '.join(s['models']) or config.get('model')}",
        "",
        f"- **Run:** {config['started_at']} · provider `{config['provider']}` · "
        f"{s['cases']} cases × {config['repeats']} repeat(s) = {s['runs']} runs",
        f"- **Safety gate:** {_gate_text(gate, s['errors'])}",
        f"- **Runs fully correct:** {s['runs_passed']}/{s['runs']} · errors: {s['errors']}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Wrong writes (target 0) | {safety['wrong_writes']} |",
        f"| False accepts (target 0) | {safety['false_accepts']} |",
        f"| Missed writes | {safety['missed_writes']} |",
        f"| Commitment precision / recall / F1 | {_pct(c['precision'])} / {_pct(c['recall'])} / "
        f"{_pct(c['f1'])} (TP {c['tp']}, FP {c['fp']}, FN {c['fn']}) |",
        f"| Email status accuracy | {_pct(s['status_accuracy'])} |",
        f"| Fraud flags accuracy | {_pct(s['flags_accuracy'])} |",
        f"| Field accuracy | {_pct(s['field_accuracy'])} |",
        f"| Consistency across repeats | {_pct(s['consistency'])} |",
        f"| Latency p50 / p95 | {_ms(s['latency_ms']['p50'])} / {_ms(s['latency_ms']['p95'])} |",
        f"| Model requests / tool calls | {s['model_requests']} / {s['tool_calls']} |",
        f"| Tokens in / out | {s['tokens']['input']:,} / {s['tokens']['output']:,} |",
        f"| Estimated cost | {format_cost(s['cost_usd'])} |",
        "",
        "## Field accuracy",
        "",
        "| Field | Accuracy |",
        "|---|---|",
        *[f"| {name} | {_pct(value)} |" for name, value in s["fields"].items()],
        "",
        "## Runs",
        "",
        "| Case | Result | Status | Flags | TP/FP/FN | Requests / tools | Latency |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in scores:
        status = r.status_got or "-"
        if r.status_ok is False:
            status += f" (want {r.status_expected})"
        lines.append(
            f"| {_run_name(r, config)} | {'pass' if r.passed else '**FAIL**'} | {status} | "
            f"{'ok' if r.flags_ok else '**wrong**'} | {r.tp}/{r.fp}/{r.fn} | "
            f"{r.stats.get('requests', '-')} / {r.stats.get('tool_calls', '-')} | "
            f"{_ms(r.stats.get('duration_ms'))} |"
        )
    failed = [r for r in scores if not r.passed]
    if failed:
        lines += ["", "## Problems", ""]
        for r in failed:
            lines.append(f"**{_run_name(r, config)}**")
            lines += [f"- {p}" for p in r.problems]
            lines.append("")
    return "\n".join(lines) + "\n"


def _run_name(score: CaseScore, config: dict[str, Any]) -> str:
    return score.case_id + (f" #{score.repeat + 1}" if config["repeats"] > 1 else "")


def _run(score: CaseScore) -> dict[str, Any]:
    data = asdict(score)
    data.update(status_ok=score.status_ok, flags_ok=score.flags_ok, passed=score.passed)
    return data


def _gate_text(gate: list[str], errors: int) -> str:
    if gate:
        return "FAILED (" + ", ".join(gate) + ")"
    if errors:
        return f"INCOMPLETE ({errors} run(s) errored; nothing was checked for them)"
    return "PASSED"


def format_cost(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _ms(value: float | None) -> str:
    return "-" if not value else f"{value / 1000:.1f}s"

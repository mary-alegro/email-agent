"""Score pipeline results against expected outcomes.

Matching: each expected commitment is paired with an unmatched predicted commitment that
has the same identity (PO, part, option flag), choosing the closest one by day range and
quantity. Unpaired expected commitments are misses (unless `optional`); unpaired predicted
ones are extras.

Safety metrics come first, because they decide whether the system of record can be trusted:
- wrong write: a commitment was written that shouldn't have been (an extra, or one the
  case says must not be written);
- false accept: written as `accepted` where the case requires `needs_review`.
"""

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from src.evals.dataset import IDENTITY_FIELDS, EvalCase, ExpectedCommitment

# USD per million input / output tokens, for the cost estimate (unknown models: no estimate).
PRICES = {"claude-opus-5-5": (4.0, 20.0)}


@dataclass
class CaseScore:
    case_id: str
    repeat: int
    error: str | None = None
    status_expected: str | None = None
    status_got: str | None = None
    flags_expected: list[str] = field(default_factory=list)
    flags_got: list[str] = field(default_factory=list)
    tp: int = 0
    fp: int = 0
    fn: int = 0
    wrong_writes: int = 0
    false_accepts: int = 0
    missed_writes: int = 0
    fields: dict[str, list[int]] = field(default_factory=dict)  # field -> [correct, total]
    problems: list[str] = field(default_factory=list)
    signature: str = ""  # outcome fingerprint, to measure consistency across repeats
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def status_ok(self) -> bool | None:
        return None if self.status_expected is None else self.status_expected == self.status_got

    @property
    def flags_ok(self) -> bool:
        return sorted(self.flags_expected) == sorted(self.flags_got)

    @property
    def passed(self) -> bool:
        return not self.problems


def flatten(commitment: dict) -> dict:
    """A CommitmentResult (API JSON) as one flat dict of comparable fields."""
    details = commitment.get("details") or {}
    return {
        **{
            k: commitment.get(k)
            for k in (*IDENTITY_FIELDS, "written", "review_status", "line_number")
        },
        **{
            k: details.get(k)
            for k in ("kind", "quantity", "min_days", "max_days", "day_basis", "anchor")
        },
    }


def score_case(
    case: EvalCase,
    result: dict | None,
    repeat: int = 0,
    error: str | None = None,
    stats: dict | None = None,
) -> CaseScore:
    expected = case.expected
    score = CaseScore(
        case_id=case.id,
        repeat=repeat,
        error=error,
        stats=stats or {},
        status_expected=expected.status,
        flags_expected=list(expected.flags),
    )
    required = [e for e in expected.commitments if not e.optional]
    if result is None or error:
        score.fn = len(required)
        score.missed_writes = sum(1 for e in required if e.written)
        score.problems.append(f"Run failed: {error or 'no result'}")
        return score

    score.status_got = result["status"]
    score.flags_got = [f["code"] for f in result.get("flags", [])]
    predicted = [flatten(c) for c in result.get("commitments", [])]
    score.signature = _signature(result["status"], predicted)

    if score.status_ok is False:
        score.problems.append(f"Status {score.status_got}, expected {score.status_expected}")
    if not score.flags_ok:
        score.problems.append(f"Flags {sorted(score.flags_got)}, expected {sorted(expected.flags)}")

    unmatched = list(range(len(predicted)))
    ordered = sorted(expected.commitments, key=lambda e: e.optional)  # required ones first
    for exp in ordered:
        index = _best_match(exp, predicted, unmatched)
        if index is None:
            if not exp.optional:
                score.fn += 1
                score.missed_writes += bool(exp.written)
                score.problems.append(f"Missing commitment {_label(exp.model_dump())}")
            continue
        unmatched.remove(index)
        score.tp += 1
        _compare(exp, predicted[index], score)

    for index in unmatched:
        extra = predicted[index]
        score.fp += 1
        if extra["written"]:
            score.wrong_writes += 1
            score.problems.append(f"WRONG WRITE: unexpected commitment written {_label(extra)}")
        else:
            score.problems.append(f"Unexpected commitment (not written) {_label(extra)}")
    return score


def _compare(exp: ExpectedCommitment, got: dict, score: CaseScore) -> None:
    for name in exp.checked_fields():
        want = getattr(exp, name)
        want = getattr(want, "value", want)  # enums -> plain values
        ok = got.get(name) == want
        tally = score.fields.setdefault(name, [0, 0])
        tally[0] += ok
        tally[1] += 1
        if not ok:
            score.problems.append(f"{_label(got)}: {name} = {got.get(name)!r}, expected {want!r}")
    if exp.written is False and got["written"]:
        score.wrong_writes += 1
        score.problems.append(f"WRONG WRITE: {_label(got)} was written")
    if exp.written and not got["written"]:
        score.missed_writes += 1
    if (
        exp.review_status == "needs_review"
        and got["written"]
        and got["review_status"] == "accepted"
    ):
        score.false_accepts += 1
        score.problems.append(f"FALSE ACCEPT: {_label(got)} accepted, needs review")


def _best_match(exp: ExpectedCommitment, predicted: list[dict], unmatched: list[int]) -> int | None:
    candidates = [
        i for i in unmatched if all(predicted[i][k] == getattr(exp, k) for k in IDENTITY_FIELDS)
    ]
    if not candidates:
        return None

    def distance(i: int) -> float:
        got = predicted[i]
        total = 0.0
        for name in ("min_days", "max_days", "quantity"):
            want = getattr(exp, name)
            if want is not None:
                total += abs((got[name] if got[name] is not None else -1000) - want)
        return total

    return min(candidates, key=distance)


def _label(c: dict) -> str:
    option = " (option)" if c.get("is_option") else ""
    days = f" {c.get('min_days')}-{c.get('max_days')}d" if c.get("min_days") is not None else ""
    qty = f" x{c['quantity']}" if c.get("quantity") else ""
    return f"[{c.get('po_number')}/{c.get('part_number')}{qty}{days}{option}]"


def _signature(status: str, predicted: list[dict]) -> str:
    keys = (
        "po_number",
        "part_number",
        "is_option",
        "written",
        "review_status",
        "min_days",
        "max_days",
    )
    return status + "|" + ";".join(sorted(",".join(str(c[k]) for k in keys) for c in predicted))


def summarize(scores: list[CaseScore]) -> dict[str, Any]:
    tp, fp, fn = (sum(getattr(s, a) for s in scores) for a in ("tp", "fp", "fn"))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    status_checked = [s for s in scores if s.status_ok is not None]

    fields: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s in scores:
        for name, (correct, total) in s.fields.items():
            fields[name][0] += correct
            fields[name][1] += total
    field_correct = sum(c for c, _ in fields.values())
    field_total = sum(t for _, t in fields.values())

    by_case: dict[str, set[str]] = defaultdict(set)
    for s in scores:
        by_case[s.case_id].add(s.signature)
    runs_per_case = Counter(s.case_id for s in scores)
    repeated = [sigs for case_id, sigs in by_case.items() if runs_per_case[case_id] > 1]

    ok_runs = [s for s in scores if not s.error]
    durations = sorted(s.stats["duration_ms"] for s in ok_runs if s.stats.get("duration_ms"))
    tokens_in = sum(s.stats.get("input_tokens") or 0 for s in ok_runs)
    tokens_out = sum(s.stats.get("output_tokens") or 0 for s in ok_runs)
    models = {s.stats.get("model") for s in ok_runs if s.stats.get("model")}
    price = PRICES.get(next(iter(models))) if len(models) == 1 else None

    return {
        "runs": len(scores),
        "cases": len(by_case),
        "runs_passed": sum(s.passed for s in scores),
        "errors": sum(1 for s in scores if s.error),
        "safety": {
            "wrong_writes": sum(s.wrong_writes for s in scores),
            "false_accepts": sum(s.false_accepts for s in scores),
            "missed_writes": sum(s.missed_writes for s in scores),
        },
        "commitments": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        },
        "status_accuracy": _ratio(
            sum(bool(s.status_ok) for s in status_checked), len(status_checked)
        ),
        "flags_accuracy": _ratio(sum(s.flags_ok for s in ok_runs), len(ok_runs)),
        "field_accuracy": _ratio(field_correct, field_total),
        "fields": {name: _ratio(c, t) for name, (c, t) in sorted(fields.items())},
        "consistency": _ratio(sum(len(sigs) == 1 for sigs in repeated), len(repeated))
        if repeated
        else None,
        "latency_ms": {
            "p50": statistics.median(durations) if durations else None,
            "p95": durations[min(len(durations) - 1, round(0.95 * (len(durations) - 1)))]
            if durations
            else None,
        },
        "model_requests": sum(s.stats.get("requests") or 0 for s in ok_runs),
        "tool_calls": sum(s.stats.get("tool_calls") or 0 for s in ok_runs),
        "tokens": {"input": tokens_in, "output": tokens_out},
        "cost_usd": round(tokens_in * price[0] / 1e6 + tokens_out * price[1] / 1e6, 4)
        if price
        else None,
        "models": sorted(models),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None

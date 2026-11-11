"""The eval harness itself: case files, scoring, and a full offline run (no LLM)."""

import json

from scripted import answer, scripted

from src.agent.schemas import ExtractedCommitment
from src.evals.dataset import CASES_DIR, load_cases
from src.evals.metrics import score_case, summarize
from src.evals.report import gate_failures, render_markdown
from src.evals.runner import run_cases


def case(case_id):
    return next(c for c in load_cases() if c.id == case_id)


def result(status="processed", flags=(), commitments=()):
    return {
        "status": status,
        "flags": [{"code": f} for f in flags],
        "commitments": list(commitments),
    }


def commitment(
    po="4500123456",
    part="JT-8831",
    option=False,
    written=True,
    review="accepted",
    line=10,
    qty=50,
    lo=42,
    hi=56,
):
    return {
        "po_number": po,
        "part_number": part,
        "is_option": option,
        "written": written,
        "review_status": review,
        "line_number": line,
        "details": {
            "kind": "duration",
            "quantity": qty,
            "min_days": lo,
            "max_days": hi,
            "day_basis": "calendar",
            "anchor": "ARO",
        },
    }


EXAMPLE_1_OK = result(
    commitments=[
        commitment(),
        commitment(option=True, written=False, review="needs_review", qty=None, lo=21, hi=21),
    ]
)


def test_cases_load_and_ids_match_file_names():
    cases = load_cases()
    assert len(cases) == 20
    assert [c.id for c in cases] == sorted(p.stem for p in CASES_DIR.glob("*.json"))
    assert {c.id for c in load_cases("fraud")} >= {"05_fraud_lookalike_bank_change"}


def test_perfect_result_passes():
    score = score_case(case("01_example_acme_expedite"), EXAMPLE_1_OK)
    assert score.passed, score.problems
    assert (score.tp, score.fp, score.fn) == (2, 0, 0)
    assert score.fields["min_days"] == [2, 2]


def test_wrong_write_false_accept_and_misses_are_caught():
    # The option got written as an accepted commitment: a wrong write.
    bad = json.loads(json.dumps(EXAMPLE_1_OK))
    bad["commitments"][1]["written"] = True
    score = score_case(case("01_example_acme_expedite"), bad)
    assert score.wrong_writes == 1 and not score.passed

    # Example 2 requires review; writing it as accepted is a false accept.
    accepted = result(
        status="processed",
        commitments=[
            commitment(po="4500999911", part="NB-4402", qty=20, lo=5, hi=5),
            commitment(
                po="4500999911", part="NB-4402", review="needs_review", qty=80, lo=25, hi=30
            ),
        ],
    )
    for c in accepted["commitments"]:
        c["details"]["day_basis"] = "business"
    score = score_case(case("02_example_northbay_partial"), accepted)
    assert score.false_accepts == 1
    assert any("Status processed" in p for p in score.problems)

    # Nothing extracted at all: both required commitments are missed.
    score = score_case(case("02_example_northbay_partial"), result("needs_review"))
    assert (score.fn, score.missed_writes) == (2, 2)


def test_optional_commitments_and_errors():
    # Example 3's second (conditional) commitment is optional: absent is fine.
    only_main = result(
        "needs_review",
        commitments=[
            {
                "po_number": None,
                "part_number": "AP-1187",
                "is_option": False,
                "written": False,
                "review_status": "needs_review",
                "line_number": None,
                "details": {"kind": "absolute_date"},
            }
        ],
    )
    assert score_case(case("03_example_rfq_no_po"), only_main).passed

    failed = score_case(case("01_example_acme_expedite"), None, error="ModelHTTPError: 529")
    assert (failed.fn, failed.missed_writes) == (2, 1) and not failed.passed  # main + option


def test_summary_and_gate():
    scores = [
        score_case(case("01_example_acme_expedite"), EXAMPLE_1_OK, repeat=0),
        score_case(case("01_example_acme_expedite"), EXAMPLE_1_OK, repeat=1),
    ]
    summary = summarize(scores)
    assert summary["commitments"]["f1"] == 1.0
    assert summary["consistency"] == 1.0
    assert gate_failures(summary) == []
    report = render_markdown(scores, summary, {"started_at": "now", "provider": "x", "repeats": 2})
    assert "**Safety gate:** PASSED" in report

    # Errored runs checked nothing, so the gate isn't reported as a plain PASS.
    errored = [score_case(case("01_example_acme_expedite"), None, error="ERP unreachable")]
    report = render_markdown(
        errored, summarize(errored), {"started_at": "now", "provider": "x", "repeats": 1}
    )
    assert "**Safety gate:** INCOMPLETE (1 run(s) errored" in report


# What a perfect model would answer for the cases used below, keyed by a line of the email.
PERFECT_ANSWERS = {
    "Thanks for the call": [],
    "PN NB-4402: we can ship the full quantity 10 business days ARO.": [
        ExtractedCommitment(
            po_number="4500999911",
            part_number="NB-4402",
            quantity=None,
            kind="duration",
            duration_min=10,
            duration_max=10,
            duration_unit="business_days",
            anchor="ARO",
            date=None,
            is_option=False,
            conditions=[],
            assumptions=[],
            segment_index=0,
            confidence=0.95,
            evidence=["PN NB-4402: we can ship the full quantity 10 business days ARO."],
        )
    ],
}


def perfect_model(messages, info):
    prompt = str(messages[0].parts[-1].content)
    for marker, commitments in PERFECT_ANSWERS.items():
        if marker in prompt:
            return answer(*commitments)
    raise AssertionError("no scripted answer for this email")


async def test_runner_end_to_end_with_a_scripted_model():
    cases = [case("09_no_signal"), case("11_ten_business_days")]
    with scripted(perfect_model):
        scores = await run_cases(cases, repeats=2)
    assert [s.passed for s in scores] == [True] * 4, [s.problems for s in scores]
    assert all(s.stats["requests"] == 1 for s in scores)  # usage comes from the result
    summary = summarize(scores)
    assert summary["safety"] == {"wrong_writes": 0, "false_accepts": 0, "missed_writes": 0}
    assert summary["consistency"] == 1.0

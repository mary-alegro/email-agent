"""Run eval cases through the real pipeline.

Each run uses the real agent (LLM + read-only MCP tools), the real MCP server and the real
code gate. Before every run the database is reset and reseeded, so runs are independent
(and the "already processed" cache never hides a run). The database is whatever
DATABASE_URL points to; the CLI points it at a throwaway file.
"""

import logging

from sqlmodel import Session

from src.agent.pipeline import process_email
from src.database.engine import engine, reset_db
from src.database.seed import seed
from src.evals.dataset import EvalCase
from src.evals.metrics import CaseScore, score_case

log = logging.getLogger(__name__)


async def run_cases(cases: list[EvalCase], repeats: int = 1) -> list[CaseScore]:
    scores = []
    for case in cases:
        for repeat in range(repeats):
            reset_db(engine)
            with Session(engine) as session:
                seed(session)
                result, error = None, None
                try:
                    email_result = await process_email(case.email, session)
                    result = email_result.model_dump(mode="json")
                    if email_result.status == "error":
                        error = "; ".join(email_result.issues) or "pipeline returned error"
                except Exception as exc:  # a crash is a result too: record it and go on
                    log.exception("Eval run crashed for %s", case.id)
                    error = f"{type(exc).__name__}: {exc}"
            stats = (result or {}).get("usage") or {}
            score = score_case(case, result, repeat=repeat, error=error, stats=stats)
            scores.append(score)
            mark = "PASS" if score.passed else "FAIL"
            print(f"  {mark}  {case.id}" + (f"  (repeat {repeat + 1})" if repeats > 1 else ""))
    return scores

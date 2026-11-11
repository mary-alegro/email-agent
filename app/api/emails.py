"""REST: submit a supplier email to the agent pipeline."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlmodel import Session

from src.agent.pipeline import process_email
from src.agent.schemas import EmailIn, EmailResult, EmailStatus
from src.database.engine import get_session

router = APIRouter(tags=["emails"])


@router.post(
    "/emails",
    response_model=EmailResult,
    responses={502: {"model": EmailResult, "description": "Processing failed; safe to retry"}},
)
async def submit_email(body: EmailIn, session: Annotated[Session, Depends(get_session)]):
    """Extract lead-time commitments from a supplier email and write them to the ERP.

    Returns a stable `message_id` (resubmitting the same email returns the same result),
    the commitments found with evidence and review status, email-level issues and flags,
    and the agent run's usage. An email with no lead-time information returns status
    `no_signal` and no commitments.
    """
    result = await process_email(body.email, session)
    if result.status == EmailStatus.ERROR:
        return JSONResponse(status_code=502, content=result.model_dump(mode="json"))
    return result

"""REST: read lead times from the ERP. A thin adapter over ERPService (no logic here)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from src.database.engine import get_session
from src.erp.schemas import LeadTimeRecord
from src.erp.service import ERPService

router = APIRouter(tags=["lead-times"])


def get_erp(session: Annotated[Session, Depends(get_session)]) -> ERPService:
    return ERPService(session)


@router.get("/lead-times", response_model=list[LeadTimeRecord])
def get_lead_time(
    erp: Annotated[ERPService, Depends(get_erp)],
    po_number: Annotated[str, Query(examples=["4500123456"])],
    part_number: Annotated[str | None, Query(examples=["JT-8831"])] = None,
) -> list[LeadTimeRecord]:
    """Current lead time for a PO, one record per PO line.

    With `part_number`, only the lines for that part (normally one); without it, every
    line of the PO. A record with an empty `lead_times` list means the line exists but no
    supplier commitment has been recorded yet. 404 if the PO doesn't exist or has no line
    for that part.
    """
    return erp.get_lead_time(po_number, part_number)

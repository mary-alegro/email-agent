"""ERP service: the shared business logic behind both the REST API and the MCP server.

Callers get and pass Pydantic schemas only; ORM objects never leave this module.
Business rules are enforced here, so they hold no matter which entry point is used.
"""

import logging

from sqlmodel import Session

from src.database import crud
from src.database.models import LeadTime, POLine
from src.erp.errors import InvalidLeadTimeError, NotFoundError
from src.erp.schemas import LeadTimeIn, LeadTimeOut, LeadTimeRecord, POLineOut, PurchaseOrderOut

log = logging.getLogger(__name__)


class ERPService:
    def __init__(self, session: Session):
        self.session = session

    # --- Reads ---

    def get_purchase_order(self, po_number: str) -> PurchaseOrderOut:
        po = crud.get_purchase_order(self.session, po_number)
        if po is None:
            raise NotFoundError(f"PO {po_number} not found")
        lines = crud.list_lines(self.session, po_number=po_number)
        return PurchaseOrderOut(
            po_number=po.po_number,
            supplier_name=po.supplier_name,
            supplier_domain=po.supplier_domain,
            order_date=po.order_date,
            lines=[_line_out(line) for line in lines],
        )

    def find_lines(
        self, po_number: str | None = None, part_number: str | None = None
    ) -> list[POLineOut]:
        """Find PO lines by PO number, part number, or both. Returns [] if nothing matches."""
        if po_number is None and part_number is None:
            raise ValueError("give po_number, part_number, or both")
        lines = crud.list_lines(self.session, po_number=po_number, part_number=part_number)
        return [_line_out(line) for line in lines]

    def get_lead_time(self, po_number: str, part_number: str | None = None) -> list[LeadTimeRecord]:
        """Current lead time per line of this PO: every line, or only the lines with this part."""
        if crud.get_purchase_order(self.session, po_number) is None:
            raise NotFoundError(f"PO {po_number} not found")
        lines = crud.list_lines(self.session, po_number=po_number, part_number=part_number)
        if not lines:
            raise NotFoundError(
                f"No line for part {part_number} on PO {po_number}"
                if part_number
                else f"PO {po_number} has no lines"
            )
        return [self._record(line) for line in lines]

    # --- Writes ---

    def write_lead_times(
        self, po_number: str, line_number: int, message_id: str, commitments: list[LeadTimeIn]
    ) -> LeadTimeRecord:
        """Set the lead-time commitments one email made for one PO line.

        Replaces anything previously written for the same (line, message), so
        reprocessing an email is idempotent.
        """
        line = crud.get_line(self.session, po_number, line_number)
        if line is None:
            # "Order is required": never create a lead time without an existing PO line.
            raise NotFoundError(f"PO {po_number} has no line {line_number}")
        if not message_id:
            raise InvalidLeadTimeError("message_id is required")
        if not commitments:
            raise InvalidLeadTimeError("at least one commitment is required")
        _check_quantities(line, commitments)

        rows = [
            LeadTime(
                po_line_id=line.id,
                message_id=message_id,
                review_status=c.review_status,
                confidence=c.confidence,
                details=c.details.model_dump(mode="json"),
            )
            for c in commitments
        ]
        crud.replace_lead_times(self.session, line.id, message_id, rows)
        log.info(
            "Lead times written for PO %s line %s",
            po_number,
            line_number,
            extra={
                "source_message_id": message_id,
                "rows": len(rows),
                "review_statuses": [str(c.review_status) for c in commitments],
            },
        )
        return self._record(line)

    # --- Helpers ---

    def _record(self, line: POLine) -> LeadTimeRecord:
        rows = crud.get_current_lead_times(self.session, line.id)
        return LeadTimeRecord(line=_line_out(line), lead_times=[_lead_time_out(r) for r in rows])


def _check_quantities(line: POLine, commitments: list[LeadTimeIn]) -> None:
    """Split-shipment quantities can't exceed what was ordered on the line."""
    quantities = [c.details.quantity for c in commitments if c.details.quantity is not None]
    if sum(quantities) > line.quantity:
        raise InvalidLeadTimeError(
            f"Committed quantity {sum(quantities)} exceeds line quantity {line.quantity}"
        )


def _line_out(line: POLine) -> POLineOut:
    return POLineOut.model_validate(line, from_attributes=True)


def _lead_time_out(row: LeadTime) -> LeadTimeOut:
    return LeadTimeOut.model_validate(row, from_attributes=True)  # details: JSON -> schema

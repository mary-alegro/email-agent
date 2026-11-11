"""MCP server for the mock ERP. Each tool is a thin wrapper over ERPService.

The tool docstrings become the tool descriptions that MCP clients (and LLMs) see,
so they say what the tool does and when it fails.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlmodel import Session

from src.database.engine import engine
from src.erp.errors import ERPError
from src.erp.schemas import LeadTimeIn, LeadTimeRecord, POLineOut, PurchaseOrderOut
from src.erp.service import ERPService

log = logging.getLogger(__name__)

mcp = FastMCP(
    name="mock-erp",
    instructions=(
        "Mock ERP system of record for purchase orders (POs). Read PO and line context, "
        "read current supplier lead times, and write extracted lead-time commitments. "
        "A lead time can only be written to an existing PO line."
    ),
)


@contextmanager
def _erp(tool: str) -> Iterator[ERPService]:
    """One DB session per tool call; domain errors become MCP tool errors (and are logged)."""
    log.debug("Tool call: %s", tool)
    with Session(engine) as session:
        try:
            yield ERPService(session)
        except (ERPError, ValueError) as exc:
            # Expected rejections (unknown PO, bad input): the caller gets the reason.
            log.info("Tool %s rejected: %s", tool, exc)
            raise ToolError(str(exc)) from exc
        except Exception:
            log.exception("Tool %s crashed", tool)
            raise


@mcp.tool(annotations={"readOnlyHint": True})
def get_purchase_order(po_number: str) -> PurchaseOrderOut:
    """Get a purchase order with its supplier, order date and lines. Errors if it doesn't exist."""
    with _erp("get_purchase_order") as erp:
        return erp.get_purchase_order(po_number)


@mcp.tool(annotations={"readOnlyHint": True})
def find_po_lines(po_number: str | None = None, part_number: str | None = None) -> list[POLineOut]:
    """Find PO lines by PO number, part number, or both. Returns an empty list if none match."""
    with _erp("find_po_lines") as erp:
        return erp.find_lines(po_number=po_number, part_number=part_number)


@mcp.tool(annotations={"readOnlyHint": True})
def get_lead_time(po_number: str, part_number: str | None = None) -> list[LeadTimeRecord]:
    """Get the current lead-time commitments for a PO, one record per line.

    With a part number, only the lines for that part; without one, every line of the PO.
    An empty `lead_times` list means no commitment has been recorded yet.
    Errors if the PO doesn't exist or has no line for that part.
    """
    with _erp("get_lead_time") as erp:
        return erp.get_lead_time(po_number, part_number)


@mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
def write_lead_times(
    po_number: str, line_number: int, message_id: str, commitments: list[LeadTimeIn]
) -> LeadTimeRecord:
    """Record the lead-time commitments a supplier email made for one existing PO line.

    Replaces anything previously written for the same line and message_id, so calling
    it again with the same message is safe. Errors if the PO line doesn't exist or if
    split quantities exceed the line quantity.
    """
    with _erp("write_lead_times") as erp:
        return erp.write_lead_times(po_number, line_number, message_id, commitments)

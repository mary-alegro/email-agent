"""ERP context for an email: which POs and lines it really refers to (via the MCP client).

1. `gather_context`: before the agent runs, look up the PO and part numbers the regexes
   found. This is the starting context the agent sees.
2. `add_agent_references`: after the agent runs, look up the POs and parts its answer
   uses that the regexes missed, but only if they actually appear in the email text, so
   the model can't steer the code to an arbitrary PO. The code does its own lookups
   rather than trusting what the agent saw through its tools.
"""

from src.agent.mcp_client import ERPClient
from src.agent.references import References
from src.agent.schemas import ERPContext, ExtractionResult
from src.utils.text import mentioned


async def gather_context(erp: ERPClient, refs: References) -> ERPContext:
    context = ERPContext()
    for po_number in refs.po_numbers:
        await _add_po(erp, context, po_number)
    for part in refs.part_numbers:
        await _add_part(erp, context, part)
    return context


async def add_agent_references(
    erp: ERPClient, extraction: ExtractionResult, email_text: str, context: ERPContext
) -> None:
    for c in extraction.commitments:
        if c.po_number and mentioned(c.po_number, email_text):
            await _add_po(erp, context, c.po_number)
        elif c.part_number and mentioned(c.part_number, email_text):
            await _add_part(erp, context, c.part_number)


def context_issues(context: ERPContext) -> list[str]:
    """Email-level issues about the PO references, for the API result."""
    issues = [
        f"PO {po} is referenced but not found in the ERP" for po in context.unknown_po_numbers
    ]
    if not context.purchase_orders and not context.unknown_po_numbers:
        issues.append("No PO number found in the email")
    return issues


async def _add_po(erp: ERPClient, context: ERPContext, po_number: str) -> None:
    """Look up a PO we haven't looked up yet; record it as found or unknown."""
    known = [p.po_number for p in context.purchase_orders] + context.unknown_po_numbers
    if po_number in known:
        return
    po = await erp.get_purchase_order(po_number)
    if po is None:
        context.unknown_po_numbers.append(po_number)
    else:
        context.purchase_orders.append(po)


async def _add_part(erp: ERPClient, context: ERPContext, part_number: str) -> None:
    """Look up a part that isn't on any PO we already have."""
    on_known_po = any(
        ln.part_number == part_number for po in context.purchase_orders for ln in po.lines
    )
    already = any(ln.part_number == part_number for ln in context.part_only_lines)
    if not (on_known_po or already):
        context.part_only_lines.extend(await erp.find_lines(part_number))

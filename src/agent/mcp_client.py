"""The agent's view of the ERP. Every read and write goes through the MCP server.

The connection target comes from config: a URL (streamable HTTP, e.g. a remote ERP
MCP server) or, by default, the in-process mock ERP server object. The agent code
is the same either way.
"""

import logging
from typing import Any

from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.config import settings
from src.erp.schemas import LeadTimeIn, LeadTimeRecord, POLineOut, PurchaseOrderOut

log = logging.getLogger(__name__)


class ERPUnavailableError(Exception):
    """The ERP's MCP server couldn't be reached. Processing can be retried later."""


def erp_mcp_target() -> Any:
    """Where the ERP's MCP server is: ERP_MCP_URL, or the in-process mock ERP server."""
    if settings.erp_mcp_url:
        return settings.erp_mcp_url
    from src.erp.mcp_server import mcp  # imported only when the in-process ERP is used

    return mcp


class ERPClient:
    """Typed wrapper around the ERP MCP tools. Use as `async with ERPClient() as erp:`.

    Used by the pipeline (lookups, writes) and the agent's output validator (lookups).
    The agent's own tools connect to the same server separately, read-only.
    """

    def __init__(self):
        self._client = Client(erp_mcp_target())

    async def __aenter__(self) -> "ERPClient":
        try:
            await self._client.__aenter__()
        except Exception as exc:  # fastmcp raises RuntimeError for connection failures
            raise ERPUnavailableError(f"ERP MCP server unreachable: {exc}") from exc
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._client.__aexit__(*exc_info)

    async def get_purchase_order(self, po_number: str) -> PurchaseOrderOut | None:
        """None if the PO doesn't exist."""
        try:
            data = await self._call("get_purchase_order", po_number=po_number)
        except ToolError:
            return None
        return PurchaseOrderOut.model_validate(data)

    async def find_lines(self, part_number: str) -> list[POLineOut]:
        data = await self._call("find_po_lines", part_number=part_number)
        return [POLineOut.model_validate(item) for item in data["result"]]

    async def write_lead_times(
        self, po_number: str, line_number: int, message_id: str, commitments: list[LeadTimeIn]
    ) -> LeadTimeRecord:
        """Raises ToolError if the ERP rejects the write (e.g. line missing, bad quantities)."""
        data = await self._call(
            "write_lead_times",
            po_number=po_number,
            line_number=line_number,
            message_id=message_id,
            commitments=[c.model_dump(mode="json") for c in commitments],
        )
        return LeadTimeRecord.model_validate(data)

    async def _call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        log.debug("Calling ERP tool %s", tool, extra={"arguments": list(arguments)})
        result = await self._client.call_tool(tool, arguments)
        return result.structured_content or {}

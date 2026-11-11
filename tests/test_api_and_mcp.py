"""REST endpoints and MCP tools are thin adapters over the same ERP service."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.erp.mcp_server import mcp

COMMITMENT = {
    "review_status": "accepted",
    "confidence": 0.92,
    "details": {
        "kind": "duration",
        "min_days": 42,
        "max_days": 56,
        "day_basis": "calendar",
        "anchor": "ARO",
        "raw_text": "6-8 weeks ARO",
        "evidence": ["6-8 weeks ARO"],
    },
}


def test_rest_get_lead_time(client):
    ok = client.get("/lead-times", params={"po_number": "4500123456", "part_number": "JT-8831"})
    assert ok.status_code == 200 and ok.json()[0]["lead_times"] == []

    missing = client.get("/lead-times", params={"po_number": "4500123456", "part_number": "X-1"})
    assert missing.status_code == 404

    unknown_po = client.get("/lead-times", params={"po_number": "4500000000"})
    assert unknown_po.status_code == 404 and "PO 4500000000 not found" in unknown_po.text

    assert client.get("/lead-times", params={"part_number": "JT-8831"}).status_code == 422
    assert client.get("/health").json() == {"status": "ok"}


def test_rest_get_lead_time_without_part_returns_every_line(client):
    response = client.get("/lead-times", params={"po_number": "4500123456"})
    assert response.status_code == 200
    records = response.json()
    assert [(r["line"]["line_number"], r["line"]["part_number"]) for r in records] == [
        (10, "JT-8831"),
        (20, "JT-9002"),
    ]
    assert all(r["lead_times"] == [] for r in records)


async def test_mcp_get_lead_time_with_and_without_part(client):
    async with Client(mcp) as c:
        await c.call_tool(
            "write_lead_times",
            {
                "po_number": "4500123456",
                "line_number": 10,
                "message_id": "msg-x",
                "commitments": [COMMITMENT],
            },
        )
        one = await c.call_tool(
            "get_lead_time", {"po_number": "4500123456", "part_number": "JT-8831"}
        )
        every = await c.call_tool("get_lead_time", {"po_number": "4500123456"})
    assert len(one.structured_content["result"]) == 1
    lines = [
        (r["line"]["part_number"], len(r["lead_times"])) for r in every.structured_content["result"]
    ]
    assert lines == [("JT-8831", 1), ("JT-9002", 0)]
    # REST and MCP agree.
    assert (
        client.get("/lead-times", params={"po_number": "4500123456"}).json()
        == every.structured_content["result"]
    )


async def test_mcp_tools():
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert names == {"get_purchase_order", "find_po_lines", "get_lead_time", "write_lead_times"}

        po = await c.call_tool("get_purchase_order", {"po_number": "4500123456"})
        assert [ln["part_number"] for ln in po.structured_content["lines"]] == [
            "JT-8831",
            "JT-9002",
        ]

        lines = await c.call_tool("find_po_lines", {"part_number": "AP-1187"})
        assert lines.structured_content == {"result": []}


@pytest.mark.parametrize(
    "tool, args",
    [
        ("get_purchase_order", {"po_number": "nope"}),
        ("find_po_lines", {}),
        (
            "write_lead_times",
            {
                "po_number": "4500123456",
                "line_number": 99,
                "message_id": "m",
                "commitments": [COMMITMENT],
            },
        ),
    ],
)
async def test_mcp_errors_become_tool_errors(tool, args):
    async with Client(mcp) as c:
        with pytest.raises(ToolError):
            await c.call_tool(tool, args)


async def test_mcp_write_is_visible_through_rest_and_mcp_reads(client):
    params = {"po_number": "4500123456", "part_number": "JT-8831"}
    async with Client(mcp) as c:
        await c.call_tool(
            "write_lead_times",
            {
                "po_number": "4500123456",
                "line_number": 10,
                "message_id": "msg-mcp",
                "commitments": [COMMITMENT],
            },
        )
        # The GET functionality is also an MCP tool, and both read through ERPService.
        mcp_read = (await c.call_tool("get_lead_time", params)).structured_content["result"]
    rest_read = client.get("/lead-times", params=params).json()
    assert mcp_read == rest_read
    rows = rest_read[0]["lead_times"]
    assert [(r["message_id"], r["details"]["max_days"]) for r in rows] == [("msg-mcp", 56)]

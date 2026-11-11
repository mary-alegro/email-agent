"""The agent's read-only ERP tools and hallucination checks, end to end.

FunctionModel scripts the model, including real tool calls that go through the ERP's
in-process MCP server against the test database.
"""

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo
from scripted import answer, retry_feedback, scripted

from src.agent.agent import READ_ONLY_ERP_TOOLS

# The regex looks for "PO 4500…"; "purchase order 4500…" slips past it.
MISSED_PO_EMAIL = """From: NorthBay Fasteners <quotes@northbayfasteners.com>
To: buyer@joby.aero
Subject: Lead time update

Hello,

For purchase order 4500999911, part NB-4402: all 100 pcs ship in 10 business days ARO.

Regards,
NorthBay Fasteners"""

QUOTE = "all 100 pcs ship in 10 business days ARO"


def commitment(**fields) -> dict:
    base = {
        "po_number": "4500999911",
        "part_number": "NB-4402",
        "quantity": 100,
        "kind": "duration",
        "duration_min": 10,
        "duration_max": 10,
        "duration_unit": "business_days",
        "anchor": "ARO",
        "date": None,
        "is_option": False,
        "conditions": [],
        "assumptions": [],
        "evidence": [QUOTE],
        "segment_index": 0,
        "confidence": 0.9,
    }
    return base | fields


def tool_returns(messages) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def post(client, text):
    response = client.post("/emails", json={"email": text})
    assert response.status_code == 200, response.text
    return response.json()


def test_agent_sees_only_read_only_erp_tools(client):
    seen = {}

    def model(messages, info: AgentInfo):
        seen["tools"] = {tool.name for tool in info.function_tools}
        return answer()

    with scripted(model):
        post(client, MISSED_PO_EMAIL)
    assert seen["tools"] == READ_ONLY_ERP_TOOLS
    assert "write_lead_times" not in seen["tools"]


def test_agent_looks_up_a_po_the_regex_missed_and_it_gets_written(client):
    def model(messages, info):
        if not tool_returns(messages):  # first turn: look the PO up
            return ModelResponse(
                parts=[ToolCallPart("get_purchase_order", {"po_number": "4500999911"})]
            )
        assert "NB-4402" in str(tool_returns(messages)[0].content)  # the tool really ran
        return answer(commitment())

    with scripted(model):
        result = post(client, MISSED_PO_EMAIL)

    assert result["po_numbers_found"] == []  # the regex missed it...
    assert "No PO number found in the email" not in result["issues"]
    written = result["commitments"][0]
    assert (written["po_number"], written["line_number"], written["written"]) == (
        "4500999911",
        10,
        True,
    )  # ...but the agent found it, the code verified it, and it was written
    assert result["status"] == "processed"


@pytest.mark.parametrize(
    "bad, expected_feedback",
    [
        (commitment(po_number="4500123456"), "PO 4500123456 is not mentioned in the email"),
        (commitment(part_number="JT-8831"), "Part JT-8831 is not mentioned in the email"),
    ],
)
def test_validator_sends_reference_problems_back(client, bad, expected_feedback):
    feedback = []

    def model(messages, info):
        feedback[:] = retry_feedback(messages)
        return answer(commitment()) if feedback else answer(bad)

    with scripted(model):
        result = post(client, MISSED_PO_EMAIL)
    assert any(expected_feedback in f for f in feedback)
    assert result["commitments"][0]["written"] is True  # corrected on the retry


def test_fraud_checks_cover_pos_the_agent_found(client):
    # The PO is only findable by the agent; the sender imitates that PO's supplier.
    spoofed = MISSED_PO_EMAIL.replace("quotes@northbayfasteners.com", "quotes@northbayfastener.com")

    def model(messages, info):
        if not tool_returns(messages):
            return ModelResponse(
                parts=[ToolCallPart("get_purchase_order", {"po_number": "4500999911"})]
            )
        return answer(commitment())

    with scripted(model):
        result = post(client, spoofed)
    assert [(f["code"], f["severity"]) for f in result["flags"]] == [("lookalike_domain", "high")]
    assert result["commitments"][0]["written"] is False


def test_a_real_conflict_in_the_email_is_reported_not_corrected(client):
    # The email itself pairs Acme's PO with NorthBay's part. The model reports it as written;
    # the validator must not push it to change the PO (the eval showed that leads to the PO
    # being dropped), and the gate must refuse to write.
    text = (
        "From: Acme Components <sales@acme-components.com>\nTo: buyer@joby.aero\n"
        "Subject: PO 4500123456\n\nHi Dominik,\n\nPO 4500123456 / PN NB-4402: 3 weeks ARO.\n"
    )
    feedback = []

    def model(messages, info):
        feedback.extend(retry_feedback(messages))
        return answer(
            commitment(
                po_number="4500123456",
                quantity=None,
                duration_min=3,
                duration_max=3,
                duration_unit="weeks",
                evidence=["PO 4500123456 / PN NB-4402: 3 weeks ARO."],
            )
        )

    with scripted(model):
        result = post(client, text)
    assert feedback == []
    written = result["commitments"][0]
    assert (written["po_number"], written["written"]) == ("4500123456", False)
    assert any("Part NB-4402 is not on PO 4500123456" in i for i in written["issues"])

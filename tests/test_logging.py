import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from scripted import answer, scripted

from src.agent.schemas import ExtractedCommitment
from src.utils.logging import ContextFilter, JsonFormatter, TextFormatter


@pytest.fixture
def log_output():
    """Capture formatted log lines exactly as the service would print them."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(TextFormatter())
    handler.addFilter(ContextFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.INFO)
    yield stream
    root.removeHandler(handler)
    root.setLevel(previous)


def test_request_id_is_generated_or_reused(client):
    generated = client.get("/health").headers["x-request-id"]
    assert len(generated) == 12

    reused = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert reused.headers["x-request-id"] == "abc-123"

    unsafe = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert unsafe.headers["x-request-id"] != "bad id\nwith newline"


def test_unexpected_error_returns_request_id_and_logs_traceback(log_output, email_texts):
    from app.main import app

    def broken_model(messages, info):
        raise RuntimeError("boom")  # an unexpected bug, not a model/API error

    with scripted(broken_model), TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/emails", json={"email": email_texts["01"]}, headers={"X-Request-ID": "bug-42"}
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "request_id": "bug-42"}
    logs = log_output.getvalue()
    assert "[req=bug-42 msg=msg_" in logs  # the error line carries both IDs
    assert "Traceback" in logs and "RuntimeError: boom" in logs


def test_message_id_reaches_erp_and_mcp_log_lines(client, log_output, email_texts):
    commitment = ExtractedCommitment(
        po_number="4500123456",
        part_number="JT-8831",
        quantity=50,
        kind="duration",
        duration_min=6,
        duration_max=8,
        duration_unit="weeks",
        anchor="ARO",
        date=None,
        is_option=False,
        conditions=[],
        assumptions=[],
        segment_index=0,
        confidence=0.95,
        evidence=["For PN JT-8831 we can ship 50 units in 6–8 weeks ARO."],
    )

    with scripted(answer(commitment)):
        result = client.post("/emails", json={"email": email_texts["01"]}).json()

    lines = log_output.getvalue().splitlines()
    tagged = [ln for ln in lines if f"msg={result['message_id']}" in ln]
    # The ERP write happens inside the MCP tool (in a worker thread): still tagged.
    assert any("Lead times written for PO 4500123456 line 10" in ln for ln in tagged)
    assert any("Email processed" in ln and "status=processed" in ln for ln in tagged)
    assert any("POST /emails status=200" in ln for ln in lines)


def test_json_format():
    record = logging.makeLogRecord(
        {"name": "x", "levelno": logging.INFO, "levelname": "INFO", "msg": "hi", "status": 200}
    )
    ContextFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "hi" and payload["status"] == 200
    assert {"ts", "level", "logger", "request_id", "message_id"} <= payload.keys()


def _format(**fields) -> str:
    record = logging.makeLogRecord(
        {"levelno": logging.INFO, "levelname": "INFO", "msg": "hi", **fields}
    )
    ContextFilter().filter(record)
    for key in ("request_id", "message_id"):
        if key in fields:
            setattr(record, key, fields[key])
    return TextFormatter().format(record)


def test_text_format_hides_empty_ids_and_renames_uvicorn_logger():
    startup = _format(name="uvicorn.error")
    assert " uvicorn hi" in startup and "req=" not in startup and "uvicorn.error" not in startup

    in_request = _format(name="app.requests", request_id="abc", message_id="-", status=200)
    assert in_request.endswith("app.requests [req=abc msg=-] hi status=200")

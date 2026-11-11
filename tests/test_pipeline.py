"""End-to-end pipeline through POST /emails, with the agent's model scripted.

Everything but the model is real: the REST app, the PydanticAI agent and its output
validator, the MCP calls, the database. Each test gives the model a fixed answer, so
these tests check the deterministic parts: validation, normalization, PO-line
resolution, dedupe, review rules, fraud blocking, writeback and idempotency.
"""

import pytest
from scripted import answer, scripted

from src.agent.schemas import ExtractedCommitment


def c(**fields) -> ExtractedCommitment:
    base = dict(
        po_number=None,
        part_number=None,
        quantity=None,
        kind="duration",
        duration_min=None,
        duration_max=None,
        duration_unit=None,
        anchor=None,
        date=None,
        is_option=False,
        conditions=[],
        assumptions=[],
        evidence=[],
        segment_index=0,
        confidence=0.95,
    )
    return ExtractedCommitment(**(base | fields))


ACME = dict(po_number="4500123456", part_number="JT-8831")
NORTHBAY = dict(po_number="4500999911", part_number="NB-4402")
WEEKS_ARO = dict(duration_unit="weeks", anchor="ARO")

CANNED = {
    "01": [
        c(
            **ACME,
            **WEEKS_ARO,
            quantity=50,
            duration_min=6,
            duration_max=8,
            evidence=["For PN JT-8831 we can ship 50 units in 6–8 weeks ARO."],
        ),
        c(
            **ACME,
            **WEEKS_ARO,
            duration_min=3,
            duration_max=3,
            is_option=True,
            conditions=["+12% surcharge"],
            evidence=["we can do an expedited build at 3 weeks ARO for +12% surcharge"],
        ),
    ],
    "02": [
        c(
            **NORTHBAY,
            quantity=20,
            duration_min=5,
            duration_max=5,
            duration_unit="business_days",
            anchor="email_date",
            conditions=["buyer must confirm partial shipment"],
            assumptions=["'available to ship in 5 business days' counted from the email date"],
            evidence=["PN NB-4402: 20 pcs available to ship in 5 business days."],
        ),
        c(
            **NORTHBAY,
            quantity=80,
            duration_min=25,
            duration_max=30,
            duration_unit="business_days",
            anchor="ARO",
            conditions=["buyer must confirm partial shipment"],
            evidence=["Remaining 80 pcs will ship in 25–30 business days after receipt of order."],
        ),
    ],
    "03": [
        c(
            part_number="AP-1187",
            kind="absolute_date",
            date="2027-06-15",
            conditions=["drawing approval by end of week"],
            confidence=0.85,
            evidence=[
                "Assuming we receive drawing approval by EOW, we can deliver PN AP-1187 by June 15."
            ],
        ),
    ],
    "04": [
        c(
            **ACME,
            **WEEKS_ARO,
            duration_min=6,
            duration_max=8,
            evidence=["Confirming we can ship PN JT-8831 in 6–8 weeks ARO for the full quantity."],
        ),
        c(
            **ACME,
            **WEEKS_ARO,
            quantity=20,
            duration_min=3,
            duration_max=3,
            is_option=True,
            evidence=["If you can take a split shipment: 20 pcs in 3 weeks ARO"],
        ),
        # The same commitment restated in quoted history: must be deduped.
        c(
            **ACME,
            **WEEKS_ARO,
            duration_min=6,
            duration_max=8,
            segment_index=2,
            evidence=["For PO 4500123456 / PN JT-8831 our standard lead time is 6–8 weeks ARO."],
        ),
    ],
    # Email 4 again, but the quoted restatement reads as "50 units" while the latest
    # message says "the full quantity": not an exact repeat, still must not be written twice.
    "04_restated": [
        c(
            **ACME,
            **WEEKS_ARO,
            duration_min=6,
            duration_max=8,
            evidence=["Confirming we can ship PN JT-8831 in 6–8 weeks ARO for the full quantity."],
        ),
        c(
            **ACME,
            **WEEKS_ARO,
            quantity=50,
            duration_min=6,
            duration_max=8,
            segment_index=2,
            evidence=["For PO 4500123456 / PN JT-8831 our standard lead time is 6–8 weeks ARO."],
        ),
    ],
    "hallucinated": [
        c(**ACME, **WEEKS_ARO, duration_min=2, duration_max=2, evidence=["We ship in 2 weeks."]),
    ],
    # Fraud fixtures: the extraction itself is fine; the checks decide what's written.
    "05": [
        c(
            **ACME,
            **WEEKS_ARO,
            duration_min=6,
            duration_max=8,
            evidence=["Confirming PN JT-8831 will ship in 6–8 weeks ARO for the full quantity."],
        ),
    ],
    "06": [
        c(
            **ACME,
            **WEEKS_ARO,
            quantity=50,
            duration_min=6,
            duration_max=8,
            evidence=["For PN JT-8831 we can ship 50 units in 6–8 weeks ARO."],
        ),
    ],
    "07": [
        c(
            **NORTHBAY,
            quantity=100,
            duration_min=10,
            duration_max=10,
            duration_unit="business_days",
            anchor="ARO",
            evidence=[
                "PN NB-4402: all 100 pcs will ship in 10 business days after receipt of order."
            ],
        ),
    ],
}


@pytest.fixture
def emails(email_texts):
    texts = dict(email_texts)
    texts["hallucinated"] = texts["01"].replace("Best,", "Cheers,")
    texts["04_restated"] = texts["04"].replace("Regards,", "Thanks,")
    texts["no_signal"] = "From: a@vendor.com\nSubject: Hello\n\nThanks for the meeting!"
    return texts


def current_lead_times(client, po_number, part_number):
    params = {"po_number": po_number, "part_number": part_number}
    return client.get("/lead-times", params=params).json()[0]["lead_times"]


def post(client, text, commitments=()):
    """Submit an email; the agent's model answers with `commitments`."""
    with scripted(answer(*commitments)):
        response = client.post("/emails", json={"email": text})
    assert response.status_code == 200, response.text
    return response.json()


def summary(result):
    return [(cm["written"], cm["review_status"], cm["is_option"]) for cm in result["commitments"]]


def test_email_1_main_commitment_written_option_reported(client, emails):
    result = post(client, emails["01"], CANNED["01"])
    assert result["status"] == "processed"
    assert summary(result) == [(True, "accepted", False), (False, "needs_review", True)]
    details = result["commitments"][0]["details"]
    # 6-8 weeks after the PO order date (2026-05-15)
    assert (details["min_days"], details["max_days"]) == (42, 56)
    assert (details["promised_date_min"], details["promised_date_max"]) == (
        "2026-06-26",
        "2026-07-10",
    )


def test_email_2_conditional_split_written_for_review(client, emails):
    result = post(client, emails["02"], CANNED["02"])
    assert result["status"] == "needs_review"
    assert summary(result) == [(True, "needs_review", False)] * 2
    assert result["commitments"][1]["details"]["day_basis"] == "business"

    # The ERP record carries what downstream needs: basis, conditions, assumptions, evidence.
    first = current_lead_times(client, "4500999911", "NB-4402")[0]
    assert first["review_status"] == "needs_review"
    assert first["details"]["conditions"] == ["buyer must confirm partial shipment"]
    assert first["details"]["assumptions"] == [
        "'available to ship in 5 business days' counted from the email date"
    ]
    assert first["details"]["evidence"]


def test_email_3_no_po_line_means_no_write(client, emails):
    result = post(client, emails["03"], CANNED["03"])
    assert result["status"] == "needs_review"
    assert "No PO number found in the email" in result["issues"]
    commitment = result["commitments"][0]
    assert commitment["written"] is False
    assert any("No PO line in ERP for part AP-1187" in i for i in commitment["issues"])
    assert any("Year not stated" in i for i in commitment["issues"])
    assert any("assumed 2027" in a for a in commitment["details"]["assumptions"])


def test_email_4_thread_deduped_and_latest_wins(client, emails):
    post(client, emails["01"], CANNED["01"])
    result = post(client, emails["04"], CANNED["04"])
    assert result["status"] == "processed"
    assert len(result["commitments"]) == 2  # quoted restatement removed

    rows = current_lead_times(client, "4500123456", "JT-8831")
    assert [r["message_id"] for r in rows] == [result["message_id"]]


def test_email_4_older_restatement_is_superseded_not_written_twice(client, emails):
    result = post(client, emails["04_restated"], CANNED["04_restated"])
    assert result["status"] == "processed"
    assert len(result["commitments"]) == 1
    assert len(current_lead_times(client, "4500123456", "JT-8831")) == 1


def test_hallucinated_evidence_is_never_written(client, emails):
    result = post(client, emails["hallucinated"], CANNED["hallucinated"])
    assert summary(result) == [(False, "needs_review", False)]
    assert "No evidence quote could be found in the email" in result["commitments"][0]["issues"]


def test_no_signal_is_not_a_failure(client, emails):
    result = post(client, emails["no_signal"])
    assert result["status"] == "no_signal" and result["commitments"] == []


def test_resubmitting_returns_stored_result_without_new_llm_call(client, emails):
    first = post(client, emails["01"], CANNED["01"])
    calls = []

    def model(messages, info):
        calls.append(1)
        return answer(*CANNED["01"])

    with scripted(model):
        again = client.post("/emails", json={"email": emails["01"]}).json()
    assert again == first
    assert calls == []


def flag_codes(result):
    return sorted((f["code"], f["severity"]) for f in result["flags"])


def test_fraud_high_severity_blocks_the_write(client, emails):
    # Lookalike sender domain + new bank details in the same email.
    result = post(client, emails["05"], CANNED["05"])
    assert flag_codes(result) == [
        ("lookalike_domain", "high"),
        ("payment_instruction_change", "high"),
    ]
    assert result["status"] == "needs_review"
    assert summary(result) == [(False, "needs_review", False)]
    assert any(i.startswith("Blocked by fraud check") for i in result["commitments"][0]["issues"])
    assert current_lead_times(client, "4500123456", "JT-8831") == []


def test_fraud_reply_to_redirect_blocks_the_write(client, emails):
    result = post(client, emails["06"], CANNED["06"])
    assert flag_codes(result) == [("reply_to_mismatch", "high")]
    assert summary(result) == [(False, "needs_review", False)]
    assert current_lead_times(client, "4500123456", "JT-8831") == []


def test_fraud_medium_severity_writes_for_review(client, emails):
    # A supplier writing from another of its domains: plausible, so written but reviewed.
    result = post(client, emails["07"], CANNED["07"])
    assert flag_codes(result) == [("sender_domain_mismatch", "medium")]
    assert summary(result) == [(True, "needs_review", False)]
    rows = current_lead_times(client, "4500999911", "NB-4402")
    assert [(r["review_status"], r["details"]["max_days"]) for r in rows] == [("needs_review", 10)]


def test_flagged_email_without_commitments_needs_review(client):
    # A bank-change phishing email usually has no lead time at all: zero extractions,
    # but the flag must not be hidden behind status "no_signal".
    text = (
        "From: Acme Components <sales@acme-components.com>\nTo: buyer@joby.aero\n"
        "Subject: PO 4500123456\n\nPlease note our new bank details for all future payments.\n"
    )
    result = post(client, text)
    assert flag_codes(result) == [("payment_instruction_change", "high")]
    assert result["commitments"] == []
    assert result["status"] == "needs_review"


def test_example_emails_are_not_flagged(client, emails):
    for key in ("01", "02", "03", "04"):
        assert post(client, emails[key], CANNED[key])["flags"] == [], key


def test_named_po_is_never_swapped_for_another_po_by_part_number(client):
    # Regression (found by the eval harness): Acme names its own PO but NorthBay's part
    # number; the model drops the PO number; the part-only fallback must not then write
    # the commitment to NorthBay's PO.
    text = (
        "From: Acme Components <sales@acme-components.com>\nTo: buyer@joby.aero\n"
        "Subject: PO 4500123456\n\nHi Dominik,\n\nPO 4500123456 / PN NB-4402: 3 weeks ARO.\n"
    )
    model_answer = c(
        po_number=None,
        part_number="NB-4402",
        duration_min=3,
        duration_max=3,
        duration_unit="weeks",
        anchor="ARO",
        evidence=["PO 4500123456 / PN NB-4402: 3 weeks ARO."],
    )
    result = post(client, text, [model_answer])
    assert summary(result) == [(False, "needs_review", False)]
    assert any("not matched to another PO" in i for i in result["commitments"][0]["issues"])
    assert current_lead_times(client, "4500999911", "NB-4402") == []


def test_unreachable_erp_is_a_retryable_error_not_a_crash(client, emails, monkeypatch):
    # Nothing listens on port 9: the ERP's MCP server can't be reached.
    from src.config import settings

    monkeypatch.setattr(settings, "erp_mcp_url", "http://127.0.0.1:9/mcp")
    response = client.post("/emails", json={"email": emails["01"]})
    assert response.status_code == 502  # like an LLM outage: safe to resubmit
    result = response.json()
    assert result["status"] == "error" and result["commitments"] == []
    assert any("ERP MCP server unreachable" in i for i in result["issues"])

import pytest
from sqlalchemy.exc import IntegrityError

from src.database import crud
from src.database.models import Email, EmailStatus, LeadTime, ReviewStatus
from src.database.seed import seed
from src.erp.schemas import LeadTimeDetails


def _row(qty, lo, hi, text):
    details = LeadTimeDetails(
        kind="duration",
        quantity=qty,
        min_days=lo,
        max_days=hi,
        day_basis="business",
        anchor="ARO",
        raw_text=text,
        evidence=[text],
    )
    return LeadTime(
        po_line_id=0,
        message_id="",
        review_status=ReviewStatus.NEEDS_REVIEW,
        confidence=0.8,
        details=details.model_dump(mode="json"),
    )


def test_seed_is_idempotent(session):
    seed(session)
    assert len(crud.list_lines(session)) == 3


def test_list_lines_filters(session):
    assert [ln.line_number for ln in crud.list_lines(session, po_number="4500123456")] == [10, 20]
    assert crud.list_lines(session, part_number="AP-1187") == []


def test_replace_lead_times_is_idempotent_and_latest_message_wins(session):
    line = crud.get_line(session, "4500999911", 10)
    split = lambda: [_row(20, 5, 5, "20 pcs"), _row(80, 25, 30, "80 pcs")]  # noqa: E731
    crud.replace_lead_times(session, line.id, "msg-a", split())
    crud.replace_lead_times(session, line.id, "msg-a", split())
    assert len(crud.get_current_lead_times(session, line.id)) == 2

    crud.replace_lead_times(session, line.id, "msg-b", [_row(None, 10, 10, "10 days")])
    current = crud.get_current_lead_times(session, line.id)
    assert [r.message_id for r in current] == ["msg-b"]


def test_foreign_key_blocks_lead_time_without_po_line(session):
    with pytest.raises(IntegrityError):
        crud.replace_lead_times(session, 9999, "msg-x", [_row(None, 1, 2, "x")])


def test_save_email_upserts(session):
    crud.save_email(
        session, Email(message_id="m", raw_body="b", status=EmailStatus.ERROR, result={})
    )
    crud.save_email(
        session, Email(message_id="m", raw_body="b", status=EmailStatus.PROCESSED, result={})
    )
    assert crud.get_email(session, "m").status == EmailStatus.PROCESSED

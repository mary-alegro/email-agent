"""Plain database access. No business rules here (those live in the ERP service and agent)."""

from sqlmodel import Session, col, delete, select

from src.database.models import Email, LeadTime, POLine, PurchaseOrder


def get_purchase_order(session: Session, po_number: str) -> PurchaseOrder | None:
    return session.get(PurchaseOrder, po_number)


def list_lines(
    session: Session, po_number: str | None = None, part_number: str | None = None
) -> list[POLine]:
    query = select(POLine)
    if po_number is not None:
        query = query.where(POLine.po_number == po_number)
    if part_number is not None:
        query = query.where(POLine.part_number == part_number)
    return list(session.exec(query.order_by(POLine.po_number, POLine.line_number)))


def get_line(session: Session, po_number: str, line_number: int) -> POLine | None:
    query = select(POLine).where(POLine.po_number == po_number, POLine.line_number == line_number)
    return session.exec(query).first()


def get_current_lead_times(session: Session, po_line_id: int) -> list[LeadTime]:
    """All rows from the most recent message written for this line (empty if none)."""
    latest = session.exec(
        select(LeadTime)
        .where(LeadTime.po_line_id == po_line_id)
        .order_by(col(LeadTime.created_at).desc(), col(LeadTime.id).desc())
    ).first()
    if latest is None:
        return []
    query = select(LeadTime).where(
        LeadTime.po_line_id == po_line_id, LeadTime.message_id == latest.message_id
    )
    return list(session.exec(query.order_by(LeadTime.id)))


def replace_lead_times(
    session: Session, po_line_id: int, message_id: str, rows: list[LeadTime]
) -> list[LeadTime]:
    """Replace this message's rows for this line in one transaction.

    Reprocessing the same email therefore never creates duplicates.
    """
    session.exec(
        delete(LeadTime).where(LeadTime.po_line_id == po_line_id, LeadTime.message_id == message_id)
    )
    for row in rows:
        row.po_line_id = po_line_id
        row.message_id = message_id
        session.add(row)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


# --- Emails (agent log) ---


def get_email(session: Session, message_id: str) -> Email | None:
    return session.get(Email, message_id)


def save_email(session: Session, email: Email) -> Email:
    """Insert or update by message_id."""
    email = session.merge(email)
    session.commit()
    session.refresh(email)
    return email

"""All database tables, plus the status enums they persist.

ERP tables (the mock system of record): purchase_orders, po_lines, lead_times.
Agent table: emails (the log of processed emails).
"""

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class ReviewStatus(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_REVIEW = "needs_review"


class EmailStatus(StrEnum):
    PROCESSED = "processed"  # commitments written to the ERP, nothing needs a human
    NEEDS_REVIEW = "needs_review"  # something needs a human (no PO match, conditions, flags...)
    NO_SIGNAL = "no_signal"  # no lead-time commitment in the email; not a failure
    ERROR = "error"  # processing failed (e.g. LLM unavailable); safe to resubmit


# --- ERP ---


class PurchaseOrder(SQLModel, table=True):
    __tablename__ = "purchase_orders"

    po_number: str = Field(primary_key=True)
    supplier_name: str
    supplier_domain: str  # e.g. "acme-components.com"; used by the spoofing checks
    order_date: date  # anchor for "ARO" (after receipt of order) lead times


class POLine(SQLModel, table=True):
    __tablename__ = "po_lines"
    __table_args__ = (UniqueConstraint("po_number", "line_number"),)

    id: int | None = Field(default=None, primary_key=True)
    po_number: str = Field(foreign_key="purchase_orders.po_number", index=True)
    line_number: int
    part_number: str = Field(index=True)
    quantity: int


class LeadTime(SQLModel, table=True):
    """One lead-time commitment for a PO line, extracted from one email.

    A split shipment ("20 pcs in 2 weeks, remainder in 6") is several rows with the
    same message_id. The current lead time of a line is the set of rows from its
    most recent message.
    """

    __tablename__ = "lead_times"

    id: int | None = Field(default=None, primary_key=True)
    # NOT NULL foreign key: there can be no lead time without an existing PO line.
    po_line_id: int = Field(foreign_key="po_lines.id", index=True)
    # Logical reference to the source email (not an FK on purpose: the ERP stays
    # self-contained and could live in a separate database).
    message_id: str = Field(index=True)
    review_status: ReviewStatus
    confidence: float = Field(ge=0.0, le=1.0)
    details: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))  # LeadTimeDetails
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- Agent ---


class Email(SQLModel, table=True):
    __tablename__ = "emails"

    message_id: str = Field(primary_key=True)  # sha256 of the normalized body
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sender: str | None = None
    subject: str | None = None
    raw_body: str = Field(sa_column=Column(Text, nullable=False))
    status: EmailStatus
    result: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))  # full POST response

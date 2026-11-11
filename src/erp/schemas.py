"""Domain types shared by the ERP service, REST API, MCP server and agent."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.database.models import ReviewStatus


class LeadTimeDetails(BaseModel):
    """A normalized lead-time commitment. Stored as JSON in `lead_times.details`."""

    kind: Literal["duration", "absolute_date", "in_stock"]
    quantity: int | None = Field(None, gt=0, description="None means the full line quantity.")

    # Duration commitments, e.g. "6-8 weeks ARO" -> min_days=42, max_days=56.
    min_days: int | None = Field(None, ge=0)
    max_days: int | None = Field(None, ge=0)
    day_basis: Literal["calendar", "business"] | None = None
    # What the countdown starts from: receipt of order, an approval, the email date, or unknown.
    anchor: Literal["ARO", "approval", "email_date", "unspecified"] | None = None

    # Dates computed from the PO order date (durations) or stated directly (absolute dates).
    promised_date_min: date | None = None
    promised_date_max: date | None = None

    conditions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list,
        description="Inferences to double-check: an assumed year, a computed remainder, ...",
    )
    raw_text: str
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> "LeadTimeDetails":
        if self.kind == "duration" and (self.min_days is None or self.max_days is None):
            raise ValueError("duration commitments need min_days and max_days")
        if self.min_days is not None and self.max_days is not None:
            if self.min_days > self.max_days:
                raise ValueError("min_days must be <= max_days")
        if self.promised_date_min and self.promised_date_max:
            if self.promised_date_min > self.promised_date_max:
                raise ValueError("promised_date_min must be <= promised_date_max")
        return self


# --- Service inputs / outputs (what REST and MCP expose; never ORM objects) ---


class POLineOut(BaseModel):
    po_number: str
    line_number: int
    part_number: str
    quantity: int


class PurchaseOrderOut(BaseModel):
    po_number: str
    supplier_name: str
    supplier_domain: str
    order_date: date
    lines: list[POLineOut]


class LeadTimeIn(BaseModel):
    """One commitment to write to a PO line."""

    review_status: ReviewStatus
    confidence: float = Field(ge=0.0, le=1.0)
    details: LeadTimeDetails


class LeadTimeOut(LeadTimeIn):
    id: int
    message_id: str
    created_at: datetime


class LeadTimeRecord(BaseModel):
    """A PO line and its current lead-time commitments (empty list = none received yet)."""

    line: POLineOut
    lead_times: list[LeadTimeOut]

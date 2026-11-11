"""Agent data types: what the LLM returns, the ERP context it gets, and the API result."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from src.database.models import EmailStatus, ReviewStatus
from src.erp.schemas import LeadTimeDetails, POLineOut, PurchaseOrderOut

# --- LLM output (structured output schema) ---
# Kept free of numeric constraints and conversions: the model copies what the email says,
# and code does the arithmetic and the validation.


class ExtractedCommitment(BaseModel):
    po_number: str | None = Field(description="PO number the commitment applies to, or null.")
    part_number: str | None = Field(description="Part number the commitment applies to, or null.")
    quantity: int | None = Field(
        description="Units this commitment covers; null if it is the full/unspecified quantity."
    )
    kind: Literal["duration", "absolute_date", "in_stock"]
    duration_min: float | None = Field(description="Lower bound as written ('6-8 weeks' -> 6).")
    duration_max: float | None = Field(description="Upper bound as written ('6-8 weeks' -> 8).")
    duration_unit: Literal["days", "business_days", "weeks", "months"] | None
    anchor: Literal["ARO", "approval", "email_date", "unspecified"] | None = Field(
        description="What a duration counts from. ARO = after receipt of order."
    )
    date: str | None = Field(description="ISO date (YYYY-MM-DD) for dated commitments, else null.")
    is_option: bool = Field(
        description="True if this is an alternative the buyer may choose instead of the main "
        "commitment (e.g. expedite for a surcharge, a split shipment if the buyer agrees)."
    )
    conditions: list[str] = Field(description="Conditions the commitment depends on.")
    assumptions: list[str] = Field(description="Anything you had to infer (e.g. the year).")
    evidence: list[str] = Field(description="Exact quotes from the email supporting this.")
    segment_index: int = Field(description="Index of the email segment the commitment is from.")
    confidence: float = Field(description="0.0-1.0: how sure you are this is a real commitment.")


class ExtractionResult(BaseModel):
    commitments: list[ExtractedCommitment]


# --- ERP context: gathered via MCP before extraction, extended by the reference checks ---


class ERPContext(BaseModel):
    purchase_orders: list[PurchaseOrderOut] = Field(default_factory=list)
    # Lines found only by part number (the email didn't name a PO we could find).
    part_only_lines: list[POLineOut] = Field(default_factory=list)
    unknown_po_numbers: list[str] = Field(default_factory=list)


# --- API input / output ---


class EmailIn(BaseModel):
    email: str = Field(min_length=1, description="Raw email text, with headers and quoted thread.")


class CommitmentResult(BaseModel):
    po_number: str | None
    part_number: str | None
    line_number: int | None = Field(description="Resolved ERP line; null if none matched.")
    written: bool = Field(description="True if this commitment was written to the ERP.")
    review_status: ReviewStatus
    is_option: bool
    confidence: float
    details: LeadTimeDetails | None = Field(description="Normalized lead time; null if invalid.")
    issues: list[str] = Field(description="Why it needs review or wasn't written.")


class Flag(BaseModel):
    """A fraud/spoofing signal raised by an email-level check (src/agent/checks.py)."""

    code: str = Field(description="Stable identifier, e.g. 'lookalike_domain'.")
    severity: Literal["medium", "high"] = Field(
        description="medium: commitments are written as needs_review. "
        "high: nothing from this email is written to the ERP."
    )
    message: str
    evidence: str | None = Field(None, description="The header or text that triggered it.")


class AgentUsage(BaseModel):
    """What the agent run cost: model, time, round trips, tool calls, tokens."""

    model: str | None
    duration_ms: int
    requests: int
    tool_calls: int
    input_tokens: int
    output_tokens: int


class EmailResult(BaseModel):
    message_id: str
    status: EmailStatus
    reference_date: date | None = None
    po_numbers_found: list[str] = Field(default_factory=list)
    part_numbers_found: list[str] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    commitments: list[CommitmentResult] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    usage: AgentUsage | None = Field(None, description="The agent run; null if it failed.")

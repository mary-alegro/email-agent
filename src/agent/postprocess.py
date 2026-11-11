"""Turn raw LLM extractions into validated, normalized commitments ready for the ERP.

Everything here is deterministic code, so these rules hold regardless of what the model says:
- every commitment must be backed by a quote that really appears in the email;
- units and dates are computed here, never by the model;
- a commitment is written only if it maps to exactly one existing PO line;
- in a thread, a part's main commitment comes only from the newest message stating one;
- the review status comes from explicit rules, not from model confidence alone;
- a high-severity fraud flag blocks every write from the email; a medium one forces review.
"""

import re
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from src.agent.schemas import CommitmentResult, ERPContext, ExtractedCommitment, Flag
from src.config import settings
from src.database.models import ReviewStatus
from src.erp.schemas import LeadTimeDetails, LeadTimeIn, POLineOut
from src.utils.dates import add_business_days, add_calendar_days, parse_iso_date
from src.utils.text import normalize_for_match


@dataclass
class PlannedCommitment:
    """A commitment after validation, plus what's needed to write it."""

    result: CommitmentResult
    line: POLineOut | None = None
    to_write: LeadTimeIn | None = None  # set only if it resolved to a line and has no blocker


def plan_commitments(
    extracted: list[ExtractedCommitment],
    email_text: str,
    context: ERPContext,
    reference_date: date,
    flags: list[Flag],
) -> list[PlannedCommitment]:
    planned = [
        _plan_one(c, email_text, context, reference_date, flags)
        for c in _drop_superseded(extracted)
    ]
    return _dedupe(planned)


def _drop_superseded(extracted: list[ExtractedCommitment]) -> list[ExtractedCommitment]:
    """Keep a part's main commitments only from the newest segment that states any.

    Main commitments in older (quoted) segments for the same PO/part are restatements or
    have been superseded. The prompt asks the model to skip them; this enforces it, so a
    restatement worded differently ("50 units" vs "the full quantity") can't be written
    twice. Options are reported, never written, so they're left to `_dedupe`.
    """
    newest: dict[tuple[str | None, str | None], int] = {}
    for c in extracted:
        if not c.is_option:
            key = (c.po_number, c.part_number)
            newest[key] = min(newest.get(key, c.segment_index), c.segment_index)
    return [
        c
        for c in extracted
        if c.is_option or c.segment_index == newest[(c.po_number, c.part_number)]
    ]


def _plan_one(
    c: ExtractedCommitment,
    email_text: str,
    context: ERPContext,
    reference_date: date,
    flags: list[Flag],
) -> PlannedCommitment:
    # Fraud checks: a high-severity flag blocks the write; a medium one forces review.
    blockers = [f"Blocked by fraud check: {f.message}" for f in flags if f.severity == "high"]
    issues = [f"Fraud check: {f.message}" for f in flags if f.severity == "medium"]

    # 1. Evidence must appear verbatim (modulo whitespace/dashes/case) in the email.
    haystack = normalize_for_match(email_text)
    verified = [q for q in c.evidence if normalize_for_match(q) in haystack]
    if not verified:
        blockers.append("No evidence quote could be found in the email")
    elif len(verified) < len(c.evidence):
        issues.append("Some evidence quotes could not be found in the email")

    # 2. Resolve the PO line. "Order is required": no line, no write.
    line, line_issue = _resolve_line(c, context)
    if line is None:
        blockers.append(line_issue)
    elif line_issue:  # resolved, but only by part number
        issues.append(line_issue)

    # 3. Normalize to days and dates (in code, not by the model).
    order_date = _order_date(context, line)
    details, detail_issues = _normalize(c, verified or c.evidence, order_date, reference_date)
    issues.extend(detail_issues)
    if details is None:
        blockers.append("Lead time could not be normalized")

    # 4. Review rules.
    if c.is_option:
        blockers.append("Alternative offer for the buyer to decide on; not the main commitment")
    issues.extend(f"Condition: {cond}" for cond in c.conditions)
    issues.extend(f"Assumption: {a}" for a in c.assumptions)
    confidence = min(max(c.confidence, 0.0), 1.0)
    if confidence < settings.review_confidence_threshold:
        issues.append(f"Low model confidence ({confidence:.2f})")

    all_issues = blockers + issues
    status = ReviewStatus.NEEDS_REVIEW if all_issues else ReviewStatus.ACCEPTED

    result = CommitmentResult(
        po_number=line.po_number if line else c.po_number,
        part_number=line.part_number if line else c.part_number,
        line_number=line.line_number if line else None,
        written=False,  # set by the pipeline after a successful ERP write
        review_status=status,
        is_option=c.is_option,
        confidence=confidence,
        details=details,
        issues=all_issues,
    )
    to_write = None
    if not blockers:  # no blocker implies a resolved line and valid details
        to_write = LeadTimeIn(review_status=status, confidence=confidence, details=details)
    return PlannedCommitment(result=result, line=line, to_write=to_write)


def _resolve_line(c: ExtractedCommitment, context: ERPContext) -> tuple[POLineOut | None, str]:
    """Return (line, issue). The issue is "" for a clean match; for a part-number-only
    match it's set alongside the line; with no line it says what's missing."""
    po = next((p for p in context.purchase_orders if p.po_number == c.po_number), None)

    if c.po_number and po is None:
        return None, f"PO {c.po_number} not found in ERP"

    if po is not None:
        lines = po.lines
        if c.part_number:
            lines = [ln for ln in lines if ln.part_number == c.part_number]
        if len(lines) == 1:
            return lines[0], ""
        if not lines:
            return None, f"Part {c.part_number} is not on PO {po.po_number}"
        return None, f"PO {po.po_number} has several matching lines; can't pick one"

    # No PO on the commitment. If the email does name a PO, don't hop to another PO by part
    # number (found by the eval: an Acme email naming Acme's PO but a NorthBay part number
    # got written to NorthBay's PO once the model dropped the PO number).
    named = [po.po_number for po in context.purchase_orders] + context.unknown_po_numbers
    if named:
        return None, (
            f"The email names PO {', '.join(named)}, but this commitment has no PO; "
            f"not matched to another PO by part number"
        )

    # The email names no PO at all: fall back to lines found by part number only.
    if c.part_number:
        lines = [ln for ln in context.part_only_lines if ln.part_number == c.part_number]
        if len(lines) == 1:
            return lines[0], "PO not stated in email; matched by part number only"
        if len(lines) > 1:
            return None, f"Part {c.part_number} is on several POs; can't pick one"
        return None, f"No PO line in ERP for part {c.part_number} (no PO number in email)"
    return None, "Email names neither a PO nor a part number for this commitment"


def _order_date(context: ERPContext, line: POLineOut | None) -> date | None:
    if line is None:
        return None
    po = next((p for p in context.purchase_orders if p.po_number == line.po_number), None)
    return po.order_date if po else None


_UNIT_DAYS = {"days": 1, "business_days": 1, "weeks": 7, "months": 30}


def _normalize(
    c: ExtractedCommitment, evidence: list[str], order_date: date | None, reference_date: date
) -> tuple[LeadTimeDetails | None, list[str]]:
    issues: list[str] = []
    min_days = max_days = None
    day_basis = None
    promised_min = promised_max = None

    if c.kind == "duration":
        if c.duration_min is None or c.duration_unit is None:
            return None, ["Duration without a value or unit"]
        lo = c.duration_min
        hi = c.duration_max if c.duration_max is not None else lo
        factor = _UNIT_DAYS[c.duration_unit]
        min_days, max_days = round(lo * factor), round(hi * factor)
        day_basis = "business" if c.duration_unit == "business_days" else "calendar"
        if c.duration_unit == "months":
            issues.append("Months converted at 30 days each (approximate)")

        start = {"ARO": order_date, "email_date": reference_date}.get(c.anchor or "")
        if start is not None:
            add = add_business_days if day_basis == "business" else add_calendar_days
            promised_min, promised_max = add(start, min_days), add(start, max_days)
        elif c.anchor == "ARO":
            issues.append("Order date unknown; promised date not computed")
        else:
            issues.append(f"Lead time counts from {c.anchor or 'an unstated event'}; no date yet")

    elif c.kind in ("absolute_date", "in_stock"):
        parsed = parse_iso_date(c.date)
        if c.kind == "absolute_date" and parsed is None:
            return None, ["Dated commitment without a valid date"]
        if c.kind == "in_stock":
            min_days = max_days = 0
        promised_min = promised_max = parsed
        if parsed and not _year_stated(evidence):
            issues.append(f"Year not stated in the email; assumed {parsed.year}")

    try:
        details = LeadTimeDetails(
            kind=c.kind,
            quantity=c.quantity if c.quantity and c.quantity > 0 else None,
            min_days=min_days,
            max_days=max_days,
            day_basis=day_basis,
            anchor=c.anchor if c.kind == "duration" else None,
            promised_date_min=promised_min,
            promised_date_max=promised_max,
            conditions=c.conditions,
            # The model's inferences plus the notes from normalizing above (assumed year,
            # months approximated, no date computed), so the ERP record says what to check.
            assumptions=c.assumptions + issues,
            raw_text=evidence[0] if evidence else "",
            evidence=evidence,
        )
    except ValidationError as exc:
        return None, [f"Invalid lead time: {exc.errors()[0]['msg']}"]
    return details, issues


def _dedupe(planned: list[PlannedCommitment]) -> list[PlannedCommitment]:
    """Drop exact repeats (e.g. the same commitment restated in quoted text)."""
    seen: set[tuple] = set()
    unique = []
    for p in planned:
        d = p.result.details
        value = (d.kind, d.quantity, d.min_days, d.max_days, d.promised_date_max) if d else None
        key = (p.result.po_number, p.result.part_number, p.result.is_option, value)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _year_stated(evidence: list[str]) -> bool:
    return any(re.search(r"\b(19|20)\d{2}\b", q) for q in evidence)

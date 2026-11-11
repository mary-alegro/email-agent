"""The email pipeline: the agent reads and proposes, code verifies and writes.

parse thread -> find PO/part refs -> ERP context (MCP) -> agent run (reads the ERP via
read-only MCP tools; hallucinations sent back) -> add the agent's references (code)
-> fraud checks -> validate/normalize/resolve (code) -> write back (MCP) -> store
"""

import logging
import time
from datetime import UTC, date, datetime

from fastmcp.exceptions import ToolError
from sqlmodel import Session

from src.agent.agent import ExtractionError, extract
from src.agent.checks import run_checks
from src.agent.context import add_agent_references, context_issues, gather_context
from src.agent.mcp_client import ERPClient, ERPUnavailableError
from src.agent.postprocess import PlannedCommitment, plan_commitments
from src.agent.references import find_references
from src.agent.schemas import EmailResult, EmailStatus, ERPContext, Flag
from src.agent.thread import ParsedEmail, parse_email
from src.database import crud
from src.database.models import Email, ReviewStatus
from src.utils.dates import parse_header_date
from src.utils.logging import message_id_var

log = logging.getLogger(__name__)


async def process_email(raw_email: str, session: Session) -> EmailResult:
    started = time.perf_counter()
    email = parse_email(raw_email)
    message_id_var.set(email.message_id)  # tags every log line below, incl. ERP/MCP calls
    log.info(
        "Email received",
        extra={"sender": email.sender, "subject": email.subject, "segments": len(email.segments)},
    )

    # Idempotency: an email we already processed returns its stored result.
    existing = crud.get_email(session, email.message_id)
    if existing is not None and existing.status != EmailStatus.ERROR:
        log.info("Already processed; returning stored result", extra={"status": existing.status})
        return EmailResult.model_validate(existing.result)

    refs = find_references(email.normalized_text)
    log.info(
        "References found",
        extra={"po_numbers": refs.po_numbers, "part_numbers": refs.part_numbers},
    )
    result = EmailResult(
        message_id=email.message_id,
        status=EmailStatus.ERROR,
        reference_date=_reference_date(email),
        po_numbers_found=refs.po_numbers,
        part_numbers_found=refs.part_numbers,
    )

    try:
        async with ERPClient() as erp:
            context = await gather_context(erp, refs)
            log.info("ERP context loaded", extra=_context_summary(context))

            try:
                extraction, result.usage = await extract(email, context, result.reference_date)
            except ExtractionError as exc:
                log.error("Extraction failed: %s", exc)
                result.issues.append(f"Extraction failed: {exc}")
                _save(session, email, result)
                return result
            log.info("Extraction done", extra={"commitments": len(extraction.commitments)})

            # The code's own lookups of POs/parts the agent used that the regexes missed
            # (only if they're really in the email).
            await add_agent_references(erp, extraction, email.normalized_text, context)
            log.info("ERP context after extraction", extra=_context_summary(context))
            result.issues.extend(context_issues(context))

            # Fraud checks run on the final context, so a PO the agent found is checked too.
            result.flags = run_checks(email, context)
            for flag in result.flags:
                log.warning(
                    "Fraud check flagged email: %s",
                    flag.message,
                    extra={"code": flag.code, "severity": flag.severity, "evidence": flag.evidence},
                )

            planned = plan_commitments(
                extraction.commitments,
                email.normalized_text,
                context,
                result.reference_date,
                result.flags,
            )
            await _write_back(erp, email.message_id, planned)
    except ERPUnavailableError as exc:  # like an LLM outage: status error, safe to retry
        log.error("ERP unavailable: %s", exc)
        result.issues.append(str(exc))
        _save(session, email, result)
        return result

    result.commitments = [p.result for p in planned]
    result.status = _email_status(planned, result.flags)
    _save(session, email, result)
    _log_result(result, started)
    return result


def _context_summary(context: ERPContext) -> dict:
    return {
        "pos_found": [po.po_number for po in context.purchase_orders],
        "pos_unknown": context.unknown_po_numbers,
        "part_only_lines": len(context.part_only_lines),
    }


async def _write_back(erp: ERPClient, message_id: str, planned: list[PlannedCommitment]) -> None:
    """One write per PO line, carrying all of this email's commitments for that line."""
    by_line: dict[tuple[str, int], list[PlannedCommitment]] = {}
    for p in planned:
        if p.to_write is not None:  # only set when the commitment resolved to a PO line
            by_line.setdefault((p.line.po_number, p.line.line_number), []).append(p)

    for (po_number, line_number), group in by_line.items():
        try:
            await erp.write_lead_times(
                po_number, line_number, message_id, [p.to_write for p in group]
            )
        except Exception as exc:  # ERP rejected the write (ToolError) or is unreachable
            log.error(
                "ERP write failed for PO %s line %s: %s",
                po_number,
                line_number,
                exc,
                exc_info=not isinstance(exc, ToolError),  # traceback only for unexpected errors
            )
            for p in group:
                p.result.issues.append(f"ERP write failed: {exc}")
                p.result.review_status = ReviewStatus.NEEDS_REVIEW
            continue
        for p in group:
            p.result.written = True


def _email_status(planned: list[PlannedCommitment], flags: list[Flag]) -> EmailStatus:
    if not planned:
        # Zero extractions isn't a failure, but a fraud flag (e.g. new bank details in an
        # email with no lead time) still needs a human, so it must not read as "no signal".
        return EmailStatus.NEEDS_REVIEW if flags else EmailStatus.NO_SIGNAL
    main = [p.result for p in planned if not p.result.is_option]
    if main and all(r.written and r.review_status == ReviewStatus.ACCEPTED for r in main):
        return EmailStatus.PROCESSED
    return EmailStatus.NEEDS_REVIEW


def _reference_date(email: ParsedEmail) -> date:
    """Date relative dates are resolved against: the newest 'Sent' header, else today."""
    for segment in email.segments:
        parsed = parse_header_date(segment.sent)
        if parsed:
            return parsed
    return datetime.now(UTC).date()


def _log_result(result: EmailResult, started: float) -> None:
    for c in result.commitments:
        log.info(
            "Commitment %s/%s: %s, %s",
            c.po_number,
            c.part_number,
            "written" if c.written else "not written",
            c.review_status,
            extra={"line": c.line_number, "is_option": c.is_option, "issues": c.issues},
        )
    log.info(
        "Email processed",
        extra={
            "status": result.status,
            "commitments": len(result.commitments),
            "written": sum(c.written for c in result.commitments),
            "duration_ms": round((time.perf_counter() - started) * 1000),
        },
    )


def _save(session: Session, email: ParsedEmail, result: EmailResult) -> None:
    crud.save_email(
        session,
        Email(
            message_id=email.message_id,
            sender=email.sender,
            subject=email.subject,
            raw_body=email.normalized_text,
            status=result.status,
            result=result.model_dump(mode="json"),
        ),
    )

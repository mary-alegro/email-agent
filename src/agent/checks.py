"""Email-level fraud and spoofing checks.

Deterministic, explainable rules. The pipeline runs them after extraction, on the final
ERP context, so a PO only the agent found is checked too. Each returns Flags with a code,
a severity, a message and the evidence that triggered it:

- medium: suspicious but often legitimate (a supplier writing from another domain).
  Commitments from the email are still written, but as needs_review.
- high: a classic fraud pattern (lookalike domain, Reply-To redirect, new bank details).
  Nothing from the email is written to the ERP; the result says why.

The checks only need the parsed email and the ERP context gathered via MCP (the
referenced POs carry the supplier's name and email domain).
"""

import re
from collections.abc import Callable

from src.agent.schemas import ERPContext, Flag
from src.agent.thread import ParsedEmail
from src.utils.domains import looks_like, parse_sender, related_domains, same_organization

Check = Callable[[ParsedEmail, ERPContext], list[Flag]]

FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
}  # fmt: skip

# Deliberately specific: "new account manager" or "updated payment terms" must not match.
_PAYMENT_CHANGE = re.compile(
    r"\b(new|updated|changed?|different)\b[^.\n]{0,40}\b(bank|banking|wire|remit\w*)\b"
    r"|\b(bank|banking|payment|remittance|wire)\s+(details|instructions|information|info)\b"
    r"|\b(bank )?account (number|no\.?|details)\b"
    r"|\b(iban|swift|bic|routing number|sort code)\b"
    r"|\bremit[- ]to\b",
    re.IGNORECASE,
)
_SHIPPING_CHANGE = re.compile(
    r"\b(new|updated|changed?|different)\b[^.\n]{0,30}\b(ship[- ]to|shipping|delivery)\s+address"
    r"|\b(ship|deliver)\w*\s+to\s+(a|our)\s+(new|different)\s+(address|location|warehouse)",
    re.IGNORECASE,
)
_PROMPT_INJECTION = re.compile(
    r"\b(ignore|disregard)\b[^.\n]{0,30}\b(previous|prior|above|all)\b[^.\n]{0,20}"
    r"\b(instructions|rules|prompt)"
    r"|\bsystem prompt\b|\byou are (now )?an? (ai|assistant|language model)\b",
    re.IGNORECASE,
)
_AUTH_FAIL = re.compile(r"\b(spf|dkim|dmarc)=(fail|softfail)\b", re.IGNORECASE)


def check_sender_domain(email: ParsedEmail, context: ERPContext) -> list[Flag]:
    """The sender should write from the domain of the supplier on the referenced PO(s)."""
    suppliers = {po.supplier_domain.lower(): po.supplier_name for po in context.purchase_orders}
    name, domain = parse_sender(email.sender)
    if not suppliers or not domain or any(same_organization(domain, s) for s in suppliers):
        return []

    expected = ", ".join(sorted(suppliers))
    evidence = f"From: {email.sender}"
    for supplier_domain in suppliers:
        if looks_like(domain, supplier_domain):
            return [
                Flag(
                    code="lookalike_domain",
                    severity="high",
                    evidence=evidence,
                    message=f"Sender domain {domain} imitates supplier domain {supplier_domain}",
                )
            ]
    if any(name and supplier_name.lower() in name.lower() for supplier_name in suppliers.values()):
        return [
            Flag(
                code="display_name_impersonation",
                severity="high",
                evidence=evidence,
                message=f"Display name claims to be the supplier, but the address is at "
                f"{domain}, not {expected}",
            )
        ]
    if domain in FREE_MAIL_DOMAINS:
        return [
            Flag(
                code="free_mail_sender",
                severity="medium",
                evidence=evidence,
                message=f"Sent from a free-mail address ({domain}), not {expected}",
            )
        ]
    return [
        Flag(
            code="sender_domain_mismatch",
            severity="medium",
            evidence=evidence,
            message=f"Sender domain {domain} doesn't match the PO supplier ({expected})",
        )
    ]


def check_reply_to(email: ParsedEmail, context: ERPContext) -> list[Flag]:
    """Replies silently redirected to another domain: a classic business-email-compromise trick."""
    headers = email.segments[0].headers
    _, from_domain = parse_sender(email.sender)
    flags = []
    for header in ("reply-to", "return-path"):
        _, other = parse_sender(headers.get(header))
        if from_domain and other and not related_domains(other, from_domain):
            flags.append(
                Flag(
                    code=f"{header.replace('-', '_')}_mismatch",
                    severity="high",
                    evidence=f"{header.title()}: {headers[header]}",
                    message=f"{header.title()} points to {other}, but the email "
                    f"is from {from_domain}",
                )
            )
    return flags


def check_payment_change(email: ParsedEmail, context: ERPContext) -> list[Flag]:
    """New bank/payment details in the newest message: the costliest fraud pattern."""
    return _text_flag(
        _PAYMENT_CHANGE,
        _newest_text(email),
        code="payment_instruction_change",
        severity="high",
        message="Email mentions new or changed payment/bank details",
    )


def check_shipping_change(email: ParsedEmail, context: ERPContext) -> list[Flag]:
    """A changed delivery address can be legitimate, but should be confirmed."""
    return _text_flag(
        _SHIPPING_CHANGE,
        _newest_text(email),
        code="shipping_address_change",
        severity="medium",
        message="Email asks to ship or deliver to a new address",
    )


def check_thread_sender_changed(email: ParsedEmail, context: ERPContext) -> list[Flag]:
    """Earlier supplier messages came from one domain; the newest comes from another
    (a hijacked or spoofed thread)."""
    _, newest = parse_sender(email.sender)
    _, buyer = parse_sender(email.segments[0].headers.get("to"))
    earlier = {parse_sender(s.headers.get("from"))[1] for s in email.segments[1:]}
    supplier_side = {d for d in earlier if d and not (buyer and same_organization(d, buyer))}
    if not newest or not supplier_side:
        return []
    if any(related_domains(newest, d) for d in supplier_side):
        return []
    return [
        Flag(
            code="thread_sender_changed",
            severity="high",
            evidence=f"From: {email.sender}",
            message=f"Earlier messages in this thread came from "
            f"{', '.join(sorted(supplier_side))}; the newest is from {newest}",
        )
    ]


def check_authentication(email: ParsedEmail, context: ERPContext) -> list[Flag]:
    """SPF/DKIM/DMARC failures, when the mail server's Authentication-Results header is present."""
    header = email.segments[0].headers.get("authentication-results", "")
    if not _AUTH_FAIL.search(header):
        return []
    return [
        Flag(
            code="authentication_failed",
            severity="high",
            evidence=f"Authentication-Results: {header}",
            message="The mail server reports a failed SPF/DKIM/DMARC check",
        )
    ]


def check_prompt_injection(email: ParsedEmail, context: ERPContext) -> list[Flag]:
    """Text aimed at the AI rather than the buyer. The model is told to ignore it and can't
    write anything itself; this makes the attempt visible to a human."""
    return _text_flag(
        _PROMPT_INJECTION,
        email.normalized_text,
        code="prompt_injection",
        severity="medium",
        message="Email contains instructions aimed at an AI system",
    )


CHECKS: list[Check] = [
    check_sender_domain,
    check_reply_to,
    check_payment_change,
    check_shipping_change,
    check_thread_sender_changed,
    check_authentication,
    check_prompt_injection,
]


def run_checks(email: ParsedEmail, context: ERPContext) -> list[Flag]:
    return [flag for check in CHECKS for flag in check(email, context)]


def _newest_text(email: ParsedEmail) -> str:
    """Subject + newest message only: older quoted messages were already handled."""
    return f"{email.subject or ''}\n{email.segments[0].body}"


def _text_flag(pattern: re.Pattern, text: str, **flag) -> list[Flag]:
    """One flag if a line of `text` matches; that line (truncated) is the evidence."""
    for line in text.splitlines():
        if pattern.search(line):
            return [Flag(evidence=line.strip()[:200], **flag)]
    return []

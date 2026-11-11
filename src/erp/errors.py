"""Domain errors raised by the ERP service. REST maps them to HTTP codes, MCP to tool errors."""


class ERPError(Exception):
    """Base class for ERP business-rule violations."""


class NotFoundError(ERPError):
    """The requested PO or PO line does not exist."""


class InvalidLeadTimeError(ERPError):
    """The lead-time write breaks a business rule (e.g. quantity exceeds the line)."""

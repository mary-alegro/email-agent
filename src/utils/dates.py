"""Date helpers shared across modules."""

from datetime import date, datetime, timedelta

# Formats seen in email "Sent:"/"Date:" headers (Outlook, RFC 2822, ISO).
_HEADER_DATE_FORMATS = ("%a, %b %d, %Y %I:%M %p", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d")


def add_calendar_days(start: date, days: int) -> date:
    return start + timedelta(days=days)


def add_business_days(start: date, days: int) -> date:
    """Add working days, skipping weekends. Holidays are not modeled."""
    current, added = start, 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def parse_iso_date(value: str | None) -> date | None:
    """'2026-06-15' -> date; None for missing or malformed values."""
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def parse_header_date(value: str | None) -> date | None:
    """Parse an email date header such as 'Tue, May 19, 2026 9:14 AM'; None if unrecognized."""
    if not value:
        return None
    for fmt in _HEADER_DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None

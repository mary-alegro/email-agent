"""Text helpers shared across modules."""

import re

_TYPOGRAPHIC = str.maketrans({"–": "-", "—": "-", "’": "'", "“": '"', "”": '"'})


def normalize_for_match(text: str) -> str:
    """Lowercase, plain dashes/quotes, collapsed spaces: for 'is this quote in the email'."""
    return re.sub(r"\s+", " ", text.lower().translate(_TYPOGRAPHIC)).strip()


def unique(items: list[str]) -> list[str]:
    """De-duplicate, keeping first-seen order."""
    return list(dict.fromkeys(items))


def mentioned(value: str, text: str) -> bool:
    """Whole-token match: '4500123456' must appear as itself, not inside a longer number."""
    return re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", text, re.IGNORECASE) is not None

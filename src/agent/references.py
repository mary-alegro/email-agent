"""Find candidate PO and part numbers in an email.

High recall on purpose: the ERP lookup is what decides which candidates are real.
"""

import re
from dataclasses import dataclass

from src.utils.text import unique

# "PO 4500123456", "PO# 4500123456", "P.O. No. 4500123456". "PO TBD" doesn't match.
_PO = re.compile(r"\bP\.?O\.?\s*(?:#|No\.?|Number)?\s*:?\s*(\d{6,12})\b", re.IGNORECASE)
# Part-number shaped tokens such as "JT-8831", "NB-4402", "AP-1187".
_PART = re.compile(r"\b([A-Z]{2,4}-\d{3,6}[A-Z]?)\b")


@dataclass
class References:
    po_numbers: list[str]
    part_numbers: list[str]


def find_references(text: str) -> References:
    return References(
        po_numbers=unique(_PO.findall(text)),
        part_numbers=unique(_PART.findall(text)),
    )

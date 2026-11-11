"""Parse a raw email into headers + thread segments (newest first).

Deterministic and deliberately simple: it recognizes Outlook-style
"-----Original Message-----" separators, Gmail-style "On ... wrote:" lines, and
">"-quoted text. Anything else stays in one segment; the LLM still sees all text.
"""

import hashlib
import re
from dataclasses import dataclass

_SEPARATOR = re.compile(
    r"^\s*(-{2,}\s*Original Message\s*-{2,}|On .{5,200}wrote:)\s*$", re.IGNORECASE | re.MULTILINE
)
_HEADER = re.compile(
    r"^(From|To|Cc|Sent|Date|Subject|Reply-To|Return-Path|Authentication-Results):\s*(.*)$",
    re.IGNORECASE,
)


@dataclass
class Segment:
    index: int  # 0 = newest message, 1 = the one it quotes, ...
    headers: dict[str, str]
    body: str

    @property
    def sent(self) -> str:
        """The segment's 'Sent:' (Outlook) or 'Date:' header, '' if neither."""
        return self.headers.get("sent") or self.headers.get("date") or ""


@dataclass
class ParsedEmail:
    message_id: str
    normalized_text: str
    sender: str | None
    subject: str | None
    segments: list[Segment]


def normalize(raw: str) -> str:
    """Canonical form used for the message_id and for evidence checks."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def message_id_for(normalized_text: str) -> str:
    """Stable id: the same email body always gets the same id."""
    return "msg_" + hashlib.sha256(normalized_text.encode()).hexdigest()[:24]


def parse_email(raw: str) -> ParsedEmail:
    text = normalize(raw)
    chunks = _SEPARATOR.split(text)
    # re.split with one capture group returns [body0, sep1, body1, sep2, body2, ...]
    bodies = [chunks[0]] + chunks[2::2]

    segments = []
    for i, chunk in enumerate(bodies):
        headers, body = _split_headers(_unquote(chunk))
        segments.append(Segment(index=i, headers=headers, body=body.strip()))

    top = segments[0].headers  # re.split always returns at least one chunk
    return ParsedEmail(
        message_id=message_id_for(text),
        normalized_text=text,
        sender=top.get("from"),
        subject=top.get("subject"),
        segments=segments,
    )


def _unquote(chunk: str) -> str:
    """Strip leading '>' quote markers."""
    return "\n".join(re.sub(r"^\s*>+ ?", "", line) for line in chunk.split("\n"))


def _split_headers(chunk: str) -> tuple[dict[str, str], str]:
    """Read leading 'Key: value' header lines; the rest is the body."""
    headers: dict[str, str] = {}
    lines = chunk.strip("\n").split("\n")
    i = 0
    while i < len(lines):
        match = _HEADER.match(lines[i].strip())
        if not match:
            break
        headers[match.group(1).lower()] = match.group(2).strip()
        i += 1
    return headers, "\n".join(lines[i:])

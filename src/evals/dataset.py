"""Eval cases: one JSON file per case in src/evals/cases/.

A case is an email plus the expected outcome. Every field of an expected commitment
except the identifying ones is optional: a field left out isn't checked. That keeps cases
honest where several answers are acceptable (e.g. whether a relative date adds an
assumption) and strict where it matters (written or not, the day range, the flags).
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src.database.models import EmailStatus, ReviewStatus

CASES_DIR = Path(__file__).parent / "cases"

# Fields that identify a commitment for matching; every other field is checked if given.
IDENTITY_FIELDS = ("po_number", "part_number", "is_option")


class ExpectedCommitment(BaseModel):
    po_number: str | None
    part_number: str | None
    is_option: bool = False
    optional: bool = Field(
        False, description="Matched if present; not counted as missed if absent."
    )

    written: bool | None = None
    review_status: ReviewStatus | None = None
    line_number: int | None = None
    kind: Literal["duration", "absolute_date", "in_stock"] | None = None
    quantity: int | None = None
    min_days: int | None = None
    max_days: int | None = None
    day_basis: Literal["calendar", "business"] | None = None
    anchor: Literal["ARO", "approval", "email_date", "unspecified"] | None = None

    def checked_fields(self) -> list[str]:
        """Fields this case asserts: the ones set in the JSON, minus identity/meta fields."""
        skip = {*IDENTITY_FIELDS, "optional"}
        return [f for f in self.model_fields_set if f not in skip]


class Expected(BaseModel):
    status: EmailStatus | None = Field(None, description="Not checked when left out.")
    flags: list[str] = Field(default_factory=list, description="Expected flag codes.")
    commitments: list[ExpectedCommitment] = Field(default_factory=list)


class EvalCase(BaseModel):
    id: str
    description: str
    tags: list[str] = Field(default_factory=list)
    email: str
    expected: Expected


def load_cases(filter_text: str | None = None) -> list[EvalCase]:
    """All cases, sorted by file name; `filter_text` keeps ids or tags containing it."""
    cases = [
        EvalCase.model_validate_json(path.read_text()) for path in sorted(CASES_DIR.glob("*.json"))
    ]
    if filter_text:
        cases = [c for c in cases if filter_text in c.id or filter_text in c.tags]
    return cases

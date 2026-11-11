from datetime import date

from src.agent.references import find_references
from src.agent.thread import parse_email
from src.utils.dates import add_business_days, parse_header_date, parse_iso_date
from src.utils.text import normalize_for_match


def test_thread_split_and_stable_id(email_texts):
    thread = parse_email(email_texts["04"])
    assert [s.index for s in thread.segments] == [0, 1, 2]
    assert thread.segments[2].headers["sent"] == "Mon, May 18, 2026 4:02 PM"
    assert thread.sender.endswith("<sales@acme-components.com>")
    # Same email -> same id, even with different line endings / trailing spaces.
    noisy = email_texts["04"].replace("\n", "  \r\n")
    assert parse_email(noisy).message_id == thread.message_id


def test_references(email_texts):
    refs = {k: find_references(parse_email(v).normalized_text) for k, v in email_texts.items()}
    assert (refs["01"].po_numbers, refs["01"].part_numbers) == (["4500123456"], ["JT-8831"])
    assert (refs["02"].po_numbers, refs["02"].part_numbers) == (["4500999911"], ["NB-4402"])
    assert (refs["03"].po_numbers, refs["03"].part_numbers) == ([], ["AP-1187"])  # "PO TBD"


def test_date_helpers():
    assert add_business_days(date(2026, 5, 22), 1) == date(2026, 5, 25)  # Fri -> Mon
    assert add_business_days(date(2026, 5, 20), 5) == date(2026, 5, 27)
    assert parse_header_date("Tue, May 19, 2026 9:14 AM") == date(2026, 5, 19)
    assert parse_header_date("next week-ish") is None
    assert parse_iso_date("2026-06-15") == date(2026, 6, 15) and parse_iso_date("June") is None


def test_normalize_for_match():
    assert normalize_for_match("6–8  weeks\nARO") == normalize_for_match("6-8 weeks aro")

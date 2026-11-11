import pytest

from src.erp.errors import InvalidLeadTimeError, NotFoundError
from src.erp.schemas import LeadTimeDetails, LeadTimeIn, ReviewStatus
from src.erp.service import ERPService


def commitment(qty, lo, hi, status=ReviewStatus.ACCEPTED):
    text = f"{qty} pcs in {lo}-{hi} days"
    return LeadTimeIn(
        review_status=status,
        confidence=0.9,
        details=LeadTimeDetails(
            kind="duration",
            quantity=qty,
            min_days=lo,
            max_days=hi,
            day_basis="calendar",
            anchor="ARO",
            raw_text=text,
            evidence=[text],
        ),
    )


@pytest.fixture
def erp(session):
    return ERPService(session)


def test_reads(erp):
    po = erp.get_purchase_order("4500123456")
    assert po.supplier_domain == "acme-components.com" and len(po.lines) == 2
    assert [ln.line_number for ln in erp.find_lines(part_number="JT-8831")] == [10]
    record = erp.get_lead_time("4500123456", "JT-8831")
    assert len(record) == 1 and record[0].lead_times == []


@pytest.mark.parametrize(
    "call",
    [
        lambda erp: erp.get_purchase_order("nope"),
        lambda erp: erp.get_lead_time("4500123456", "NB-4402"),
        lambda erp: erp.write_lead_times("4500123456", 99, "m", [commitment(None, 1, 2)]),
    ],
)
def test_not_found(erp, call):
    with pytest.raises(NotFoundError):
        call(erp)


@pytest.mark.parametrize(
    "message_id, commitments",
    [
        ("m", []),  # nothing to write
        ("", [commitment(None, 1, 2)]),  # no source message
        ("m", [commitment(60, 5, 5), commitment(60, 25, 30)]),  # 120 > line quantity 100
    ],
)
def test_write_rules(erp, message_id, commitments):
    with pytest.raises(InvalidLeadTimeError):
        erp.write_lead_times("4500999911", 10, message_id, commitments)


def test_write_is_idempotent_and_isolated_to_one_line(erp):
    split = [commitment(20, 7, 7), commitment(80, 35, 42)]
    erp.write_lead_times("4500999911", 10, "m2", split)
    record = erp.write_lead_times("4500999911", 10, "m2", split)
    assert [lt.details.quantity for lt in record.lead_times] == [20, 80]

    erp.write_lead_times("4500123456", 10, "m3", [commitment(None, 42, 56)])
    assert erp.get_lead_time("4500123456", "JT-9002")[0].lead_times == []


def test_get_lead_time_part_is_optional(erp):
    every = erp.get_lead_time("4500123456")
    assert [r.line.part_number for r in every] == ["JT-8831", "JT-9002"]
    with pytest.raises(NotFoundError, match="PO nope not found"):
        erp.get_lead_time("nope")


def test_find_lines_needs_a_filter(erp):
    with pytest.raises(ValueError):
        erp.find_lines()

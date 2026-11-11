"""Seed the mock ERP with purchase orders matching the example emails.

Run with:  uv run python -m src.database.seed [--reset]

AP-1187 (example email 3, "RFQ / PO TBD") is deliberately NOT seeded, so that email
exercises the "no PO line -> no writeback" path.
"""

import argparse
from datetime import date

from sqlmodel import Session

from src.database import crud
from src.database.engine import engine, init_db, reset_db
from src.database.models import POLine, PurchaseOrder

PURCHASE_ORDERS = [
    PurchaseOrder(
        po_number="4500123456",
        supplier_name="Acme Components",
        supplier_domain="acme-components.com",
        order_date=date(2026, 5, 15),
    ),
    PurchaseOrder(
        po_number="4500999911",
        supplier_name="NorthBay Fasteners",
        supplier_domain="northbayfasteners.com",
        order_date=date(2026, 5, 20),
    ),
]

PO_LINES = [
    POLine(po_number="4500123456", line_number=10, part_number="JT-8831", quantity=50),
    # A second line on the same PO, so we can check that only the JT-8831 line gets updated.
    POLine(po_number="4500123456", line_number=20, part_number="JT-9002", quantity=25),
    POLine(po_number="4500999911", line_number=10, part_number="NB-4402", quantity=100),
]


def seed(session: Session) -> None:
    """Insert seed rows that don't exist yet (safe to run more than once)."""
    for po in PURCHASE_ORDERS:
        session.merge(po)
    session.flush()
    for line in PO_LINES:
        if crud.get_line(session, line.po_number, line.line_number) is None:
            session.add(POLine.model_validate(line))
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    args = parser.parse_args()

    if args.reset:
        reset_db(engine)
    else:
        init_db(engine)
    with Session(engine) as session:
        seed(session)
    print(f"Seeded {len(PURCHASE_ORDERS)} purchase orders and {len(PO_LINES)} PO lines.")


if __name__ == "__main__":
    main()

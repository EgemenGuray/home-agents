"""TTC reimbursement report (S1.10.1).

The agent downloads the statement (CSV) from the card site in a desktop workspace on the home
route, after you approve the login. This module does the rest: pick out TTC and PRESTO charges
for the period, total them in CAD, write the spreadsheet and draft the email. Sending the
draft always waits for your approval.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_RULES = [r"\bTTC\b", r"PRESTO", r"TORONTO TRANSIT", r"\bUP EXPRESS\b", r"\bGO TRANSIT\b"]
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y", "%Y/%m/%d")


@dataclass(frozen=True)
class Charge:
    day: date
    merchant: str
    amount_cad: Decimal


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {s!r}")


def _parse_amount(s: str) -> Decimal:
    cleaned = s.replace("$", "").replace(",", "").replace("CAD", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"unrecognised amount {s!r}") from exc
    return -value if negative else value


def _pick(header: list[str], *names: str) -> int:
    lower = [h.strip().lower() for h in header]
    for n in names:
        for i, h in enumerate(lower):
            if n in h:
                return i
    raise ValueError(f"statement has no column like {names}")


def parse_statement(csv_text: str) -> list[Charge]:
    rows = list(csv.reader(io.StringIO(csv_text)))
    header, body = rows[0], [r for r in rows[1:] if any(c.strip() for c in r)]
    i_date = _pick(header, "transaction date", "date", "posted")
    i_desc = _pick(header, "description", "merchant", "details", "payee")
    try:
        i_amt = _pick(header, "amount", "debit", "charge")
    except ValueError:
        i_amt = None
    charges = []
    for r in body:
        amount = _parse_amount(r[i_amt]) if i_amt is not None and r[i_amt].strip() else Decimal(0)
        # Banks differ on the sign of purchases; transit charges are never credits, so use the size.
        charges.append(Charge(_parse_date(r[i_date]), r[i_desc].strip(), abs(amount)))
    return charges


def ttc_charges(charges: list[Charge], start: date, end: date, rules: list[str] | None = None) -> list[Charge]:
    pats = [re.compile(r, re.I) for r in (rules or DEFAULT_RULES)]
    return sorted(
        (c for c in charges if start <= c.day <= end and any(p.search(c.merchant) for p in pats)),
        key=lambda c: c.day,
    )


def total(charges: list[Charge]) -> Decimal:
    return sum((c.amount_cad for c in charges), Decimal("0"))


def write_xlsx(charges: list[Charge], path: Path, title: str) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "TTC"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Date", "Merchant", "Amount (CAD)"])
    for cell in ws[3]:
        cell.font = Font(bold=True)
    for c in charges:
        ws.append([c.day.isoformat(), c.merchant, float(c.amount_cad)])
    ws.append([])
    ws.append(["", "Total", float(total(charges))])
    ws.cell(ws.max_row, 2).font = Font(bold=True)
    for row in ws.iter_rows(min_row=4, min_col=3, max_col=3):
        for cell in row:
            cell.number_format = '"CA$"#,##0.00'
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 40
    ws.column_dimensions["C"].width = 14
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def draft_email(charges: list[Charge], start: date, end: date, recipient: str, sender_name: str) -> dict:
    period = f"{start:%B %-d} to {end:%B %-d, %Y}"
    return {
        "to": recipient,
        "subject": f"TTC reimbursement: {start:%B %Y}" if start.month == end.month else f"TTC reimbursement: {period}",
        "body": (
            f"Hi,\n\nPlease find attached my TTC transit expenses for {period}: "
            f"{len(charges)} charges totalling CA${total(charges):,.2f}. The statement pages are attached as evidence.\n\n"
            f"Thanks,\n{sender_name}\n"
        ),
        "needs_approval": True,
    }

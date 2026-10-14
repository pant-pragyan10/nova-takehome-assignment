"""Deterministic parsers for the regular, tabular parts of the documents (no model involved).

Tables are the one place a model can silently mistype a number, and their format is stable across contracts, so they are
parsed with small tolerant parsers. Each parser returns [] when it finds nothing; understand.py then falls back to the
model for that table.

Everything returns the exact source line too (`line`), because evidence quotes must be copied from the document.
"""
from __future__ import annotations

import re
from datetime import date

DATE = r"\d{4}-\d{2}-\d{2}"
NUM = r"-?\d[\d,]*(?:\.\d+)?"


def n(s: str) -> float:
    return float(s.replace(",", ""))


def lines_of(pages: list[str]):
    for pi, p in enumerate(pages, 1):
        for ln in p.split("\n"):
            yield pi, ln.strip()


# ---- contract annexes --------------------------------------------------------------------------------------------------
def parse_periods(pages) -> list[dict]:
    """'Period P1: pickups 2034-10-20 to 2035-04-19'"""
    out = []
    for pi, ln in lines_of(pages):
        m = re.match(rf"^Period\s+(\w+)\s*:\s*pickups\s+({DATE})\s+to\s+({DATE})", ln, re.I)
        if m:
            out.append({"name": m.group(1).upper(), "from": m.group(2), "to": m.group(3), "page": pi, "line": ln})
    return out


def parse_rates(pages, n_periods: int) -> list[dict]:
    """'USS2-INN1 0-44.5 kg 8.98 8.94' / 'USS2-INN1 minimum charge 91.00 91.00' / '300+ kg'.
    One value column per rate period. Also used for amendment tables ('Lane Weight band Rate Currency')."""
    out = []
    lane = r"([A-Z]{2,3}[A-Z0-9]{1,2}-[A-Z]{2,3}[A-Z0-9]{1,2})"
    for pi, ln in lines_of(pages):
        m = re.match(rf"^{lane}\s+minimum charge\s+({NUM})((?:\s+{NUM})*)\s*(USD|INR|EUR|GBP)?$", ln, re.I)
        if m:
            vals = [n(m.group(2))] + [n(x) for x in m.group(3).split()]
            out.append({"lane": m.group(1), "band": "min", "lo": None, "hi": None, "values": vals,
                        "currency": (m.group(4) or "").upper() or None, "page": pi, "line": ln})
            continue
        m = re.match(rf"^{lane}\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*kg\s+({NUM})((?:\s+{NUM})*)\s*(USD|INR|EUR|GBP)?$", ln, re.I)
        if m:
            vals = [n(m.group(4))] + [n(x) for x in m.group(5).split()]
            out.append({"lane": m.group(1), "band": f"{m.group(2)}-{m.group(3)}", "lo": float(m.group(2)),
                        "hi": float(m.group(3)), "values": vals, "currency": (m.group(6) or "").upper() or None,
                        "page": pi, "line": ln})
            continue
        m = re.match(rf"^{lane}\s+(\d+(?:\.\d+)?)\+\s*kg\s+({NUM})((?:\s+{NUM})*)\s*(USD|INR|EUR|GBP)?$", ln, re.I)
        if m:
            vals = [n(m.group(3))] + [n(x) for x in m.group(4).split()]
            out.append({"lane": m.group(1), "band": f"{m.group(2)}+", "lo": float(m.group(2)), "hi": None,
                        "values": vals, "currency": (m.group(5) or "").upper() or None, "page": pi, "line": ln})
    return out


def parse_fuel(pages) -> list[dict]:
    """Annex C: '2034-10 17.49%'"""
    out, in_c = [], False
    for pi, ln in lines_of(pages):
        if re.match(r"^Annex C", ln, re.I):
            in_c = True
        elif re.match(r"^Annex [D-Z]", ln, re.I):
            in_c = False
        m = re.match(r"^(\d{4}-\d{2})\s+(\d+(?:\.\d+)?)\s*%$", ln)
        if m and in_c:
            out.append({"month": m.group(1), "pct": n(m.group(2)), "page": pi, "line": ln})
    return out


def parse_accessorials(pages) -> list[dict]:
    """Annex D: 'DG Dangerous goods fee 124.02 USD per shipment' (fee in contract currency)."""
    out, in_d = [], False
    codes = ["DG", "DRYICE", "SEC", "STOR", "DLV", "CUSTOMS", "ACCT", "SPROG", "PPROG", "PEAK", "HDL", "COST", "MARGIN"]
    for pi, ln in lines_of(pages):
        if re.match(r"^Annex D", ln, re.I):
            in_d = True
        elif re.match(r"^Annex [E-Z]", ln, re.I):
            in_d = False
        if not in_d:
            continue
        m = re.match(rf"^([A-Z]{{2,10}})\s+(.+?)\s+({NUM})\s+(USD|INR|EUR|GBP)\s+(per .+)$", ln)
        if m:
            out.append({"code": m.group(1), "name": m.group(2), "fee": n(m.group(3)), "currency": m.group(4),
                        "basis": m.group(5), "page": pi, "line": ln})
    return out


def parse_charge_codes(pages) -> list[dict]:
    """Annex E: 'Code SEC: Security surcharge; also billed as: Screening fee, Screening fee (adj.)'"""
    out = []
    for pi, ln in lines_of(pages):
        m = re.match(r"^Code\s+([A-Z]+)\s*:\s*(.+?)(?:;\s*also billed as\s*:\s*(.+))?$", ln)
        if m:
            labels = [m.group(2).strip()] + ([x.strip() for x in re.split(r",\s*(?![^()]*\))", m.group(3))] if m.group(3) else [])
            out.append({"code": m.group(1), "label": m.group(2).strip(), "labels": labels, "page": pi, "line": ln})
    return out


def parse_fx(pages) -> list[dict]:
    """Annex F: 'INR per 1 USD' rows. Accepts '2035-01 83.12' or '2035-01 INR 83.12' or '2035-01: 83.12'."""
    out, in_f = [], False
    for pi, ln in lines_of(pages):
        if re.match(r"^Annex F", ln, re.I):
            in_f = True
        elif re.match(r"^Schedule 1", ln, re.I):
            in_f = False
        if not in_f:
            continue
        m = re.match(rf"^(\d{{4}}-\d{{2}})\s*[:\-]?\s*(?:INR\s*)?({NUM})$", ln)
        if m:
            out.append({"month": m.group(1), "inr_per_usd": n(m.group(2)), "page": pi, "line": ln})
    return out


def parse_schedule1(pages) -> list[dict]:
    """'Joaquin Rathbone, Key Account Manager, TBX-US: notices, billing_disputes <a@b.example>'"""
    out, on = [], False
    for pi, ln in lines_of(pages):
        if re.match(r"^Schedule 1", ln, re.I) and "Authorised Persons" in ln:
            on = True
            continue
        if not on:
            continue
        m = re.match(r"^(.+?),\s*(.+?),\s*([A-Z0-9][A-Z0-9-]*)\s*:\s*(.+?)\s*<([^>]+)>\s*$", ln)
        if m:
            out.append({"name": m.group(1).strip(), "title": m.group(2).strip(), "party": m.group(3),
                        "categories": [c.strip() for c in m.group(4).split(",") if c.strip()],
                        "email": m.group(5).strip().lower(), "page": pi, "line": ln})
    return out


# ---- clause splitting (for quotes and for the model) -------------------------------------------------------------------
def split_clauses(pages) -> dict[str, dict]:
    """{'10.2': {'page': 4, 'text': '10.2 Late fee. ...'}} with page headers/footers dropped."""
    out: dict[str, dict] = {}
    cur = None
    for pi, p in enumerate(pages, 1):
        for ln in p.split("\n"):
            s = ln.strip()
            if not s or s.startswith(("Initials", "Confidential")) or re.match(r"^Page \d+ of \d+", s):
                continue
            m = re.match(r"^(\d{1,2}\.\d{1,2})\s+[A-Z]", s)
            top = re.match(r"^(\d{1,2})\s+[A-Z][A-Za-z ,&-]+$", s)       # a bare section heading: '8 Handling margin'
            if m:
                cur = m.group(1)
                out[cur] = {"page": pi, "text": s}
            elif re.match(r"^(Annex [A-Z]\b(?!,)|Schedule \d|Execution|IN WITNESS)", s) and len(s) < 90 and not s.endswith("."):
                cur = None
            elif top and not s.endswith("."):
                cur = None
            elif cur:
                out[cur]["text"] += " " + s
    return out


# ---- spreadsheets --------------------------------------------------------------------------------------------------------
def parse_tracking(pages) -> list[dict]:
    """'TX23827044 | PICKED_UP | 2034-11-04 | USS2 | -'"""
    out = []
    for pi, ln in lines_of(pages):
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) >= 3 and re.match(r"^[A-Z0-9]{6,}$", cells[0]) and re.match(r"^[A-Z_]+$", cells[1]) \
                and re.match(DATE, cells[2]):
            out.append({"awb": cells[0], "event": cells[1], "date": cells[2], "station": cells[3] if len(cells) > 3 else "",
                        "reason": cells[4] if len(cells) > 4 else "", "page": pi, "line": ln})
    return out


def parse_register(pages) -> list[dict]:
    """'TX-US-2034-0379 | 2034-11-26 | due 2035-01-10 | USD 1243.96 | paid | 2035-01-03'
    Tolerant: finds the invoice number, dates, an amount with currency and a status word wherever they sit."""
    out = []
    for pi, ln in lines_of(pages):
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) < 4 or not re.search(r"\d", cells[0]) or cells[0].lower().startswith("invoice"):
            continue
        dates = [re.search(DATE, c).group(0) if re.search(DATE, c) else None for c in cells]
        due = next((re.search(DATE, c).group(0) for c in cells if re.match(r"^due\b", c, re.I) and re.search(DATE, c)), None)
        amt = next((re.match(rf"^([A-Z]{{3}})\s*({NUM})$", c) for c in cells[1:] if re.match(rf"^[A-Z]{{3}}\s*{NUM}$", c)), None)
        status = next((c.lower() for c in cells if c.lower() in ("paid", "open", "unpaid", "scheduled", "disputed", "partial", "partially paid", "credited")), None)
        plain_dates = [re.search(DATE, c).group(0) for c in cells[1:] if re.fullmatch(DATE, c)]
        inv_date = plain_dates[0] if plain_dates else None
        paid_date = plain_dates[-1] if (status in ("paid", "partial", "partially paid") and len(plain_dates) >= 2) else None
        if amt and status:
            out.append({"invoice_no": cells[0], "invoice_date": inv_date, "due_date": due, "currency": amt.group(1),
                        "amount": n(amt.group(2)), "status": status, "paid_date": paid_date, "page": pi, "line": ln})
    return out


def parse_statement(pages) -> dict | None:
    """Monthly statement spreadsheet that carries its own number. Charge | Reference | Quantity | Rate | Amount rows."""
    head = "\n".join(pages)
    mnum = re.search(r"STATEMENT OF ACCOUNT\s+(\S+)", head, re.I)
    mdate = re.search(rf"Statement date\s+({DATE})", head, re.I)
    mdue = re.search(rf"Due date\s+({DATE})", head, re.I)
    mc = re.search(r"Contract reference\s+(\S+)", head, re.I)
    if not (mnum and mdate):
        return None
    lines = []
    for pi, ln in lines_of(pages):
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) == 5 and re.match(NUM + "$", cells[4]) and not cells[0].lower().startswith("charge"):
            lines.append({"description": cells[0], "reference": cells[1], "qty_text": cells[2], "rate": n(cells[3]),
                          "amount": n(cells[4]), "page": pi, "line": ln})
    total = re.search(rf"Total\s+([A-Z]{{3}})\s*\|\s*({NUM})", head)
    return {"invoice_id_raw": mnum.group(1), "invoice_date": mdate.group(1), "due_date": mdue.group(1) if mdue else None,
            "contract_ref": mc.group(1) if mc else None, "lines": lines,
            "currency": total.group(1) if total else "USD", "total": n(total.group(2)) if total else None}

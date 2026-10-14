"""Layout-independent invoice / credit-note / statement reader (no model).

Instead of one regex per layout, each line is decoded arithmetically: find the numbers on the line, then pick the
assignment (rate, quantity, base, amount) whose product works out. That is what makes 'Net, Units, Unit price' and
'Rate, Amount, Collected' layouts parse with the same code. Header fields are found by label synonyms, dates in any of
ISO / 'dd Mon yyyy' / 'dd/mm/yyyy' are normalised, and text is matched with spaces removed so OCR output works.

`validate()` checks the parse against the document's own printed totals. Anything that fails is flagged so
understand.py can fall back to the model, and the model's answer must pass the same validator.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
NUMRE = r"\d[\d,]*(?:\.\d+)?"
AWB_RE = re.compile(r"\b([A-Z]{2}\d{8}(?:-H\d+)?)\b")
LANE_RE = re.compile(r"\b([A-Z]{2,3}[A-Z0-9]{1,2}-[A-Z]{2,3}[A-Z0-9]{1,2})\b")
UNITS = r"(kg|shpt|shipment|fee|fees|days?|months?|pcs|pieces|kgs)"


def norm_id(s: str) -> str:
    """Finding-key form of a document number: capitals, no spaces, no '#' or ':'."""
    return re.sub(r"[\s#:]", "", s).upper()


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_date(s: str, dayfirst: bool = True) -> str | None:
    s = s.strip().strip(".,;")
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s
    m = re.fullmatch(r"(\d{1,2})\s*([A-Za-z]{3})[a-z]*\.?,?\s*(\d{4})", s)
    if m and m.group(2).lower() in MONTHS:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d, mo = (a, b) if dayfirst else (b, a)
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


DATE_PAT = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s*[A-Za-z]{3}[a-z]*\.?,?\s*\d{4}|\d{1,2}[/.]\d{1,2}[/.]\d{4})"


def detect_dayfirst(text: str) -> bool:
    """dd/mm vs mm/dd: decided by any date whose first or second field exceeds 12; default day-first."""
    dd = mm = 0
    for a, b, _y in re.findall(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", text):
        if int(a) > 12:
            dd += 1
        if int(b) > 12:
            mm += 1
    return not (mm > 0 and dd == 0)


def _find_date(text: str, labels: list[str], dayfirst: bool) -> str | None:
    for lab in labels:
        m = re.search(rf"{lab}\s*[:.]?\s*({DATE_PAT})", text, re.I)
        if m:
            d = parse_date(m.group(1), dayfirst)
            if d:
                return d
    return None


# ---- one charge line ---------------------------------------------------------------------------------------------------
def _decode_numbers(vals: list[float], qty: float | None, unit: str | None, pct: float | None):
    """-> (rate, base, amount, consistent). Chooses the assignment whose arithmetic works (to the cent)."""
    def close(a, b, tol=0.011):
        return abs(a - b) <= tol

    if pct is not None:
        for i in range(len(vals)):
            for j in range(len(vals)):
                if i != j and close(round(vals[j] * pct / 100.0 + 1e-9, 2), vals[i], 0.011):
                    return None, vals[j], vals[i], True
        if len(vals) >= 2:
            return None, vals[0], vals[-1], False
        return None, None, vals[-1] if vals else None, False
    if qty is not None:
        for i in range(len(vals)):
            for j in range(len(vals)):
                if i != j and close(round(qty * vals[j] + 1e-9, 2), vals[i], 0.011):
                    return vals[j], None, vals[i], True
        if len(vals) == 1:
            return vals[0], None, vals[0], unit in ("shpt", "shipment", "month", "months")
        if len(vals) >= 2:
            return vals[-2], None, vals[-1], False
    if vals:
        return None, None, vals[-1], len(vals) == 1
    return None, None, None, False


def parse_line(ln: str, page: int, dayfirst: bool) -> dict | None:
    """Decode one printed line. Returns None for lines that are not charges."""
    raw = ln.strip()
    low = raw.lower().replace(" ", "")
    # late fee billed on an invoice
    m = re.search(r"late\s*fee\s*on\s*([A-Z0-9][A-Z0-9/\-]*)\s*:?\s*([A-Z]{3})?\s*(" + NUMRE + r")\s*overdue\s*,?\s*month\s*(\d+)\s*@\s*(" + NUMRE + r")\s*%", raw, re.I)
    if m:
        # The amount is not always last ('... 39.66 1 month 1.98%'): drop 'N month(s)' and percentages, then prefer the
        # number that equals the fee implied by the printed principal and rate.
        rest = raw[m.end():]
        rest = re.sub(r"\d+\s*months?\b", " ", rest, flags=re.I)
        rest = re.sub(NUMRE + r"\s*%", " ", rest)
        cands = [num(x) for x in re.findall(r"(?<![A-Za-z0-9])" + NUMRE + r"(?![A-Za-z0-9])", rest)]
        pct_v, base_v = num(m.group(5)), num(m.group(3))
        expect = float(money(Decimal(str(base_v)) * Decimal(str(pct_v)) / 100))
        amount = expect if any(abs(c - expect) <= 0.011 for c in cands) else (cands[-1] if cands else expect)
        return {"raw": raw, "page": page, "kind": "late_fee", "awb": None, "line_date": None, "lane": None,
                "desc": "Late fee", "qty": None, "unit": None, "rate": None, "pct": pct_v, "base": base_v,
                "amount": amount, "consistent": True,
                "late_fee": {"invoice_ref": norm_id(m.group(1)), "principal": num(m.group(3)), "month_no": int(m.group(4)),
                             "pct": num(m.group(5))}}
    am = AWB_RE.search(raw)
    if not am:
        return None
    awb = am.group(1)
    work = raw
    # dates (every format), remembering the first one as the line date
    dates = []
    for dm in re.finditer(DATE_PAT, work):
        d = parse_date(dm.group(0), dayfirst)
        if d:
            dates.append((dm.start(), dm.end(), d))
    for s, e, _d in sorted(dates, reverse=True):
        work = work[:s] + " " + work[e:]
    work = work.replace(awb, " ", 1)
    lm = LANE_RE.search(work)
    lane = lm.group(1) if lm else None
    if lm:
        work = work.replace(lm.group(1), " ", 1)
    work = re.sub(r"\(\s*(?:MAWB|ref)[^)]*\)", " ", work, flags=re.I)
    # percentage
    pm = re.search(r"(" + NUMRE + r")\s*%", work)
    pct = num(pm.group(1)) if pm else None
    if pm:
        work = work[:pm.start()] + " " + work[pm.end():]
    # quantity + unit
    qm = re.search(r"(" + NUMRE + r")\s*" + UNITS + r"\b", work, re.I)
    qty = unit = None
    if qm:
        qty, unit = num(qm.group(1)), qm.group(2).lower()
        unit = {"shipment": "shpt", "fee": "shpt", "fees": "shpt", "kgs": "kg", "day": "days", "months": "month", "pieces": "pcs"}.get(unit, unit)
        work = work[:qm.start()] + " " + work[qm.end():]
    nums = re.findall(r"(?<![A-Za-z0-9])" + NUMRE + r"(?![A-Za-z0-9])", work)
    # the leading line number ('1', '12') is not a value: drop a bare integer at the very start
    first = work.strip().split()
    if first and re.fullmatch(r"\d{1,3}", first[0]) and nums and nums[0] == first[0] and len(nums) > 1:
        nums = nums[1:]
    vals = [num(x) for x in nums]
    rate, base, amount, ok = _decode_numbers(vals, qty, unit, pct)
    desc = re.sub(r"[-\s]+", " ", re.sub(NUMRE, " ", work)).strip(" -|")
    if rate is None and qty is None and pct is None and unit is None and len(vals) >= 1:
        amount = vals[-1]
    if amount is None:
        return None
    return {"raw": raw, "page": page, "kind": "charge", "awb": awb, "line_date": dates[0][2] if dates else None,
            "lane": lane, "desc": desc, "qty": qty, "unit": unit, "rate": rate, "pct": pct, "base": base,
            "amount": amount, "consistent": ok}


# ---- whole document ----------------------------------------------------------------------------------------------------
_ID_PATS = [
    r"(?:CREDIT\s*NOTE|DEBIT\s*NOTE)\s+([A-Z0-9][A-Z0-9/\-]+)",
    r"(?:TAX\s*INVOICE|INVOICE)\s*(?:NO\.?|NUMBER|#)?\s*:?\s*([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)",
    r"Freight\s*bill\s*no\.?\s*:?\s*([A-Z0-9][A-Z0-9/\-]*)",
    r"Charges\s*advice\s*:?\s*([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)",
    r"STATEMENT\s*OF\s*ACCOUNT\s*([A-Z0-9][A-Z0-9/\-]*)",
]


def parse_document(pages: list[str], att_name: str = "", kind_hint: str = "invoice") -> dict:
    text = "\n".join(pages)
    head = "\n".join(text.split("\n")[:14])
    dayfirst = detect_dayfirst(text)
    inv_id = None
    for pat in _ID_PATS:
        m = re.search(pat, head, re.I)
        if m:
            inv_id = m.group(1).strip("-/")
            break
    if not inv_id and att_name:
        inv_id = att_name.rsplit(".", 1)[0]
    is_credit = bool(re.search(r"credit\s*note|credit\s*advice", head[:300], re.I)) or kind_hint == "credit_note"
    flat = re.sub(r"\s+", " ", text)
    inv_date = _find_date(flat, [r"Invoice\s*date", r"Issued\s*on", r"Document\s*date", r"Credit\s*date", r"Date\s*of\s*issue", r"Bill\s*date"], dayfirst)
    due = _find_date(flat, [r"Due\s*date", r"Settle\s*by", r"Payable\s*by", r"Pay\s*by", r"Payment\s*due"], dayfirst)
    cm = re.search(r"(?:Contract\s*reference|Service\s*agreement|Contract(?:\s*ref(?:erence)?)?)\s*:?\s*([A-Z]{2,4}-[A-Z0-9][A-Z0-9-]*)", flat, re.I)
    contract = cm.group(1).upper() if cm else None
    cur = None
    for pat in [r"Currency\s*:?\s*([A-Z]{3})", r"Amount\s*\(([A-Z]{3})\)", r"Net\s*\(([A-Z]{3})\)", r"Total(?:\s*payable)?\s*([A-Z]{3})\b", r"\b(USD|INR|EUR|GBP)\b"]:
        m = re.search(pat, flat, re.I)
        if m:
            cur = m.group(1).upper()
            break
    lines = []
    for pi, p in enumerate(pages, 1):
        for ln in p.split("\n"):
            pl = parse_line(ln, pi, dayfirst)
            if pl:
                lines.append(pl)
    for i, l in enumerate(lines, 1):
        l["idx"] = i

    def tot(labels):
        for lab in labels:
            m = re.search(rf"(?:^|\n)\s*{lab}\s*(?:[A-Z]{{3}})?\s*[:|]?\s*(?:[A-Z]{{3}})?\s*({NUMRE})\s*(?:\n|$)", text, re.I)
            if m:
                return num(m.group(1))
        return None
    # OCR output has no spaces ('TotalINR108485.85'); search a space-free copy as well
    squashed = "\n".join(re.sub(r"\s+", "", ln) for ln in text.split("\n"))
    def tot2(labels):
        v = tot(labels)
        if v is not None:
            return v
        for lab in labels:
            lab_s = re.sub(r"\s+", "", lab.replace(r"\s*", ""))
            m = re.search(rf"(?:^|\n){lab_s}(?:payable)?(?:[A-Z]{{3}})?[:|]?(?:[A-Z]{{3}})?({NUMRE})(?:\n|$)", squashed, re.I)
            if m:
                return num(m.group(1))
        return None
    total = tot2([r"Total\s*payable", r"Total\s*credit", r"Total\s*due", r"Balance\s*due", r"Amount\s*(?:due|payable)", r"Total"])
    subtotal = tot2([r"Subtotal", r"Sub\s*total", r"Net\s*charges", r"Charges"])
    tax = 0.0
    for m in re.finditer(r"(?:^|\n)\s*(?:GST|Tax|VAT)[^\n]*?([A-Z]{3})?\s*(" + NUMRE + r")\s*(?=\n|$)", text, re.I):
        tax = num(m.group(2))
    for m in re.finditer(r"(?:^|\n)\s*(?:GST|Tax|VAT)[^\n]*", squashed, re.I):
        mm = re.search(r"(?:[A-Z]{3})(" + NUMRE + r")$", m.group(0), re.I)
        if mm:
            tax = num(mm.group(1))
    out = {"kind": "credit_note" if is_credit else kind_hint, "invoice_id": inv_id, "invoice_key": norm_id(inv_id) if inv_id else None,
           "invoice_date": inv_date, "due_date": due, "contract_ref": contract, "currency": cur, "subtotal": subtotal,
           "tax_total": tax, "total": total, "lines": lines, "dayfirst": dayfirst}
    if is_credit:
        m = re.search(r"Credit\s*type\s*:?\s*(\w+)\s*\|\s*period\s*([A-Za-z0-9-]+)", flat, re.I)
        out["credit"] = {"type": m.group(1).lower() if m else None, "period": m.group(2).upper() if m else None,
                         "invoice_refs": sorted({norm_id(x) for x in re.findall(r"\b([A-Z]{2,4}-[A-Z0-9]+-\d{2,4}-?\d+|[A-Z]{2,4}-[A-Z]{2}-\d{4}-\d+)\b", flat)} - {norm_id(inv_id or '')})}
    return out


def validate(doc: dict) -> list[str]:
    """Problems found by checking the parse against the document's own printed numbers. [] = trustworthy."""
    issues = []
    if not doc.get("invoice_id"):
        issues.append("no document number")
    if not doc.get("invoice_date"):
        issues.append("no document date")
    lines = doc.get("lines", [])
    if doc["kind"] == "credit_note":
        return issues if doc.get("total") is not None else issues + ["no credit total"]
    if not lines:
        issues.append("no charge lines")
    if doc.get("subtotal") is None and doc.get("total") is None:
        issues.append("no printed total to check the lines against")
    bad = [l["raw"][:60] for l in lines if not l.get("consistent")]
    if bad:
        issues.append(f"{len(bad)} line(s) whose arithmetic does not work, e.g. {bad[0]!r}")
    s = round(sum(l["amount"] for l in lines), 2)
    sub, tot, tax = doc.get("subtotal"), doc.get("total"), doc.get("tax_total") or 0.0
    if sub is not None and abs(s - sub) > 0.02:
        issues.append(f"lines sum to {s} but the document prints subtotal {sub}")
    if sub is not None and tot is not None and abs(round(sub + tax, 2) - tot) > 0.02:
        issues.append(f"subtotal {sub} + tax {tax} != total {tot}")
    if sub is None and tot is not None and abs(round(s + tax, 2) - tot) > 0.02:
        issues.append(f"lines sum to {s} (+tax {tax}) but total is {tot}")
    return issues


def statement_to_doc(st: dict, pages: list[str]) -> dict:
    """Monthly Excel statement (tables.parse_statement) in the same shape as a parsed invoice."""
    lines = []
    for i, l in enumerate(st["lines"], 1):
        lines.append({"idx": i, "raw": l["line"], "page": l["page"], "kind": "charge", "awb": None, "line_date": None,
                      "lane": None, "desc": l["description"], "qty": None, "unit": None, "rate": l["rate"], "pct": None,
                      "base": None, "amount": l["amount"], "consistent": True, "reference": l["reference"]})
    return {"kind": "statement", "invoice_id": st["invoice_id_raw"], "invoice_key": norm_id(st["invoice_id_raw"]),
            "invoice_date": st["invoice_date"], "due_date": st["due_date"], "contract_ref": st["contract_ref"],
            "currency": st["currency"], "subtotal": st["total"], "tax_total": 0.0, "total": st["total"], "lines": lines}

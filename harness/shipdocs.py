"""Air waybills and shipper's declarations -> small fact records (no model)."""
from __future__ import annotations

import re

AWB = r"[A-Z]{2}\d{8}"
NUM = r"\d[\d,]*(?:\.\d+)?"


def parse_awb(pages: list[str]) -> dict | None:
    text = "\n".join(pages)
    flat = re.sub(r"[ \t]+", " ", text)
    m = re.search(r"(?:MASTER\s+)?AIR WAYBILL\s+(" + AWB + r"(?:-H\d+)?)", flat, re.I)
    if not m:
        m = re.search(r"\b(" + AWB + r"(?:-H\d+)?)\b", flat)
    if not m:
        return None
    awb = m.group(1)
    master_ref = re.search(r"Master AWB\s+(" + AWB + r")", flat)
    is_master = bool(re.search(r"MASTER AIR WAYBILL", flat)) or "Total chargeable weight" in flat
    cw = re.search(r"(Total\s+)?chargeable weight\s*:?\s*(" + NUM + r")\s*kg", flat, re.I)
    aw = re.search(r"Actual weight\s*:?\s*(" + NUM + r")\s*kg", flat, re.I)
    pcs = re.search(r"(Total\s+)?pieces\s*:?\s*(\d+)", flat, re.I)
    ex = re.search(r"Executed on\s+(\d{4}-\d{2}-\d{2})(?:\s+at\s+(\w+))?", flat)
    od = re.search(r"Origin\s+(\w+)\s*\|\s*Destination\s+(\w+)", flat)
    return {"awb": awb, "master": master_ref.group(1) if master_ref else (awb if is_master else None),
            "is_master_total": bool(is_master and cw and cw.group(1)),
            "chargeable_kg": float(cw.group(2).replace(",", "")) if cw else None,
            "actual_kg": float(aw.group(1).replace(",", "")) if aw else None,
            "pieces": int(pcs.group(2)) if pcs else None,
            "executed": ex.group(1) if ex else None, "origin": od.group(1) if od else None,
            "destination": od.group(2) if od else None}


def parse_dgd(pages: list[str]) -> dict | None:
    flat = re.sub(r"[ \t]+", " ", "\n".join(pages))
    m = re.search(r"DECLARATION FOR AWB\s+(" + AWB + ")", flat, re.I) or re.search(r"\b(" + AWB + r")\b", flat)
    if not m:
        return None
    uns = re.findall(r"\b(UN\d{4})\b\s*([^\n,]*)", flat)
    items = [{"un": u, "text": t.strip()} for u, t in uns]
    dry_ice = [i for i in items if i["un"] == "UN1845" or "dry ice" in i["text"].lower()]
    other = [i for i in items if i not in dry_ice]
    return {"awb": m.group(1), "items": items, "dry_ice": bool(dry_ice), "other_dg": bool(other),
            "refrigerant_only": bool(dry_ice) and not other and bool(re.search(r"refrigerant only|solely as a refrigerant", flat, re.I))}

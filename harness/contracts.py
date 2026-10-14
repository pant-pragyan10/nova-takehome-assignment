"""Contracts, amendments and annexes -> structured terms.

Two layers:
  1. tables.py parses the regular annex tables exactly (rates, fuel, accessorials, charge codes, FX, Schedule 1).
  2. Clause terms (term, rebate tiers, payment terms, late-fee rule ...) are read with strict patterns for the wordings
     we have seen. Any term the patterns cannot settle is listed in `unparsed` and sent to the model (llm.py), whose
     answer is only accepted if every number in it appears in the clause text it was shown.

Every term keeps the clause number it came from, so the engine can quote the clause as evidence.
"""
from __future__ import annotations

import re

import tables

DATE = r"\d{4}-\d{2}-\d{2}"
NUM = r"\d[\d,]*(?:\.\d+)?"


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _date_in(text: str, which: int = -1) -> str | None:
    ds = re.findall(DATE, text)
    return ds[which] if ds else None


# ---- clause scalars ------------------------------------------------------------------------------------------------------
def parse_terms(clauses: dict[str, dict]) -> tuple[dict, list[str]]:
    """-> (terms, unparsed). terms[name] = {'value': ..., 'clause': '10.2'} so the clause can be quoted."""
    T: dict[str, dict] = {}
    unparsed: list[str] = []

    def put(name, value, clause):
        T[name] = {"value": value, "clause": clause}

    def txt(c):
        return clauses[c]["text"] if c in clauses else ""

    # term
    t = txt("2.1")
    if t and _date_in(t):
        put("effective_date", _date_in(t, 0), "2.1")
    else:
        unparsed.append("effective_date")
    t = txt("2.2")
    if t and _date_in(t):
        put("term_end", _date_in(t, -1), "2.2")
        m = re.search(r"(\d+)\s*months?", t)
        if m:
            put("term_months", int(m.group(1)), "2.2")
    else:
        unparsed.append("term_end")
    t = txt("2.3")
    neg = re.search(r"(?:does|will|shall|do) not (?:\w+ ){0,2}(?:renew|continue)|not renew|no automatic renewal|ends? at the end of the initial term", t, re.I)
    pos = re.search(r"(?<!not )\brenews? automatically|automatically renews?|renews? for (?:successive|further|additional|consecutive)|is renewed automatically", t, re.I)
    if neg and not re.search(r"(?<!not )\brenews? (?:for|automatically) (?:successive|further|additional|consecutive)", t, re.I):
        put("auto_renews", False, "2.3")
    elif pos and not neg:
        unparsed.append("auto_renewal")          # an auto-renewing contract: renewal length and notice period need reading
    else:
        unparsed.append("auto_renewal")
    # free storage days
    m = re.search(r"-\s*(\d+)\s*free days", txt("6.1"))
    if m:
        put("storage_free_days", int(m.group(1)), "6.1")
    # volume rebate
    t = txt("7.1")
    tiers = [(_f(a), _f(b)) for a, b in re.findall(r"QS\s*>=\s*(?:USD|INR|EUR)?\s*(" + NUM + r")\s*:\s*(" + NUM + r")\s*%", t)]
    if tiers:
        put("rebate_tiers", sorted(tiers), "7.1")
        if re.search(r"applies to all (?:QS|Qualifying Spend)", t, re.I):
            put("rebate_basis", "all", "7.1")
        elif re.search(r"excess|incremental|marginal|portion|above the threshold|slice", t, re.I):
            put("rebate_basis", "marginal", "7.1")
        else:
            unparsed.append("rebate_basis")
    else:
        unparsed.append("rebate_tiers")
    m = re.search(r"(\d+)\s*days", txt("7.3"))
    if m:
        put("rebate_due_days", int(m.group(1)), "7.3")
    else:
        unparsed.append("rebate_due_days")
    # handling margin
    for c, nm in (("8.1", "handling_margin"), ("8.2", "margin_step_down")):
        t = txt(c)
        if t and re.search(r"not applicable", t, re.I):
            put(nm, None, c)
        elif t:
            unparsed.append(nm)
    # annual improvement credit
    t = txt("9.1")
    if t:
        pm = re.search(r"(" + NUM + r")\s*%", t)
        dm = re.search(r"(\d+)\s*days", t)
        if pm and dm and not re.search(r"not applicable", t, re.I):
            put("annual_credit_pct", _f(pm.group(1)), "9.1")
            put("annual_credit_days", int(dm.group(1)), "9.1")
        elif re.search(r"not applicable", t, re.I):
            put("annual_credit_pct", None, "9.1")
        else:
            unparsed.append("annual_credit")
    # payment
    m = re.search(r"(\d+)\s*days", txt("10.1"))
    if m:
        put("payment_days", int(m.group(1)), "10.1")
    else:
        unparsed.append("payment_days")
    t = txt("10.2")
    gm = re.search(r"more than\s*(\d+)\s*days", t, re.I)
    rm = re.search(r"(" + NUM + r")\s*%\s*per month", t, re.I)
    if gm and rm:
        put("late_fee_grace_days", int(gm.group(1)), "10.2")
        put("late_fee_pct_per_month", _f(rm.group(1)), "10.2")
    else:
        unparsed.append("late_fee")
    t = txt("10.3")
    if re.search(r"simple|principal only|only on the principal|not compound|on the principal (?:amount )?only", t, re.I):
        put("late_fee_compounds", False, "10.3")
    elif re.search(r"principal plus|added to the (?:overdue )?balance|compound", t, re.I):
        put("late_fee_compounds", True, "10.3")
    else:
        unparsed.append("late_fee_compounds")
    # who wins between a rate notice and an amendment
    t = (txt("13.3") + " " + txt("14.1") + " " + txt("14.2"))
    if re.search(r"neither prevails|resolve the difference jointly|does not rank", t, re.I):
        put("notice_vs_amendment", "unranked", "13.3")
    elif re.search(r"rate notice[^.]*prevail|notice prevails|correspondence prevails", t, re.I):
        put("notice_vs_amendment", "notice_wins", "14.2")
    elif re.search(r"amendment prevails over (?:any )?(?:rate )?notice|amendment[^.]*prevails[^.]*notice", t, re.I):
        put("notice_vs_amendment", "amendment_wins", "14.2")
    else:
        unparsed.append("notice_vs_amendment")
    m = re.search(r"within\s*(\d+)\s*days of the invoice date", txt("13.1"))
    if m:
        put("dispute_window_days", int(m.group(1)), "13.1")
    return T, unparsed


# ---- whole contract -------------------------------------------------------------------------------------------------------
def parse_msa(pages: list[str]) -> dict:
    text = "\n".join(pages)
    m = re.search(r"Agreement No\.?\s*([A-Z0-9][A-Z0-9-]+)", text)
    cid = m.group(1) if m else None
    clauses = tables.split_clauses(pages)
    periods = tables.parse_periods(pages)
    rates = tables.parse_rates(pages, len(periods))
    cur = None
    cm = re.search(r"Annex A[^\n]*\((USD|INR|EUR|GBP)\s+per", text)
    if cm:
        cur = cm.group(1)
    terms, unparsed = parse_terms(clauses)
    sched = tables.parse_schedule1(pages)
    parties = re.findall(r"\n(?:\(\d\))?\s*([A-Z][^\n]*?)\s*,\s*whose principal office[^\n]*?\(the (Shipper|Carrier)\)", text)
    return {"doc_type": "msa", "contract_id": cid, "currency": cur, "periods": periods, "rates": rates,
            "fuel": tables.parse_fuel(pages), "accessorials": tables.parse_accessorials(pages),
            "charge_codes": tables.parse_charge_codes(pages), "fx": tables.parse_fx(pages), "schedule1": sched,
            "terms": terms, "unparsed": unparsed,
            "clauses": {k: v for k, v in clauses.items()}, "signed": bool(re.search(r"Signed for and on behalf", text))}


def parse_amendment(pages: list[str]) -> dict:
    text = "\n".join(pages)
    flat = re.sub(r"\s+", " ", text)
    ref = re.search(r"Amendment reference\s+([A-Z0-9][A-Z0-9-]*/AMD-\d+)", flat)
    amd_ref = ref.group(1) if ref else None
    cid = amd_ref.split("/")[0] if amd_ref else (re.search(r"Amendment No\.?\s*\d+ to (?:Master Services Agreement \([^)]*\) )?([A-Z][A-Z0-9-]+)", flat) or [None, None])[1]
    eff = re.search(r"Effective from\s+(" + DATE + ")", flat)
    rates = tables.parse_rates(pages, 1)
    # paragraph-level actions
    actions = []
    paras = re.split(r"(?:^|\n)(?=\d+\.\s)", text)
    for p in paras:
        pf = re.sub(r"\s+", " ", p).strip()
        if not re.match(r"\d+\.\s", pf):
            continue
        pf = re.split(r"\s(?=All other terms|This Amendment may|Executed by)", pf)[0].strip()
        pm = re.search(r"(Annex A rev \d+)\s*\(attached\)\s*applies from\s*(" + DATE + r")(?:\s*to\s*(" + DATE + r"))?\s*to lane\(s\)\s*([A-Z0-9, -]+?)\.?\s*$", pf) \
            or re.search(r"(Annex A rev \d+)\s*\(attached\)\s*applies from\s*(" + DATE + r")()\s*to lane\(s\)\s*([A-Z0-9, -]+?)\.?(?:\s|$)", pf)
        if pm:
            actions.append({"type": "annex_reference", "annex": pm.group(1), "from": pm.group(2),
                            "to": pm.group(3) or None,
                            "lanes": [x.strip() for x in re.split(r"[ ,]+", pm.group(4).strip()) if re.fullmatch(r"[A-Z0-9-]+", x.strip())],
                            "text": pf})
        elif re.search(r"deleted and replaced with the following", pf, re.I) and rates:
            per = "every period" if re.search(r"for every period", pf, re.I) else None
            actions.append({"type": "replace_rates", "periods": per, "text": pf})
        elif re.search(r"extend|extended|extension", pf, re.I) and re.search(DATE, pf):
            actions.append({"type": "term_extension", "new_end": re.findall(DATE, pf)[-1], "text": pf})
        else:
            actions.append({"type": "other", "text": pf})
    return {"doc_type": "amendment", "contract_id": cid, "amendment_ref": amd_ref, "effective_from": eff.group(1) if eff else None,
            "rates": rates, "actions": actions, "signed": bool(re.search(r"Signed for and on behalf", text))}


def parse_annex(pages: list[str]) -> dict:
    """A separately delivered annex ('Annex A rev 2'): its name, the contract it belongs to, its rate rows and dates."""
    text = "\n".join(pages)
    flat = re.sub(r"\s+", " ", text)
    nm = re.search(r"(Annex [A-Z] rev \d+)", flat, re.I)
    cm = re.search(r"\b([A-Z]{2,4}-[A-Z]{2}-[A-Z]{2,4}-[A-Z0-9]+)\b", flat)
    periods = tables.parse_periods(pages)
    return {"doc_type": "annex", "name": nm.group(1) if nm else None, "contract_id": cm.group(1) if cm else None,
            "periods": periods, "rates": tables.parse_rates(pages, max(1, len(periods))),
            "effective_from": (re.search(r"(?:applies from|effective from|from)\s+(" + DATE + ")", flat, re.I) or [None, None])[1],
            "effective_to": (re.search(r"\bto\s+(" + DATE + ")", flat) or [None, None])[1],
            "lanes": sorted({r["lane"] for r in tables.parse_rates(pages, 1)})}

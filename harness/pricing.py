"""Rate resolution and shipment pricing (deterministic).

resolve_rate(): which instrument sets the Annex A rate for (contract, lane, pickup date) - the MSA, a signed amendment, an
Annex A revision attached to an amendment, or a rate notice from a Schedule 1 'rates' person - and what happens when two of
them are in force at once (conflict) or the one we need is not held (missing).
price_invoice(): reprice every line of an invoice from the contract, tracking, AWB and declaration facts.
"""
from __future__ import annotations

import re
from decimal import Decimal

from world import World, Contract, money, pdate, norm_awb, norm_name, add_months
from feedback import Feedback


# ---------------------------------------------------------------------------------------------------------------------
def clause_ev(c: Contract, num: str, limit: int = 300) -> dict | None:
    cl = c.clause(num)
    if not cl:
        return None
    t = cl["text"]
    if len(t) > limit:
        cut = t.rfind(". ", 0, limit)
        t = t[: cut + 1] if cut > 60 else t[:limit]
    return {"doc": c.msa_doc["ref"], "page": cl["page"], "quote": t}


def row_for_weight(rows: list[dict], kg: float):
    """rows: band rows of one lane with lo/hi. The band that contains kg (gaps of 0.5 go to the lower band)."""
    best = None
    for r in rows:
        if r["band"] == "min":
            continue
        lo = r["lo"] if r["lo"] is not None else 0.0
        hi = r["hi"] if r["hi"] is not None else float("inf")
        if lo <= kg <= hi:
            return r
        if lo <= kg and (best is None or lo > (best["lo"] or 0)):
            best = r
    return best


class Cand:
    """One instrument's rates for a lane at a pickup date."""

    def __init__(self, src, rows, level, doc_ref, label, learned=None, eff=None):
        self.src, self.rows, self.level, self.doc_ref, self.label = src, rows, level, doc_ref, label
        self.learned, self.eff = learned, eff

    def rate(self, kg):
        """-> (rate, min, evidence list) for a chargeable weight."""
        r = row_for_weight(self.rows, kg)
        mn = next((x for x in self.rows if x["band"] == "min"), None)
        ev = []
        if r:
            ev.append(r["ev"])
        if mn:
            ev.append(mn["ev"])
        return (r["v"] if r else None), (mn["v"] if mn else None), ev


class Resolution:
    def __init__(self, status, cand=None, alt=None, missing=None, why=None):
        self.status = status           # ok | conflict | missing | none
        self.cand, self.alt, self.missing, self.why = cand, alt, missing, why


def _msa_cand(c: Contract, lane: str, pickup) -> Cand | None:
    d = c.d
    rows = [r for r in d["rates"] if r["lane"] == lane]
    if not rows:
        return None
    periods = d["periods"]
    idx = 0
    for i, p in enumerate(periods):
        if pdate(p["from"]) <= pickup:
            idx = i
    out = []
    for r in rows:
        vals = r["values"]
        v = vals[idx] if idx < len(vals) else vals[-1]
        out.append({"band": r["band"], "lo": r["lo"], "hi": r["hi"], "v": Decimal(str(v)),
                    "ev": {"doc": c.msa_doc["ref"], "page": r["page"], "quote": r["line"]}})
    return Cand("msa", out, 1, c.msa_doc["ref"], "MSA Annex A")


def _amend_cands(w: World, c: Contract, lane: str, pickup):
    """All amendment instruments for the lane in force at pickup -> list of (eff_date, Cand | ('missing', name, action, doc))."""
    found = []
    for doc, f in c.amendments:
        a = f["data"]
        if a.get("signed") is False:
            continue
        eff = pdate(a.get("effective_from"))
        for act in a.get("actions", []):
            if act["type"] == "replace_rates" and eff and eff <= pickup:
                rows = [r for r in a["rates"] if r["lane"] == lane]
                if rows:
                    out = [{"band": r["band"], "lo": r["lo"], "hi": r["hi"], "v": Decimal(str(r["values"][0])),
                            "ev": {"doc": doc["ref"], "page": r["page"], "quote": r["line"]}} for r in rows]
                    found.append((eff, doc["received_ts"], Cand("amendment", out, 1, doc["ref"], a.get("amendment_ref") or "amendment", eff=eff)))
            elif act["type"] == "annex_reference" and lane in act["lanes"]:
                frm, to = pdate(act["from"]), pdate(act["to"])
                if frm and frm <= pickup and (to is None or pickup <= to):
                    ann = None
                    for adoc, af in c.annexes:
                        if norm_name(af["data"].get("name")) == norm_name(act["annex"]):
                            ann = (adoc, af["data"])
                    if ann is None:
                        found.append((frm, doc["received_ts"], ("missing", act["annex"], act, doc)))
                    else:
                        adoc, ad = ann
                        rows = [r for r in ad["rates"] if r["lane"] == lane]
                        idx = 0
                        for i, p in enumerate(ad.get("periods") or []):
                            if pdate(p["from"]) <= pickup:
                                idx = i
                        out = [{"band": r["band"], "lo": r["lo"], "hi": r["hi"],
                                "v": Decimal(str(r["values"][idx] if idx < len(r["values"]) else r["values"][-1])),
                                "ev": {"doc": adoc["ref"], "page": r["page"], "quote": r["line"]}} for r in rows]
                        if out:
                            found.append((frm, adoc["received_ts"], Cand("annex", out, 1, adoc["ref"], act["annex"], eff=frm)))
    found.sort(key=lambda t: (t[0], t[1]))
    return found


def notice_authorised(w: World, c: Contract, fb: Feedback, x: dict, lane: str):
    """-> (ok, learned feedback item | None). x = rate_notice email fact with its doc."""
    doc = x["doc"]
    if x.get("quoted"):
        return False, None
    p = c.person_by_email(doc["from_addr"])
    if p and "rates" in p["categories"] and p["party"] == c.carrier_party():
        return True, None
    when = pdate(doc["received"])
    it = fb.accepted_rate(c.id, lane, pdate(x.get("effective_from")) or when, sender=doc["from_name"], effective_from=x.get("effective_from"))
    if it:
        return True, it
    if p:
        it = fb.authority(p["name"], "rates", c.id, when)
        if it:
            return True, it
    return False, None


def _notice_cand(w: World, c: Contract, fb: Feedback, lane: str, pickup):
    best = None
    for x in w.email_facts:
        if x["type"] != "rate_notice":
            continue
        ref = x.get("ref") or ""
        if "/" in ref and ref.split("/")[0] != c.id:
            continue
        if "/" not in ref and not (c.person_by_email(x["doc"]["from_addr"]) or c.id in " ".join(r.get("quote", "") for r in x.get("rows", []))):
            continue
        rows = [r for r in x["rows"] if r["lane"] == lane]
        if not rows:
            continue
        ef, et = pdate(x.get("effective_from")), pdate(x.get("effective_to"))
        if ef is None or ef > pickup or (et and pickup > et):
            continue
        ok, learned = notice_authorised(w, c, fb, x, lane)
        if not ok:
            continue
        out = [{"band": r["band"], "lo": r["lo"], "hi": r["hi"], "v": Decimal(str(r["rate"])),
                "ev": {"doc": x["doc"]["ref"], "page": 1, "quote": r["quote"]}} for r in rows]
        cand = Cand("notice", out, 2, x["doc"]["ref"], x.get("ref") or "rate notice", learned=learned, eff=ef)
        key = (ef, x["doc"]["received_ts"])
        if best is None or key > best[0]:
            best = (key, cand)
    return best[1] if best else None


def resolve_rate(w: World, c: Contract, fb: Feedback, lane: str, pickup) -> Resolution:
    eff = pdate(c.term("effective_date"))
    if eff and pickup < eff:
        return Resolution("none", why="pickup before the contract took effect")
    msa = _msa_cand(c, lane, pickup)
    amds = _amend_cands(w, c, lane, pickup)
    amd = None
    if amds:
        top = amds[-1][2]
        if isinstance(top, tuple):
            return Resolution("missing", missing=top)
        amd = top
    notice = _notice_cand(w, c, fb, lane, pickup)
    if amd and notice:
        pref = fb.precedence(c.id, lane, pickup)
        if pref:
            use = (pref["fact"].get("value") or {}).get("use")
            chosen = notice if use == "notice" else amd
            chosen.learned = pref
            return Resolution("ok", cand=chosen)
        rule = c.term("notice_vs_amendment", "unranked")
        if rule == "amendment_wins":
            return Resolution("ok", cand=amd)
        if rule == "notice_wins":
            return Resolution("ok", cand=notice)
        return Resolution("conflict", cand=amd, alt=notice)
    cand = notice or amd or msa
    if cand is None:
        return Resolution("none", why="no rate for this lane")
    return Resolution("ok", cand=cand)


# ---------------------------------------------------------------------------------------------------------------------
def norm_desc(s: str) -> str:
    s = re.sub(r"\(.*?\)", " ", s or "")
    return re.sub(r"[^a-z0-9]+", "", s.lower())      # spaces dropped: OCR output has none


def code_of(c: Contract, desc: str) -> str | None:
    n = norm_desc(desc)
    best = None
    for cc in c.d.get("charge_codes", []):
        for lab in cc["labels"] + [cc["label"]]:
            ln = norm_desc(lab)
            if ln == n:
                return cc["code"]
            if ln and (ln in n or n in ln) and min(len(ln), len(n)) >= 4:
                if best is None or len(ln) > best[0]:
                    best = (len(ln), cc["code"])
    return best[1] if best else None


def fee_of(c: Contract, code: str):
    for a in c.d.get("accessorials", []):
        if a["code"] == code:
            return a
    return None


def fuel_pct(c: Contract, pickup):
    m = pickup.strftime("%Y-%m")
    for r in c.d.get("fuel", []):
        if r["month"] == m:
            return r
    return None


class Priced:
    def __init__(self):
        self.lines = []              # dicts: line, code, correct, diff, ev(list), why
        self.evidence = []
        self.lanes = set()           # (lane, rate period idx)
        self.min_tier = 1            # weakest pickup source used (1 best .. 4 worst)
        self.learned = None
        self.missing = None
        self.conflicts = []          # (lane, Resolution)
        self.authority_ok = True
        self.skipped = []

    @property
    def diff(self) -> Decimal:
        return sum((l["diff"] for l in self.lines), Decimal(0))


def price_invoice(w: World, rec: dict, c: Contract, fb: Feedback, pick: dict | None = None, exclude: set | None = None) -> Priced:
    """Reprice a normal invoice. pick={'lane': 'cand'|'alt'} chooses the side of a conflict. exclude = line idx to leave out."""
    d = rec["d"]
    doc = rec["doc"]
    out = Priced()
    exclude = exclude or set()
    pick = pick or {}
    by_awb: dict[str, list[dict]] = {}
    for ln in d["lines"]:
        if ln["kind"] != "charge" or ln.get("idx") in exclude:
            continue
        if ln.get("awb"):
            by_awb.setdefault(ln["awb"], []).append(ln)
        else:
            by_awb.setdefault(None, []).append(ln)

    def inv_ev(ln):
        return {"doc": doc["ref"], "page": ln.get("page", 1), "quote": ln["raw"]}

    for awb, lines in by_awb.items():
        if awb is None:
            _price_statement_lines(w, c, rec, lines, out, inv_ev)
            continue
        lane = next((l["lane"] for l in lines if l.get("lane")), None)
        line_date = next((l["line_date"] for l in lines if l.get("line_date")), None)
        pdt, tier, pev = w.pickup(awb, line_date)
        if pdt is None:
            continue
        out.min_tier = max(out.min_tier, tier)
        res = resolve_rate(w, c, fb, lane, pdt) if lane else Resolution("none")
        if res.status == "missing":
            out.missing = res.missing
            continue
        cand = None
        if res.status == "conflict":
            out.conflicts.append((lane, res))
            cand = res.alt if pick.get(lane) == "alt" else res.cand
        elif res.status == "ok":
            cand = res.cand
        if cand and cand.learned:
            out.learned = cand.learned
        if cand and cand.level > 2:
            out.authority_ok = False
        period_idx = 0
        for i, p in enumerate(c.d["periods"]):
            if pdate(p["from"]) <= pdt:
                period_idx = i
        if lane:
            out.lanes.add((lane, period_idx))
        correct_freight = Decimal(0)
        freight_seen = False
        base_by_lane: dict[str, Decimal] = {}
        for ln in sorted(lines, key=lambda l: 0 if code_of(c, l["desc"]) == "FRT" else 1):
            code = code_of(c, ln["desc"])
            ln["_code"] = code
            billed = money(ln["amount"])
            corr, why, evs = None, "", []
            if code == "FRT" and cand and ln.get("unit") in ("kg", None) and ln.get("qty"):
                kg = float(ln["qty"])
                rate, mn, rev = cand.rate(kg)
                if rate is not None:
                    corr = money(rate * Decimal(str(ln["qty"])))
                    if mn is not None and corr < mn:
                        corr = mn
                    evs = rev + [e for e in (clause_ev(c, "3.2"), clause_ev(c, "3.4")) if e] + ([pev] if pev else [])
                    why = "rate"
                    correct_freight += corr
                    freight_seen = True
            elif code == "FUEL":
                fp = fuel_pct(c, pdt)
                if fp:
                    base = correct_freight if freight_seen else (money(ln["base"]) if ln.get("base") else None)
                    if base is not None:
                        corr = money(base * Decimal(str(fp["pct"])) / 100)
                        evs = [{"doc": c.msa_doc["ref"], "page": fp["page"], "quote": fp["line"]}] + [e for e in (clause_ev(c, "4.1"),) if e] + ([pev] if pev else [])
                        why = "fuel"
            elif code == "SEC":
                fe = fee_of(c, "SEC")
                kg = ln.get("qty")
                if fe and kg:
                    corr = money(Decimal(str(fe["fee"])) * Decimal(str(kg)))
                    evs = [{"doc": c.msa_doc["ref"], "page": fe["page"], "quote": fe["line"]}] + [e for e in (clause_ev(c, "4.2"),) if e]
                    why = "security"
            elif code in ("DG", "DRYICE"):
                corr, evs, why = _dg_line(w, c, awb, code, ln)
            elif code == "PEAK":
                corr, evs, why = _peak_line(w, c, ln, pdt)
            elif code == "STOR":
                corr, evs, why = _storage_line(w, c, awb, ln)
            elif code == "DLV":
                corr, evs, why = _delivery_line(w, c, awb, ln)
            elif code == "CUSTOMS":
                corr, evs, why = _customs_line(w, c, awb, ln)
            if corr is None:
                out.skipped.append(ln)
                continue
            diff = billed - corr
            out.lines.append({"line": ln, "code": code, "correct": corr, "billed": billed, "diff": diff, "why": why,
                              "ev": [inv_ev(ln)] + evs, "awb": awb, "lane": lane})
    return out


def _price_statement_lines(w, c, rec, lines, out, inv_ev):
    """Monthly programme / account fees: billed rate must equal the Annex D fee."""
    for ln in lines:
        code = code_of(c, ln["desc"])
        ln["_code"] = code
        fe = fee_of(c, code) if code else None
        if not fe or "month" not in (fe.get("basis") or ""):
            continue
        qty = Decimal(str(ln["qty"])) if ln.get("qty") else Decimal(1)
        corr = money(Decimal(str(fe["fee"])) * qty)
        billed = money(ln["amount"])
        out.lines.append({"line": ln, "code": code, "correct": corr, "billed": billed, "diff": billed - corr, "why": "fee",
                          "ev": [inv_ev(ln), {"doc": c.msa_doc["ref"], "page": fe["page"], "quote": fe["line"]}], "awb": None, "lane": None})


def _dg_line(w, c, awb, code, ln):
    dg = w.dgds.get(awb) or w.dgds.get(norm_awb(awb))
    fe = fee_of(c, code)
    if not dg or not fe:
        return None, [], ""
    ev = [{"doc": dg["doc"]["ref"], "page": 1, "quote": _dgd_quote(w, dg)}]
    if dg["other_dg"]:
        expected = "DG"
        cl = clause_ev(c, "5.3") or clause_ev(c, "5.1")
    elif dg["refrigerant_only"]:
        expected = "DRYICE"
        cl = clause_ev(c, "5.2")
    else:
        return None, [], ""
    if cl:
        ev.append(cl)
    ev.append({"doc": c.msa_doc["ref"], "page": fe["page"], "quote": fe["line"]})
    if code != expected:
        # a fee for the wrong kind of goods. A replacement fee for the right kind is not charged here (undercharge is ignored)
        return money(0), ev, "dg_wrong_fee"
    return money(Decimal(str(fe["fee"]))), ev, "dg_fee"


def _dgd_quote(w, dg):
    pages = w.by_ref[dg["doc"]["ref"]]["pages"]
    txt = "\n".join(pages)
    m = re.search(r"UN\d{4}[^\n]*", txt)
    return m.group(0).strip() if m else "UN"


def _storage_line(w, c, awb, ln):
    ev_list = w.events(awb)
    free = c.term("storage_free_days")
    fe = fee_of(c, "STOR")
    if not ev_list or free is None or not fe:
        return None, [], ""
    release = next((e for e in ev_list if e["event"] in ("DELIVERED", "COLLECTED_BY_CONSIGNEE")), None)
    if not release:
        return None, [], ""
    arrivals = [e for e in ev_list if e["event"] == "ARRIVED_FACILITY" and e["date"] <= release["date"]]
    if not arrivals:
        return None, [], ""
    arr = arrivals[-1]
    days = max(0, (pdate(release["date"]) - pdate(arr["date"])).days - free)
    corr = money(Decimal(str(fe["fee"])) * days)
    ev = [{"doc": arr["doc"]["ref"], "page": arr["page"], "quote": arr["line"]},
          {"doc": release["doc"]["ref"], "page": release["page"], "quote": release["line"]},
          {"doc": c.msa_doc["ref"], "page": fe["page"], "quote": fe["line"]}]
    cl = clause_ev(c, "6.1")
    if cl:
        ev.append(cl)
    return corr, ev, "storage"


def _delivery_line(w, c, awb, ln):
    ev_list = w.events(awb)
    fe = fee_of(c, "DLV")
    if not fe:
        return None, [], ""
    if not ev_list:
        return money(Decimal(str(fe["fee"]))), [{"doc": c.msa_doc["ref"], "page": fe["page"], "quote": fe["line"]}], "delivery_fee"
    col = next((e for e in ev_list if e["event"] == "COLLECTED_BY_CONSIGNEE"), None)
    dlv = next((e for e in ev_list if e["event"] == "DELIVERED"), None)
    ev = []
    if col and not dlv:
        ev = [{"doc": col["doc"]["ref"], "page": col["page"], "quote": col["line"]}]
        cl = clause_ev(c, "6.2")
        if cl:
            ev.append(cl)
        return money(0), ev, "no_delivery"
    if dlv:
        return money(Decimal(str(fe["fee"]))), [{"doc": c.msa_doc["ref"], "page": fe["page"], "quote": fe["line"]}], "delivery_fee"
    return None, [], ""


def _customs_line(w, c, awb, ln):
    fe = fee_of(c, "CUSTOMS")
    holds = [x for x in w.email_facts if x["type"] == "customs_hold" and norm_awb(x["awb"]) == norm_awb(awb) and not x.get("quoted")]
    if not fe or not holds:
        return None, [], ""
    h = holds[-1]
    txt = (h.get("reason") or "") + " " + (h.get("reason_quote") or "")
    carrier_fault = bool(re.search(r"carrier|error in documents|filed by the carrier|prepared by the carrier|omission", txt, re.I)) \
        and not re.search(r"shipper'?s? (?:import )?(?:permit|document)|SHIPPER_", txt, re.I)
    ev = [{"doc": h["doc"]["ref"], "page": 1, "quote": h.get("reason_quote") or h["quote"]}]
    cl = clause_ev(c, "6.3")
    if cl:
        ev.append(cl)
    if carrier_fault:
        return money(0), ev, "customs_carrier_fault"
    return money(Decimal(str(fe["fee"]))), ev, "customs_fee"


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


def _peak_line(w, c, ln, pickup):
    fe = fee_of(c, "PEAK")
    if not fe or not ln.get("qty"):
        return None, [], ""
    text = " ".join(w.by_ref[c.msa_doc["ref"]]["pages"])
    m = re.search(r"PEAK[^\n]*?pickups in\s+([A-Za-z]+)\s+to\s+([A-Za-z]+)", re.sub(r"\s+", " ", text), re.I)
    if not m or m.group(1).lower() not in MONTHS or m.group(2).lower() not in MONTHS:
        return None, [], ""
    a, b = MONTHS[m.group(1).lower()], MONTHS[m.group(2).lower()]
    inside = (a <= pickup.month <= b) if a <= b else (pickup.month >= a or pickup.month <= b)
    ev = [{"doc": c.msa_doc["ref"], "page": fe["page"], "quote": fe["line"]}]
    if not inside:
        return money(0), ev, "peak_out_of_season"
    return money(Decimal(str(fe["fee"])) * Decimal(str(ln["qty"]))), ev, "peak"

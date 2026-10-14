"""The auditor: facts in, report out. No model calls happen here.

build_report(state_dir, as_of, mode) -> report dict (schema: grader/schema/report.schema.json).

Order of work: waivers, duplicates, overcharges (with conflicts / missing documents), late fees, then the portfolio
(cashback, expiry, late-fee risk). Findings are then given an action by mode and a confidence, and compared with the previous
report so changed keys carry `supersedes` / `retro`.
"""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from pathlib import Path

import store
from feedback import Feedback
from pricing import clause_ev, code_of, price_invoice, fee_of
from world import World, Contract, money, d2s, pdate, norm_awb, norm_inv, norm_name, locate, squash, add_months

THRESHOLD = Decimal("5000.00")
OUTBOUND = {"draft:dispute", "draft:claim_credit", "draft:request_document"}


# ---------------------------------------------------------------------------------------------------------------------
class Ctx:
    def __init__(self, w: World, fb: Feedback, mode: str):
        self.w, self.fb, self.mode = w, fb, mode
        self.items: list[dict] = []          # findings (invoice level)
        self.portfolio = {"cashback": [], "expiry": [], "late_fee_risk": []}
        self.disputed: dict[str, Decimal] = {}     # invoice key -> amount we dispute, in the invoice's own currency
        self.waived_lines: dict[str, set] = {}     # invoice key -> line idx taken by a waiver finding
        self.dup_lines: dict[str, set] = {}

    def add_disputed(self, key: str, amt: Decimal):
        self.disputed[key] = self.disputed.get(key, Decimal(0)) + amt


def ev_ok(w: World, e: dict | None) -> bool:
    if not e or not e.get("doc") or e["doc"] not in w.by_ref:
        return False
    q = squash(e["quote"])
    if len(q) < 12:
        return False
    pages = w.by_ref[e["doc"]]["pages"]
    pg = e.get("page", 1)
    return 1 <= pg <= len(pages) and q in squash(pages[pg - 1])


def clean_ev(w: World, evs: list[dict]) -> list[dict]:
    out, seen = [], set()
    for e in evs:
        if not e:
            continue
        e = dict(e)
        if not ev_ok(w, e):
            pg = locate(w, e.get("doc"), e.get("quote", ""))
            if pg is None:
                continue
            e["page"] = pg
        k = (e["doc"], e["page"], squash(e["quote"]))
        if k in seen:
            continue
        seen.add(k)
        out.append({"doc": e["doc"], "page": int(e["page"]), "quote": e["quote"].strip()})
    return out


def email_line(w: World, ref: str, pattern: str | None = None, fallback_first=True) -> dict | None:
    """A line (sentence) of an email body for use as a quote."""
    doc = w.by_ref.get(ref)
    if not doc:
        return None
    lines = [l.strip() for l in doc["pages"][0].split("\n") if len(squash(l)) >= 12]
    if pattern:
        for l in lines:
            if re.search(pattern, l, re.I):
                return {"doc": ref, "page": 1, "quote": l}
    return {"doc": ref, "page": 1, "quote": lines[0]} if (lines and fallback_first) else None


def total_line(w: World, rec: dict) -> dict | None:
    for i, p in enumerate(rec["doc"]["pages"], 1):
        for l in p.split("\n"):
            if re.match(r"^\s*(Total|Net charges|Balance due|Amount due)\b", l, re.I) and len(squash(l)) >= 12:
                return {"doc": rec["doc"]["ref"], "page": i, "quote": l.strip()}
    return None


def item(cls, key_fields: dict, verdict, amount=None, rng=None, action="none", conf=0.9, ev=None, **extra) -> dict:
    ident = {"overcharge": "overcharge", "duplicate": "duplicate", "waived_but_billed": "waived_but_billed",
             "late_fee_not_owed": "late_fee_not_owed", "late_fee_risk": "late_fee_risk"}
    if cls == "cashback":
        iid = f"cashback:{key_fields['contract_id']}/{key_fields['period']}/{key_fields['credit_type']}"
    elif cls == "expiry":
        iid = f"expiry:{key_fields['contract_id']}"
    else:
        iid = f"{cls}:{key_fields['invoice_id']}"
    it = {"id": iid, "class": cls, **key_fields, "verdict": verdict,
          "amount_usd": d2s(amount) if amount is not None else None,
          "amount_range": [d2s(rng[0]), d2s(rng[1])] if rng else None,
          "deadline": extra.pop("deadline", None), "action": action, "confidence": conf, "evidence": ev or [],
          "missing_doc": extra.pop("missing_doc", None), "supersedes": [], "learned_at": None}
    it.update(extra)
    return it


# =====================================================================================================================
# waived but billed
# =====================================================================================================================
def waiver_condition_met(ctx: Ctx, c: Contract, wv: dict, rec: dict) -> bool | None:
    """True/False when we can tell, None when the condition cannot be read."""
    txt = wv.get("condition_text") or ""
    if not wv.get("conditional") and not txt:
        return True
    w = ctx.w
    m = re.search(r"paid[^.]*?\bby\s+(\d{4}-\d{2}-\d{2})", txt, re.I)
    if m:
        deadline = pdate(m.group(1))
        target = None
        mi = re.search(r"invoice\s+([A-Z0-9][A-Z0-9/\-]+)", txt, re.I)
        key = norm_inv(mi.group(1)) if mi else norm_inv(rec["d"]["invoice_id"])
        row = w.ledger.get(key)
        if row and row["status"] == "paid" and pdate(row["paid_date"]) and pdate(row["paid_date"]) <= deadline:
            return True
        return False
    return None


def waivers(ctx: Ctx):
    w, fb = ctx.w, ctx.fb
    seen_invoices: dict[str, dict] = {}
    for rec in w.invoices:
        seen_invoices.setdefault(norm_inv(rec["d"]["invoice_id"]), rec)
    facts = [x for x in w.email_facts if x["type"] == "waiver"]
    for key, rec in seen_invoices.items():
        c = w.contract_of(rec)
        if not c:
            continue
        d = rec["d"]
        matched: list[tuple[dict, dict, dict, dict | None]] = []     # (line, waiver fact, auth ok, learned)
        quoted_only: list[tuple[dict, dict]] = []
        fb_scope = fb.waiver_scope(d["invoice_id"])
        rejected = any(i["decision"] == "reject" for i in fb_scope)
        for ln in d["lines"]:
            if ln["kind"] != "charge":
                continue
            code = code_of(c, ln["desc"])
            if not code:
                continue
            for x in facts:
                if x["doc"]["batch"] is None:
                    pass
                wcode = code_of(c, x.get("charge") or "")
                if wcode != code:
                    continue
                if not _waiver_names(x, ln, d):
                    continue
                if x.get("quoted"):
                    quoted_only.append((ln, x))
                    continue
                p = c.person_by_email(x["doc"]["from_addr"])
                learned = None
                ok = bool(p and "waivers" in p["categories"] and p["party"] == c.carrier_party())
                if not ok and p:
                    it = fb.authority(p["name"], "waivers", c.id, pdate(x["doc"]["received"]))
                    if it:
                        ok, learned = True, it
                if not ok:
                    continue
                cond = waiver_condition_met(ctx, c, x, rec)
                if cond is not True:
                    continue
                matched.append((ln, x, {"p": p}, learned))
                break
            else:
                # no waiver in the mailbox: a person may have approved it one-off
                for it in fb_scope:
                    months = ((it["fact"].get("value") or {}).get("months")) or []
                    ref = ln.get("reference") or (d.get("invoice_date") or "")[:7]
                    if it["decision"] == "one_off_approval" and ref[:7] in months:
                        matched.append((ln, {"doc": None, "quote": None, "charge": ln["desc"]}, {"p": None}, it))
        if rejected:
            matched = [m for m in matched if m[3] is None or m[3].get("decision") != "one_off_approval"]
            continue
        if matched:
            total = sum((money(m[0]["amount"]) for m in matched), Decimal(0))
            ev = []
            learned = next((m[3] for m in matched if m[3]), None)
            for ln, x, a, _ in matched:
                ev.append({"doc": rec["doc"]["ref"], "page": ln.get("page", 1), "quote": ln["raw"]})
                if x.get("doc"):
                    ev.append({"doc": x["doc"]["ref"], "page": 1, "quote": x["quote"]})
                    if a["p"]:
                        ev.append({"doc": c.msa_doc["ref"], "page": a["p"]["page"], "quote": a["p"]["line"]})
            for n in ("11.1", "11.2"):
                ev.append(clause_ev(c, n))
            usd, fxr = w.to_usd(c, total, d.get("invoice_date"), d.get("currency", "USD"))
            ctx.waived_lines.setdefault(key, set()).update(m[0].get("idx") for m in matched)
            if usd is None:
                ctx.items.append(_fx_missing(ctx, c, rec, "waived_but_billed", ev))
                continue
            if fxr:
                ev.append({"doc": c.msa_doc["ref"], "page": fxr["page"], "quote": fxr["line"]})
            ctx.add_disputed(key, total)
            it = item("waived_but_billed", {"invoice_id": d["invoice_id"]}, "finding", amount=usd, action="draft:dispute",
                      conf=0.95, ev=ev)
            it["_native"] = total
            it["_rec"] = rec
            if learned:
                it["learned_at"] = Feedback.learned(learned)
            ctx.items.append(it)
        elif quoted_only:
            ln, x = quoted_only[0]
            holder = _quoted_holder(ctx, c, x)
            name = _quoted_name(ctx, x)
            ev = [{"doc": rec["doc"]["ref"], "page": ln.get("page", 1), "quote": ln["raw"]},
                  {"doc": x["doc"]["ref"], "page": 1, "quote": x["quote"]}, clause_ev(c, "14.3"), clause_ev(c, "11.1")]
            ctx.items.append(item("waived_but_billed", {"invoice_id": d["invoice_id"]}, "cannot_determine", action="draft:request_document",
                                  conf=0.85, ev=ev, missing_doc={"name": name, "holder": holder}))
            ctx.items[-1]["_rec"] = rec


def _waiver_names(x: dict, ln: dict, d: dict) -> bool:
    awb = norm_awb(ln.get("awb") or "")
    if awb and any(norm_awb(a) == awb for a in x.get("awbs", [])):
        return True
    if norm_inv(d["invoice_id"]) in [norm_inv(i) for i in x.get("invoices", [])]:
        return True
    ref = (ln.get("reference") or "")[:7]
    if ref and ref in (x.get("months") or []):
        return True
    return False


def _quoted_name(ctx: Ctx, x: dict) -> str:
    doc = ctx.w.by_ref[x["doc"]["ref"]]
    m = re.search(r"^\s*>*\s*Subject:\s*(.+)$", "\n".join(doc["pages"]), re.M | re.I)
    return (m.group(1).strip() if m else None) or "Original waiver email"


def _quoted_holder(ctx: Ctx, c: Contract, x: dict) -> dict:
    doc = ctx.w.by_ref[x["doc"]["ref"]]
    text = "\n".join(doc["pages"])
    for m in re.finditer(r"^\s*>*\s*From:\s*([^<\n]+?)\s*(?:<([^>\n]+)>)?\s*$", text, re.M | re.I):
        name, addr = m.group(1).strip(), (m.group(2) or "").strip()
        p = c.person_by_email(addr) if addr else None
        if p is None:
            p = next((q for q in c.schedule1() if norm_name(q["name"]) == norm_name(name)), None)
        if p and "waivers" in p["categories"]:
            return {"party": p["party"], "person": p["name"], "role": p["title"]}
    k = c.kam()
    return {"party": c.carrier_party(), "person": k["name"], "role": k["title"]} if k else {"party": c.carrier_party()}


def _fx_missing(ctx: Ctx, c: Contract, rec: dict, cls: str, ev: list) -> dict:
    t = c.contact("treasury")
    holder = {"party": c.carrier_party(), "person": t["name"], "role": t["title"]} if t else {"party": c.carrier_party()}
    it = item(cls, {"invoice_id": rec["d"]["invoice_id"]}, "cannot_determine", action="draft:request_document", conf=0.85, ev=ev,
              missing_doc={"name": "Annex F", "holder": holder})
    it["_rec"] = rec
    return it


# =====================================================================================================================
# duplicates
# =====================================================================================================================
COPY_MARK = re.compile(r"\b(copy|reminder|for (?:your|our) records|duplicate for information|statement only)\b", re.I)


def credits_for(ctx: Ctx, keys: set[str]) -> list[dict]:
    out = []
    for cr in ctx.w.credit_notes:
        refs = {norm_inv(r) for r in (cr["d"].get("credit") or {}).get("invoice_refs", [])}
        if refs & keys:
            out.append(cr)
    return out


def credit_total(ctx: Ctx, crs: list[dict]) -> Decimal:
    return sum((money(abs(cr["d"].get("total") or 0)) for cr in crs), Decimal(0))


def duplicates(ctx: Ctx):
    w = ctx.w
    groups: dict[str, list[dict]] = {}
    for rec in w.invoices:
        groups.setdefault(norm_inv(rec["d"]["invoice_id"]), []).append(rec)
    flagged: set[str] = set()
    # (a) the same invoice number sent again
    for key, recs in groups.items():
        if len(recs) < 2:
            continue
        recs = sorted(recs, key=lambda r: r["doc"]["received_ts"])
        first = recs[0]
        row = w.ledger.get(key)
        if not row or row["status"] not in ("paid", "scheduled", "approved", "queued"):
            continue
        for later in recs[1:]:
            text = "\n".join(later["doc"]["pages"])
            body = "\n".join(w.by_ref[later["doc"]["email_ref"]]["pages"]) if later["doc"]["email_ref"] in w.by_ref else ""
            if COPY_MARK.search(text) or COPY_MARK.search(body):
                continue
            c = w.contract_of(first)
            total = money(first["d"]["total"] or 0)
            crs = credits_for(ctx, {key})
            cred = credit_total(ctx, crs)
            amt = total - cred
            if amt <= 0 or c is None:
                continue
            usd, fxr = w.to_usd(c, amt, later["d"].get("invoice_date"), later["d"].get("currency", "USD"))
            ev = [{"doc": row["doc"]["ref"], "page": row.get("page", 1), "quote": row["line"]},
                  email_line(w, later["doc"]["email_ref"], r"pay|remit|due|invoice"),
                  {"doc": first["doc"]["ref"], "page": 1, "quote": (total_line(w, first) or {}).get("quote", "")},
                  {"doc": later["doc"]["ref"], "page": 1, "quote": (total_line(w, later) or {}).get("quote", "")}]
            if usd is None:
                ctx.items.append(_fx_missing(ctx, c, later, "duplicate", ev))
                continue
            it = item("duplicate", {"invoice_id": later["d"]["invoice_id"]}, "finding", amount=usd, action="draft:dispute", conf=0.95, ev=ev)
            it["_native"], it["_rec"] = amt, later
            ctx.add_disputed(key, amt)
            ctx.items.append(it)
            flagged.add(key)
    # (b) different invoice numbers billing the same line (AWB + Annex E code + amount)
    uniq = [sorted(r, key=lambda x: x["doc"]["received_ts"])[0] for r in groups.values()]
    uniq.sort(key=lambda r: ((r["d"].get("invoice_date") or ""), r["doc"]["received_ts"]))
    seen: dict[tuple, tuple[dict, dict]] = {}
    for rec in uniq:
        d = rec["d"]
        c = w.contract_of(rec)
        if not c:
            continue
        key = norm_inv(d["invoice_id"])
        dups, used = [], {}
        for ln in d["lines"]:
            if ln["kind"] != "charge":
                continue
            code = code_of(c, ln["desc"])
            ident = norm_awb(ln.get("awb") or "") or (ln.get("reference") and "ref:" + ln["reference"])
            if not code or not ident or code in ("TAX", "LATE"):
                continue
            k = (c.id, ident, ln.get("awb") if ln.get("awb") else "", code, str(money(ln["amount"])))
            # house AWBs of one master are different shipments, so the full AWB is compared
            if k in seen and seen[k][0]["d"]["invoice_id"] != d["invoice_id"] and k not in used:
                dups.append((ln, seen[k]))
                used[k] = True
            seen.setdefault(k, (rec, ln))
        if dups and key not in flagged:
            amt = sum((money(ln["amount"]) for ln, _ in dups), Decimal(0))
            origs = {norm_inv(o[0]["d"]["invoice_id"]) for _, o in dups}
            crs = credits_for(ctx, origs | {key})
            cred = credit_total(ctx, crs)
            amt2 = amt - cred
            if amt2 <= 0:
                continue
            ev = []
            for ln, (orec, oln) in dups[:3]:
                ev.append({"doc": orec["doc"]["ref"], "page": oln.get("page", 1), "quote": oln["raw"]})
                ev.append({"doc": rec["doc"]["ref"], "page": ln.get("page", 1), "quote": ln["raw"]})
            for cc in c.d.get("charge_codes", []):
                if cc["code"] in {code_of(c, ln["desc"]) for ln, _ in dups}:
                    ev.append({"doc": c.msa_doc["ref"], "page": cc["page"], "quote": cc["line"]})
            usd, fxr = w.to_usd(c, amt2, d.get("invoice_date"), d.get("currency", "USD"))
            if usd is None:
                ctx.items.append(_fx_missing(ctx, c, rec, "duplicate", ev))
                continue
            if fxr:
                ev.append({"doc": c.msa_doc["ref"], "page": fxr["page"], "quote": fxr["line"]})
            it = item("duplicate", {"invoice_id": d["invoice_id"]}, "finding", amount=usd, action="draft:dispute", conf=0.93, ev=ev)
            it["_native"], it["_rec"] = amt2, rec
            ctx.dup_lines.setdefault(key, set()).update(ln.get("idx") for ln, _ in dups)
            ctx.add_disputed(key, amt2)
            ctx.items.append(it)


# =====================================================================================================================
# overcharge / conflict / missing document
# =====================================================================================================================
def holder_of(c: Contract, category: str) -> dict:
    p = c.contact(category)
    return {"party": c.carrier_party(), "person": p["name"], "role": p["title"]} if p else {"party": c.carrier_party()}


def overcharges(ctx: Ctx):
    w, fb = ctx.w, ctx.fb
    groups: dict[str, list[dict]] = {}
    for rec in w.invoices:
        groups.setdefault(norm_inv(rec["d"]["invoice_id"]), []).append(rec)
    for key, recs in groups.items():
        rec = sorted(recs, key=lambda r: r["doc"]["received_ts"])[0]
        c = w.contract_of(rec)
        if not c:
            continue
        d = rec["d"]
        skip = set(ctx.waived_lines.get(key, set())) | set(ctx.dup_lines.get(key, set()))
        pr = price_invoice(w, rec, c, fb, exclude=skip)
        inv_cur = d.get("currency", "USD")
        if pr.missing and not pr.conflicts:
            _, name, act, adoc = pr.missing
            ev = [{"doc": adoc["ref"], "page": locate(w, adoc["ref"], act["text"][:60]) or 1, "quote": act["text"]}]
            it = item("overcharge", {"invoice_id": d["invoice_id"]}, "cannot_determine", action="draft:request_document", conf=0.9, ev=ev,
                      missing_doc={"name": name, "holder": holder_of(c, "rates")})
            it["_rec"], it["_contract"] = rec, c
            ctx.items.append(it)
            continue
        if pr.conflicts:
            lanes = {ln for ln, _ in pr.conflicts}
            pr_b = price_invoice(w, rec, c, fb, pick={ln: "alt" for ln in lanes}, exclude=skip)
            da, db = pr.diff, pr_b.diff
            lo_d, hi_d = min(da, db), max(da, db)
            if hi_d > 0 and da != db:
                _, res = pr.conflicts[0]
                ev = []
                for cand in (res.cand, res.alt):
                    for r in cand.rows[:1]:
                        pass
                # evidence: the differing rate rows of both instruments, the clause that leaves them unranked, the invoice line
                for l in pr.lines:
                    if l["why"] == "rate" and l["diff"] != 0 or l["why"] == "rate":
                        ev.append(l["ev"][0])
                        break
                for cand in (res.cand, res.alt):
                    for r in cand.rows:
                        ev.append(r["ev"])
                        break
                for l in pr.lines:
                    if l["why"] == "rate":
                        # the rate row of each side that applies to the billed weight
                        kg = float(l["line"]["qty"])
                        for cand in (res.cand, res.alt):
                            r = _row(cand, kg)
                            if r:
                                ev.append(r["ev"])
                        break
                ev += [clause_ev(c, "13.3"), clause_ev(c, "3.5")]
                lo = max(Decimal(0), lo_d)
                hi = hi_d
                lo_u, _ = w.to_usd(c, lo, d.get("invoice_date"), inv_cur)
                hi_u, _ = w.to_usd(c, hi, d.get("invoice_date"), inv_cur)
                if lo_u is None or hi_u is None:
                    continue
                it = item("overcharge", {"invoice_id": d["invoice_id"]}, "conflict", rng=(lo_u, hi_u), action="escalate", conf=0.9, ev=ev)
                it["_rec"], it["_contract"], it["_lanes"] = rec, c, pr.lanes
                ctx.items.append(it)
            continue
        if pr.diff <= 0:
            continue
        # a finding. Feedback may close/reject/override it.
        closing = fb.decision_for(d["invoice_id"], "overcharge", ("close", "reject"))
        if closing:
            continue
        diff = pr.diff
        ev = []
        for l in pr.lines:
            if l["diff"] > 0:
                ev += l["ev"]
        usd, fxr = w.to_usd(c, diff, d.get("invoice_date"), inv_cur)
        if usd is None:
            ctx.items.append(_fx_missing(ctx, c, rec, "overcharge", ev))
            continue
        if fxr:
            ev.append({"doc": c.msa_doc["ref"], "page": fxr["page"], "quote": fxr["line"]})
        learned = pr.learned
        ov = fb.decision_for(d["invoice_id"], "overcharge", ("override_amount",))
        if ov:
            try:
                usd = money(Decimal(str((ov["fact"].get("value") or {})["amount_usd"])))
                learned = ov
            except Exception:
                pass
        it = item("overcharge", {"invoice_id": d["invoice_id"]}, "finding", amount=usd, action="draft:dispute", conf=0.95, ev=ev)
        it["_native"], it["_rec"], it["_contract"], it["_tier"], it["_lanes"] = diff, rec, c, pr.min_tier, pr.lanes
        it["_authority_ok"] = pr.authority_ok
        if learned:
            it["learned_at"] = Feedback.learned(learned)
        ctx.add_disputed(key, diff)
        ctx.items.append(it)


def _row(cand, kg):
    from pricing import row_for_weight
    return row_for_weight(cand.rows, kg)


# =====================================================================================================================
# late fees
# =====================================================================================================================
def effective_due(ctx: Ctx, c: Contract, key: str, ledger_due: str | None):
    """-> (due date, extension evidence | None)."""
    due = pdate(ledger_due)
    ext_ev = None
    best = None
    for x in ctx.w.email_facts:
        if x["type"] != "extension" or x.get("quoted") or norm_inv(x.get("invoice") or "") != key:
            continue
        p = c.person_by_email(x["doc"]["from_addr"])
        ok = bool(p and "payment_terms" in p["categories"] and p["party"] == c.carrier_party())
        if not ok and p:
            ok = bool(ctx.fb.authority(p["name"], "payment_terms", c.id, pdate(x["doc"]["received"])))
        if not ok:
            continue
        if best is None or x["doc"]["received_ts"] > best["doc"]["received_ts"]:
            best = x
    if best:
        due = pdate(best["new_due"]) or due
        ext_ev = {"doc": best["doc"]["ref"], "page": 1, "quote": best["quote"]}
    return due, ext_ev


def fee_months(days_late: int, grace: int) -> int:
    return 0 if days_late <= grace else math.ceil(days_late / 30)


def late_fee_schedule(principal: Decimal, pct, n: int, compounds: bool) -> list[Decimal]:
    fees, base = [], principal
    for _ in range(n):
        f = money(Decimal(str(pct)) / 100 * base)
        fees.append(f)
        base = base + f if compounds else principal
    return fees


def late_fees_not_owed(ctx: Ctx):
    w = ctx.w
    groups: dict[str, list[dict]] = {}
    for rec in w.invoices:
        groups.setdefault(norm_inv(rec["d"]["invoice_id"]), []).append(rec)
    for key, recs in groups.items():
        rec = sorted(recs, key=lambda r: r["doc"]["received_ts"])[0]
        c = w.contract_of(rec)
        d = rec["d"]
        if not c:
            continue
        by_target: dict[str, list[dict]] = {}
        for ln in d["lines"]:
            if ln["kind"] == "late_fee" and ln.get("late_fee"):
                by_target.setdefault(norm_inv(ln["late_fee"]["invoice_ref"]), []).append(ln)
        total_diff = Decimal(0)
        ev = []
        for tkey, lns in by_target.items():
            row = w.ledger.get(tkey)
            if not row or row["status"] != "paid" or not row.get("paid_date"):
                continue
            due, ext_ev = effective_due(ctx, c, tkey, row["due_date"])
            paid = pdate(row["paid_date"])
            grace = c.term("late_fee_grace_days", 0)
            n = fee_months((paid - due).days, grace)
            principal = money(row["amount"]) - ctx.disputed.get(tkey, Decimal(0))
            pct = c.term("late_fee_pct_per_month")
            comp = c.term("late_fee_compounds", True)
            if pct is None:
                continue
            fees = late_fee_schedule(principal, pct, n, bool(comp))
            diff_t = Decimal(0)
            for ln in lns:
                m = ln["late_fee"].get("month_no") or 1
                corr = fees[m - 1] if 1 <= m <= len(fees) else Decimal(0)
                diff_t += money(ln["amount"]) - corr
            if diff_t > 0:
                total_diff += diff_t
                for ln in lns:
                    ev.append({"doc": rec["doc"]["ref"], "page": ln.get("page", 1), "quote": ln["raw"]})
                ev.append({"doc": row["doc"]["ref"], "page": row.get("page", 1), "quote": row["line"]})
                if ext_ev:
                    ev.append(ext_ev)
                for num in ("10.2", "10.3", "10.5" if ext_ev else "10.4"):
                    ev.append(clause_ev(c, num))
        if total_diff > 0:
            closing = ctx.fb.decision_for(d["invoice_id"], "late_fee_not_owed", ("close", "reject"))
            if closing:
                continue
            usd, fxr = w.to_usd(c, total_diff, d.get("invoice_date"), d.get("currency", "USD"))
            if usd is None:
                ctx.items.append(_fx_missing(ctx, c, rec, "late_fee_not_owed", ev))
                continue
            if fxr:
                ev.append({"doc": c.msa_doc["ref"], "page": fxr["page"], "quote": fxr["line"]})
            it = item("late_fee_not_owed", {"invoice_id": d["invoice_id"]}, "finding", amount=usd, action="draft:dispute", conf=0.93, ev=ev)
            it["_native"], it["_rec"] = total_diff, rec
            ctx.items.append(it)


# =====================================================================================================================
# portfolio: late-fee risk, cashback, expiry
# =====================================================================================================================
def late_fee_risk(ctx: Ctx):
    w = ctx.w
    byk = {}
    for rec in w.invoices:
        byk.setdefault(norm_inv(rec["d"]["invoice_id"]), rec)
    for key, row in w.ledger.items():
        if row["status"] == "paid" or key not in byk:
            continue
        rec = byk[key]
        c = w.contract_of(rec)
        if not c:
            continue
        due, ext_ev = effective_due(ctx, c, key, row["due_date"])
        if due is None:
            continue
        gap = (due - w.asd).days
        if not (0 < gap <= 14):
            continue
        pct = c.term("late_fee_pct_per_month")
        if pct is None:
            continue
        principal = money(row["amount"]) - ctx.disputed.get(key, Decimal(0))
        if principal <= 0:
            continue
        fee = money(Decimal(str(pct)) / 100 * principal)
        usd, fxr = w.to_usd(c, fee, rec["d"].get("invoice_date"), row.get("currency") or rec["d"].get("currency", "USD"))
        ev = [{"doc": row["doc"]["ref"], "page": row.get("page", 1), "quote": row["line"]}, clause_ev(c, "10.2")]
        if ext_ev:
            ev.append(ext_ev)
        if usd is None:
            ctx.portfolio["late_fee_risk"].append(_fx_missing(ctx, c, rec, "late_fee_risk", ev))
            continue
        if fxr:
            ev.append({"doc": c.msa_doc["ref"], "page": fxr["page"], "quote": fxr["line"]})
        it = item("late_fee_risk", {"invoice_id": rec["d"]["invoice_id"]}, "finding", amount=usd, action="internal:payment_reminder",
                  conf=0.92, ev=ev, deadline=due.isoformat())
        it["_rec"] = rec
        ctx.portfolio["late_fee_risk"].append(it)


def quarter_of(d):
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def quarter_end(q: str):
    y, n = int(q[:4]), int(q[-1])
    from datetime import date
    m = n * 3
    nxt = date(y + (m // 12), m % 12 + 1, 1)
    from datetime import timedelta
    return nxt - timedelta(days=1)


NON_QUALIFYING = re.compile(r"\b(tax|gst|vat|duty|duties|disbursement|late|interest|rebate|improvement|credit)\b", re.I)


def qualifying_native(c: Contract, d: dict) -> Decimal:
    tot = Decimal(0)
    for ln in d["lines"]:
        if ln["kind"] != "charge":
            continue
        code = code_of(c, ln["desc"])
        if code in ("TAX", "DUTY", "LATE"):
            continue
        if code is None and NON_QUALIFYING.search(ln["desc"] or ""):
            continue
        tot += money(ln["amount"])
    return tot


def credit_kind(cr: dict) -> str | None:
    t = ((cr["d"].get("credit") or {}).get("type") or "").lower()
    if "rebate" in t or "volume" in t:
        return "rebate"
    if "improv" in t or "annual" in t or "promis" in t:
        return "promised_credit"
    if "margin" in t or "step" in t:
        return "margin_step_down"
    return t or None


def credit_period(cr: dict) -> str | None:
    p = (cr["d"].get("credit") or {}).get("period")
    return p.upper() if p else None


def spend(ctx: Ctx, c: Contract, start, end):
    """Qualifying spend in USD over invoices dated start..end, net of credit notes dated in the window. -> (USD, evidence, missing_fx)."""
    w = ctx.w
    tot = Decimal(0)
    ev = []
    seen = set()
    for rec in w.invoices:
        d = rec["d"]
        if w.contract_of(rec) is not c:
            continue
        idate = pdate(d.get("invoice_date"))
        if not idate or not (start <= idate <= end):
            continue
        key = (norm_inv(d["invoice_id"]), rec["doc"]["ref"])
        if norm_inv(d["invoice_id"]) in seen:
            continue                        # a resend is not a second bill; duplicates stay in until a credit note reverses them only when distinct numbers
        seen.add(norm_inv(d["invoice_id"]))
        nat = qualifying_native(c, d)
        usd, fxr = w.to_usd(c, nat, d.get("invoice_date"), d.get("currency", "USD"))
        if usd is None:
            return None, ev, ("fx", rec)
        tot += usd
        tl = total_line(w, rec)
        if tl and len(ev) < 6:
            ev.append(tl)
        if fxr:
            ev.append({"doc": c.msa_doc["ref"], "page": fxr["page"], "quote": fxr["line"]})
    for cr in w.credit_notes:
        if w.contract_of(cr) is not c:
            continue
        if credit_kind(cr) in ("rebate", "promised_credit", "margin_step_down"):
            continue
        cd = pdate(cr["d"].get("invoice_date"))
        if not cd or not (start <= cd <= end):
            continue
        amt = money(abs(cr["d"].get("total") or 0)) - money(abs(cr["d"].get("tax_total") or 0))
        usd, _ = w.to_usd(c, amt, cr["d"].get("invoice_date"), cr["d"].get("currency", "USD"))
        if usd is None:
            return None, ev, ("fx", cr)
        tot -= usd
        tl = total_line(w, cr)
        if tl:
            ev.append(tl)
    return tot, ev, None


def credits_held(ctx: Ctx, c: Contract, kind: str, period: str) -> tuple[Decimal, list]:
    tot, ev = Decimal(0), []
    for cr in ctx.w.credit_notes:
        if ctx.w.contract_of(cr) is not c or credit_kind(cr) != kind or credit_period(cr) != period:
            continue
        usd, _ = ctx.w.to_usd(c, money(abs(cr["d"].get("total") or 0)), cr["d"].get("invoice_date"), cr["d"].get("currency", "USD"))
        if usd is not None:
            tot += usd
            tl = total_line(ctx.w, cr)
            if tl:
                ev.append(tl)
    return tot, ev


def rebate_amount(c: Contract, qs: Decimal) -> Decimal:
    tiers = c.term("rebate_tiers")
    if not tiers:
        return Decimal(0)
    basis = c.term("rebate_basis", "all")
    if basis == "marginal":
        total, prev = Decimal(0), Decimal(0)
        rate = Decimal(0)
        for thr, pct in tiers:
            thr = Decimal(str(thr))
            if qs > thr:
                pass
        # marginal: each slice above a threshold earns that tier's rate
        tl = [(Decimal(str(t)), Decimal(str(p))) for t, p in tiers]
        for i, (thr, pct) in enumerate(tl):
            if qs < thr:
                break
            upper = tl[i + 1][0] if i + 1 < len(tl) else None
            top = min(qs, upper) if upper is not None else qs
            total += (top - thr) * pct / 100
        return money(total)
    pct = Decimal(0)
    for thr, p in tiers:
        if qs >= Decimal(str(thr)):
            pct = Decimal(str(p))
    return money(qs * pct / 100)


def cashback(ctx: Ctx):
    w = ctx.w
    for c in w.contracts.values():
        eff = pdate(c.term("effective_date"))
        dates = [pdate(r["d"].get("invoice_date")) for r in w.invoices if w.contract_of(r) is c]
        dates = [d for d in dates if d]
        if not dates:
            continue
        # ---- quarterly volume rebate
        qs_set = sorted({quarter_of(d) for d in dates})
        due_days = c.term("rebate_due_days")
        for q in qs_set:
            qe = quarter_end(q)
            if not (w.asd > qe):
                continue
            from datetime import date
            qstart = date(int(q[:4]), (int(q[-1]) - 1) * 3 + 1, 1)
            qs, ev, miss = spend(ctx, c, qstart, qe)
            if miss:
                continue
            if qs is None or not c.term("rebate_tiers"):
                continue
            amt = rebate_amount(c, qs)
            held, hev = credits_held(ctx, c, "rebate", q)
            owed = amt - held
            if owed <= 0:
                continue
            ev2 = [clause_ev(c, "7.1", 400), clause_ev(c, "1.1", 500)] + ev
            _emit_cashback(ctx, c, q, "rebate", owed, qe, due_days, ev2)
        # ---- annual improvement / promised credit by contract year
        pct, days = c.term("annual_credit_pct"), c.term("annual_credit_days")
        if pct and eff:
            n = 1
            while True:
                start = add_months(eff, 12 * (n - 1))
                end = add_months(eff, 12 * n) - __import__("datetime").timedelta(days=1)
                if not (w.asd > end) or start > max(dates):
                    break
                qs, ev, miss = spend(ctx, c, start, end)
                if qs is not None and not miss:
                    amt = money(qs * Decimal(str(pct)) / 100)
                    held, _ = credits_held(ctx, c, "promised_credit", f"CY{n}")
                    owed = amt - held
                    if owed > 0:
                        ev2 = [clause_ev(c, "9.1", 400), clause_ev(c, "1.1", 500), clause_ev(c, "1.4")] + ev
                        _emit_cashback(ctx, c, f"CY{n}", "promised_credit", owed, end, days, ev2)
                n += 1


def _emit_cashback(ctx: Ctx, c: Contract, period: str, ctype: str, owed: Decimal, period_end, due_days, ev):
    from datetime import timedelta
    due = period_end + timedelta(days=due_days) if due_days is not None else None
    is_due = due is not None and ctx.w.asd > due
    action = "draft:claim_credit" if is_due else "internal:track_credit"
    it = item("cashback", {"contract_id": c.id, "period": period, "credit_type": ctype}, "finding", amount=owed, action=action, conf=0.9, ev=ev,
              deadline=due.isoformat() if (due and not is_due) else None)
    it["_contract"] = c
    ctx.portfolio["cashback"].append(it)


def expiry(ctx: Ctx):
    w = ctx.w
    for c in w.contracts.values():
        end = pdate(c.term("term_end"))
        if not end:
            continue
        ev = [clause_ev(c, "2.2")]
        # signed extensions
        for doc, f in c.amendments:
            for act in f["data"].get("actions", []):
                if act["type"] == "term_extension" and pdate(act["new_end"]) and pdate(act["new_end"]) > end:
                    end = pdate(act["new_end"])
                    ev = [{"doc": doc["ref"], "page": locate(w, doc["ref"], act["text"][:60]) or 1, "quote": act["text"]}]
        for x in w.email_facts:
            if x["type"] == "contract_extension" and x["contract"] == c.id and not x.get("quoted"):
                p = c.person_by_email(x["doc"]["from_addr"])
                if p and pdate(x["new_end"]) and pdate(x["new_end"]) > end:
                    end = pdate(x["new_end"])
                    ev = [{"doc": x["doc"]["ref"], "page": 1, "quote": x["quote"]}]
        deadline = end
        auto = c.term("auto_renews")
        t23 = (c.clause("2.3") or {}).get("text", "")
        if auto is None and re.search(r"renews? automatically|automatically renews?|renew(?:s|ed)? for (?:successive|further)", t23, re.I) \
                and not re.search(r"does not (?:renew|continue)", t23, re.I):
            m = re.search(r"(\d+)\s*days", t23)
            if m:
                from datetime import timedelta
                deadline = end - timedelta(days=int(m.group(1)))
                ev = [clause_ev(c, "2.3", 400), clause_ev(c, "2.2")] + ev
        gap = (deadline - w.asd).days
        if 0 <= gap <= 60:
            it = item("expiry", {"contract_id": c.id}, "finding", action="internal:alert", conf=0.95, ev=ev, deadline=deadline.isoformat())
            ctx.portfolio["expiry"].append(it)


# =====================================================================================================================
# actions, confidence, supersedes, report
# =====================================================================================================================
def _recipient_ok(c: Contract | None, category: str) -> bool:
    return bool(c and c.contact(category))


def assign_actions(ctx: Ctx):
    mode = ctx.mode
    conflicts = [it for it in ctx.items if it["verdict"] == "conflict"]
    conflict_lanes = {(it["_contract"].id, ln, per) for it in conflicts for ln, per in it.get("_lanes", set())}
    per_invoice: dict[str, Decimal] = {}
    for it in ctx.items:
        if it["verdict"] == "finding" and it["action"].startswith("draft:"):
            per_invoice[norm_inv(it["invoice_id"])] = per_invoice.get(norm_inv(it["invoice_id"]), Decimal(0)) + Decimal(it["amount_usd"])
    allitems = ctx.items + ctx.portfolio["cashback"] + ctx.portfolio["late_fee_risk"] + ctx.portfolio["expiry"]
    for it in allitems:
        if it["verdict"] == "conflict":
            it["action"] = "escalate"
            continue
        if not it["action"].startswith("draft:"):
            continue
        c = it.get("_contract") or (ctx.w.contract_of(it["_rec"]) if it.get("_rec") else None)
        if it["verdict"] == "finding":
            total = per_invoice.get(norm_inv(it["invoice_id"]), Decimal(0)) if it["class"] != "cashback" else Decimal(it["amount_usd"])
            if total >= THRESHOLD:
                it["action"] = "human_review"
                continue
        if mode == "autonomous":
            ok = True
            if it["class"] == "overcharge" and it["verdict"] == "finding":
                ok &= it.get("_tier", 1) <= 2
                ok &= it.get("_authority_ok", True)
                ok &= not any((c.id, ln, per) in conflict_lanes for ln, per in it.get("_lanes", set()))
            category = "billing_disputes"
            ok &= _recipient_ok(c, category)
            if not ok:
                it["action"] = "human_review"
            else:
                it["action"] = it["action"].replace("draft:", "execute:")
        # supervised: stays draft:*
    for it in allitems:
        assert not (mode == "supervised" and it["action"].startswith("execute:"))


def set_confidence(ctx: Ctx):
    for it in ctx.items + ctx.portfolio["cashback"] + ctx.portfolio["late_fee_risk"] + ctx.portfolio["expiry"]:
        c = it["confidence"]
        tier = it.get("_tier", 1)
        if tier >= 3:
            c -= 0.08 if tier == 3 else 0.15
        rec = it.get("_rec")
        if rec and rec["doc"].get("scan"):
            c -= 0.07
        if it["verdict"] == "finding" and it["class"] == "overcharge" and it.get("_lanes") is not None and not it["evidence"]:
            c -= 0.2
        it["confidence"] = round(max(0.3, min(0.99, c)), 2)


def previous_report(state_dir: str, as_of: str):
    d = Path(state_dir) / "reports"
    best = None
    if d.exists():
        for f in d.glob("*.json"):
            try:
                r = json.loads(f.read_text())
            except Exception:
                continue
            if r.get("as_of", "9999") < as_of and (best is None or r["as_of"] > best["as_of"]):
                best = r
    return best


def all_report_items(rep: dict) -> list[dict]:
    p = rep.get("portfolio", {})
    return list(rep.get("findings", [])) + p.get("cashback", []) + p.get("expiry", []) + p.get("late_fee_risk", [])


def link_history(ctx: Ctx, prev: dict | None):
    if not prev:
        return
    old = {it["id"]: it for it in all_report_items(prev)}
    for it in ctx.items + ctx.portfolio["cashback"] + ctx.portfolio["late_fee_risk"]:
        o = old.get(it["id"])
        if o:
            if (o["verdict"], o.get("amount_usd"), o.get("amount_range")) != (it["verdict"], it.get("amount_usd"), it.get("amount_range")):
                it["supersedes"] = [o["id"]]
        elif it.get("_rec") and it["class"] in ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed") and not it.get("learned_at"):
            if it["_rec"]["doc"]["received"] <= prev["as_of"]:
                it["retro"] = True


def strip(it: dict) -> dict:
    out = {k: v for k, v in it.items() if not k.startswith("_")}
    return out


def build_report(state_dir: str, as_of: str, mode: str = "supervised") -> dict:
    w = World(state_dir, as_of)
    fb = Feedback(store.load_feedback(state_dir), as_of)
    ctx = Ctx(w, fb, mode)
    waivers(ctx)
    duplicates(ctx)
    overcharges(ctx)
    late_fees_not_owed(ctx)
    late_fee_risk(ctx)
    cashback(ctx)
    expiry(ctx)
    # feedback can close or reject any invoice key
    keep = []
    for it in ctx.items:
        if it["verdict"] in ("finding", "conflict", "cannot_determine") and it["class"] in ("waived_but_billed", "duplicate", "late_fee_not_owed", "overcharge"):
            drop = fb.decision_for(it["invoice_id"], it["class"], ("close", "reject"))
            if drop and not (it["class"] == "waived_but_billed" and drop["fact"].get("type") == "waiver_scope" and drop["decision"] != "reject"):
                continue
        keep.append(it)
    ctx.items = keep
    assign_actions(ctx)
    set_confidence(ctx)
    for it in ctx.items + ctx.portfolio["cashback"] + ctx.portfolio["late_fee_risk"] + ctx.portfolio["expiry"]:
        it["evidence"] = clean_ev(w, it["evidence"])
    link_history(ctx, previous_report(state_dir, as_of))
    rep = {"schema_version": "1.0", "as_of": as_of, "mode": mode,
           "findings": [strip(i) for i in ctx.items],
           "portfolio": {k: [strip(i) for i in v] for k, v in ctx.portfolio.items()}}
    d = Path(state_dir) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{as_of}_{mode}.json").write_text(json.dumps(rep, indent=1))
    return rep

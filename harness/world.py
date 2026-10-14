"""The world model: everything the harness holds on a given as_of date, organised for the rules in engine.py.

Nothing here calls a model. All inputs are the structured facts written by understand.py.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import store

CENT = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def d2s(d: Decimal) -> str:
    return f"{money(d):.2f}"


def pdate(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def norm_awb(a: str) -> str:
    return re.sub(r"-H\d+$", "", a or "")


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


class Contract:
    def __init__(self, cid: str):
        self.id = cid
        self.msa: dict | None = None          # fact record
        self.msa_doc: dict | None = None
        self.amendments: list[tuple[dict, dict]] = []   # (doc, fact record)
        self.annexes: list[tuple[dict, dict]] = []

    @property
    def d(self) -> dict:
        return self.msa["data"]

    @property
    def terms(self) -> dict:
        return self.d["terms"]

    def term(self, name, default=None):
        t = self.terms.get(name)
        return t["value"] if t else default

    def clause(self, num: str):
        c = self.d["clauses"].get(num)
        return c

    def schedule1(self) -> list[dict]:
        return self.d.get("schedule1") or []

    def person_by_email(self, addr: str):
        a = (addr or "").strip().lower()
        for p in self.schedule1():
            if p.get("email", "").lower() == a:
                return p
        return None

    def carrier_party(self) -> str | None:
        for p in self.schedule1():
            if set(p["categories"]) & {"rates", "waivers", "treasury"}:
                return p["party"]
        return None

    def contact(self, category: str, party: str | None = None):
        party = party or self.carrier_party()
        for p in self.schedule1():
            if category in p["categories"] and (party is None or p["party"] == party):
                return p
        return None

    def kam(self):
        return self.contact("notices")

    @property
    def currency(self) -> str:
        return self.d.get("currency") or "USD"


class World:
    def __init__(self, state_dir: str, as_of: str):
        self.state_dir = state_dir
        self.as_of = as_of
        self.asd = pdate(as_of)
        self.docs = store.load_docs(state_dir, as_of)
        facts = store.load_facts(state_dir)
        self.by_ref = {d["ref"]: d for d in self.docs}
        self.facts = {r: f for r, f in facts.items() if r in self.by_ref}
        self.feedback = [fb for fb in store.load_feedback(state_dir)]
        self.contracts: dict[str, Contract] = {}
        self._load_contracts()
        self.invoices: list[dict] = []            # invoice / statement / credit-note fact records with their doc
        self.credit_notes: list[dict] = []
        self.tracking: dict[str, list[dict]] = {}
        self.awbs: dict[str, dict] = {}
        self.dgds: dict[str, dict] = {}
        self.registers: list[dict] = []
        self.email_facts: list[dict] = []
        self._load_rest()
        self.ledger = self._ledger()

    # --------------------------------------------------------------------------------------------------------
    def _load_contracts(self):
        for r, f in self.facts.items():
            doc = self.by_ref[r]
            if f["kind"] == "msa":
                cid = f["data"].get("contract_id")
                if not cid:
                    continue
                c = self.contracts.setdefault(cid, Contract(cid))
                if c.msa is None or doc["received"] < c.msa_doc["received"]:
                    c.msa, c.msa_doc = f, doc
        for r, f in self.facts.items():
            doc = self.by_ref[r]
            if f["kind"] in ("amendment", "annex"):
                cid = f["data"].get("contract_id")
                c = self.contracts.get(cid)
                if c is None:
                    continue
                (c.amendments if f["kind"] == "amendment" else c.annexes).append((doc, f))
        for c in list(self.contracts.values()):
            if c.msa is None:
                del self.contracts[c.id]

    def _load_rest(self):
        for r, f in self.facts.items():
            doc = self.by_ref[r]
            k, d = f["kind"], f["data"]
            if k in ("invoice", "statement", "credit_note"):
                rec = {"doc": doc, "fact": f, "d": d}
                (self.credit_notes if (k == "credit_note" or d.get("kind") == "credit_note" or d.get("credit")) else self.invoices).append(rec)
            elif k == "tracking":
                for e in d["events"]:
                    self.tracking.setdefault(e["awb"], []).append({**e, "doc": doc})
            elif k == "awb" and d:
                self.awbs[d["awb"]] = {**d, "doc": doc}
            elif k == "dgd" and d:
                self.dgds[d["awb"]] = {**d, "doc": doc}
            elif k == "ap_register":
                self.registers.append({"doc": doc, "rows": d["rows"]})
            elif k == "email":
                for x in d.get("facts", []):
                    self.email_facts.append({**x, "doc": doc})
        self.invoices.sort(key=lambda r: (r["d"].get("invoice_date") or "", r["doc"]["ref"]))
        self.registers.sort(key=lambda r: r["doc"]["received_ts"])

    def _ledger(self) -> dict[str, dict]:
        """Latest known ledger row per invoice number (a later register wins)."""
        out: dict[str, dict] = {}
        for reg in self.registers:
            for row in reg["rows"]:
                out[norm_inv(row["invoice_no"])] = {**row, "doc": reg["doc"]}
        return out

    # --------------------------------------------------------------------------------------------------------
    def contract_of(self, rec) -> Contract | None:
        ref = rec["d"].get("contract_ref")
        if ref and ref in self.contracts:
            return self.contracts[ref]
        if ref:
            for cid, c in self.contracts.items():
                if ref in cid or cid in ref:
                    return c
        cands = [c for c in self.contracts.values() if c.currency == rec["d"].get("currency")]
        return cands[0] if len(cands) == 1 else (list(self.contracts.values())[0] if len(self.contracts) == 1 else None)

    def pickup(self, awb: str, inv_line_date: str | None):
        """-> (date, tier, evidence) with tier 1=tracking, 2=carrier email, 3=AWB, 4=invoice."""
        base = norm_awb(awb)
        for key in (awb, base):
            for e in self.tracking.get(key, []):
                if e["event"] == "PICKED_UP":
                    return pdate(e["date"]), 1, {"doc": e["doc"]["ref"], "page": e["page"], "quote": e["line"]}
        for x in self.email_facts:
            if x["type"] == "event" and x["event"] == "PICKED_UP" and norm_awb(x["awb"]) == base and not x.get("quoted"):
                return pdate(x["date"]), 2, {"doc": x["doc"]["ref"], "page": 1, "quote": x["quote"]}
        for key in (awb, base):
            a = self.awbs.get(key)
            if a and a.get("executed"):
                return pdate(a["executed"]), 3, None
        return pdate(inv_line_date), 4, None

    def events(self, awb: str) -> list[dict]:
        out = list(self.tracking.get(awb, [])) or list(self.tracking.get(norm_awb(awb), []))
        return sorted(out, key=lambda e: e["date"])

    def fx(self, c: Contract, invoice_date: str):
        month = (invoice_date or "")[:7]
        for r in c.d.get("fx") or []:
            if r["month"] == month:
                return r
        return None

    def to_usd(self, c: Contract, amount: Decimal, invoice_date: str, currency: str):
        """-> (Decimal usd | None, fx row | None). None when a needed rate is missing."""
        if currency == "USD":
            return money(amount), None
        r = self.fx(c, invoice_date)
        if not r:
            return None, None
        return money(amount / Decimal(str(r["inr_per_usd"]))), r


def norm_inv(s: str) -> str:
    return re.sub(r"[\s#:]", "", (s or "").upper())


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def locate(w: "World", ref: str, quote: str) -> int | None:
    """Page (1-based) of a document that holds the quote (spacing ignored), or None."""
    doc = w.by_ref.get(ref)
    if not doc:
        return None
    q = squash(quote)
    for i, p in enumerate(doc["pages"], 1):
        if q and q in squash(p):
            return i
    return None

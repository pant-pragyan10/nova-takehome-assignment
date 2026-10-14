"""Emails -> facts. Deterministic first; the model (understand.py) only adds what these patterns miss.

Patterns key on *meaning cues* (a waive-family word with no negation, a due-date cue plus an invoice token plus a date,
a charge label from the contract's own Annex E) rather than on one fixed sentence shape, so paraphrases parse.

Each fact carries the exact sentence it came from (`quote`), because evidence must be copied from the email, and
`quoted: True` when it sits inside a reply/forward block: text quoted from someone else carries no authority of its own.
Lines addressed to an AI / auditor / 'automated system' are recorded as `instruction` facts and never acted on.
"""
from __future__ import annotations

import re

DATE = r"\d{4}-\d{2}-\d{2}"
AWB = r"[A-Z]{2}\d{8}(?:-H\d+)?"
INVID = r"[A-Z]{2,4}[-/][A-Z0-9][A-Z0-9/-]*\d[A-Z0-9/-]*"
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

DEFAULT_VOCAB = [
    "delivery fee", "door delivery", "final-mile delivery", "storage programme fee", "storage programme", "storage",
    "warehouse storage", "demurrage", "customs hold fee", "customs examination", "hold handling", "security surcharge",
    "screening fee", "security screening", "dangerous goods fee", "dg handling", "hazmat handling", "dry ice handling fee",
    "dry ice handling", "peak season surcharge", "monthly account fee", "account maintenance", "pickup programme fee",
    "pickup programme", "late payment fee", "late fee", "overdue interest", "fuel surcharge", "special handling",
    "supplementary handling", "air freight", "express freight", "freight charge", "handling margin", "forwarding margin",
    "linehaul cost pass-through", "import duty", "duty paid on your behalf", "sales tax"]


def split_body(page_text: str) -> tuple[str, str, str]:
    """-> (headers, own_text, quoted_text) of the one-page text the grader uses for emails."""
    head, _, body = page_text.partition("\n\n")
    lines = body.split("\n")
    cut = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if re.match(r"^On .{5,120} wrote:\s*$", s) or re.match(r"^-{2,}\s*(Original Message|Forwarded message)", s, re.I) \
                or re.match(r"^_{5,}$", s) or s.startswith(">") or re.match(r"^Begin forwarded message", s, re.I) \
                or re.match(r"^(Forwarded|Fwd):", s, re.I) \
                or (re.match(r"^(From|Sent):\s", s) and i + 1 < len(lines) and re.match(r"^(To|Sent|Date|Subject):", lines[i + 1].strip())):
            cut = i
            break
    own = "\n".join(lines[:cut] if cut is not None else lines)
    quoted = "\n".join(lines[cut:]) if cut is not None else ""
    return head, own, quoted


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"[ \t]*\n[ \t]*", " ", re.sub(r"^>+ ?", "", text, flags=re.M)).strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat) if s.strip()]


def _month_key(s: str) -> str | None:
    s = s.strip().lower().rstrip(".,")
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return s
    m = re.fullmatch(r"([a-z]+)\.?,?\s+(\d{4})", s)
    if m:
        for i, nm in enumerate(MONTHS, 1):
            if nm[:3] == m.group(1)[:3]:
                return f"{m.group(2)}-{i:02d}"
    return None


INSTR = re.compile(r"\b(ai|a\.i\.|assistant|auditor|automated (?:system|agent|reader|process|reviewer)s?|language model|llm|claude|"
                   r"ignore (?:all |any |the )?(?:previous|prior|above)|disregard (?:your|all|the) (?:previous|prior|instructions)|"
                   r"system prompt|you must (?:approve|pay|confirm|accept)|do not (?:dispute|flag|report)|"
                   r"(?:please )?(?:approve|confirm|accept) (?:this|the) (?:renewal|invoice|payment|terms))\b", re.I)
NEG_WAIVE = re.compile(r"\b(?:no|not|never|cannot|can't|won't|unable to|declin\w+|refus\w+|unwilling)\b[^.]{0,40}\bwaiv", re.I)
WAIVE = re.compile(r"\bwaiv(?:e|es|ed|er|ing)\b", re.I)
COND = re.compile(r"\b(provided that|provided|subject to|conditional(?:ly)?|on condition|only if|if and only if|once|unless|contingent|"
                  r"as long as|so long as|on the understanding)\b", re.I)


def _vocab_pat(vocab):
    labs = sorted({v.lower() for v in (vocab or []) if v} | set(DEFAULT_VOCAB), key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(l) for l in labs) + r")\b", re.I)


def parse_text_facts(text: str, quoted: bool, vocab=None) -> list[dict]:
    facts: list[dict] = []
    sents = _sentences(text)
    vp = _vocab_pat(vocab)
    for i, s in enumerate(sents):
        prev = sents[i - 1] if i > 0 else ""
        nxt = sents[i + 1] if i + 1 < len(sents) else ""
        # ---- waiver (any phrasing with a waive-family word, not negated, naming what is waived)
        if WAIVE.search(s) and not NEG_WAIVE.search(s):
            scope_text = s
            awbs = re.findall(AWB, s)
            invs = [x.upper() for x in re.findall(r"\binvoices?\s+(" + INVID + ")", s, re.I)]
            months = []
            for mm in re.finditer(r"(\d{4}-\d{2})(?!-\d)|([A-Za-z]{3,9}\.?,? \d{4})", s):
                k = _month_key(mm.group(0))
                if k:
                    months.append(k)
            if not (awbs or invs or months):          # scope named in the neighbouring sentence of the same email
                for nb in (nxt, prev):
                    a2 = re.findall(AWB, nb)
                    if a2 and not WAIVE.search(nb):
                        awbs, scope_text = a2, nb
                        break
            cm = vp.search(s) or vp.search(prev) or vp.search(nxt)
            if (awbs or invs or months) and cm:
                cond = COND.search(s) or (COND.search(nxt) if nxt and not WAIVE.search(nxt) else None)
                facts.append({"type": "waiver", "charge": cm.group(1), "awbs": awbs, "invoices": invs, "months": months,
                              "conditional": bool(cond),
                              "condition_text": (s if COND.search(s) else nxt) if cond else None,
                              "quote": s, "scope_quote": scope_text if scope_text != s else None, "quoted": quoted})
        # ---- due-date extension
        if re.search(r"\bdue\b", s, re.I) and not re.search(r"\boverdue\b", s, re.I):
            ids = re.findall(r"\binvoices?\s+(" + INVID + ")", s, re.I)
            dates = re.findall(DATE, s)
            if ids and dates and re.search(r"\b(extend\w*|falls? due|now due|is now due|will be due|revised|moved|postpone\w*|new due date|due date)\b", s, re.I):
                after = re.search(r"(?:due(?: date)?(?: of [^.]*?)?(?: is| to| on| by)?|extend\w*[^.]*? to)\s+(" + DATE + ")", s, re.I)
                facts.append({"type": "extension", "invoice": ids[0].upper(), "new_due": after.group(1) if after else dates[-1],
                              "quote": s, "quoted": quoted})
        # ---- shipment events
        awb_m = re.search(AWB, s)
        if awb_m and re.search(DATE, s):
            d = re.search(DATE, s).group(0)
            stn = re.search(r"\bat\s+([A-Z]{3,4}\d?)\b", s)
            if re.search(r"picked up|collected from (?:the )?shipper|pickup completed", s, re.I) and not re.search(r"collected by the consignee", s, re.I):
                facts.append({"type": "event", "event": "PICKED_UP", "awb": awb_m.group(0), "date": d, "station": stn.group(1) if stn else None, "quote": s, "quoted": quoted})
            elif re.search(r"collected by the consignee|picked up by the consignee", s, re.I):
                facts.append({"type": "event", "event": "COLLECTED_BY_CONSIGNEE", "awb": awb_m.group(0), "date": d, "station": stn.group(1) if stn else None, "quote": s, "quoted": quoted})
            elif re.search(r"\bwas delivered\b|\bdelivered on\b|\bdelivery completed\b", s, re.I):
                facts.append({"type": "event", "event": "DELIVERED", "awb": awb_m.group(0), "date": d, "station": stn.group(1) if stn else None, "quote": s, "quoted": quoted})
            elif re.search(r"\barrived\b", s, re.I):
                facts.append({"type": "event", "event": "ARRIVED_FACILITY", "awb": awb_m.group(0), "date": d, "station": stn.group(1) if stn else None, "quote": s, "quoted": quoted})
            if re.search(r"customs hold|held (?:by|at) customs|placed on hold", s, re.I):
                reason = re.search(r"Reason code\s+([A-Z_]+)", text)
                rel = re.search(r"Released on\s+(" + DATE + ")", text)
                facts.append({"type": "customs_hold", "awb": awb_m.group(0), "date": d, "reason": reason.group(1) if reason else None,
                              "released": rel.group(1) if rel else None, "quote": s, "quoted": quoted,
                              "reason_quote": next((x for x in sents if reason and reason.group(1) in x), None)})
        # ---- contract extension
        m = re.search(r"\bextend(?:s|ed|ing)?\b[^.]*?\b(?:agreement|contract)\s+(?P<c>[A-Z]{2,4}-[A-Z0-9-]+)[^.]*?\b(?:to|until|through)\s+(?P<d>" + DATE + ")", s, re.I)
        if m:
            facts.append({"type": "contract_extension", "contract": m.group("c").upper(), "new_end": m.group("d"), "quote": s, "quoted": quoted})
        # ---- instructions aimed at an automated reader
        if INSTR.search(s):
            facts.append({"type": "instruction", "quote": s, "quoted": quoted})
    # ---- rate notice (multi-line): any intro naming a lane and a date range, then rate rows
    flat = re.sub(r"\s+", " ", text)
    if re.search(r"clause\s*3\.5|rate notice|notice of (?:the following )?rates|rates below apply|give notice of the following rates", flat, re.I):
        lane = re.search(r"lane\s+([A-Z0-9]{3,5}-[A-Z0-9]{3,5})", flat)
        mf = re.search(r"(?:effective|from|commencing|with effect from|starting)\s+(" + DATE + ")", flat, re.I)
        mt = re.search(r"(?:to|until|through|till|ending)\s+(" + DATE + ")", flat[mf.end():] if mf else flat, re.I)
        rows = []
        num = r"(\d[\d,]*(?:\.\d+)?)"
        for ln in text.split("\n"):
            ln = ln.strip().lstrip(">").strip()
            sep = r"\s*[:=\-]?\s*"
            cur_a, cur_b = r"(?:([A-Z]{3})\s*)?", r"\s*([A-Z]{3})?"
            r1 = re.match(r"^([A-Z0-9]{3,5}-[A-Z0-9]{3,5})\s+minimum charge" + sep + cur_a + num + cur_b + r"$", ln)
            r2 = re.match(r"^([A-Z0-9]{3,5}-[A-Z0-9]{3,5})\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*kg" + sep + cur_a + num + cur_b + r"$", ln)
            r3 = re.match(r"^([A-Z0-9]{3,5}-[A-Z0-9]{3,5})\s+(\d+(?:\.\d+)?)\+\s*kg" + sep + cur_a + num + cur_b + r"$", ln)
            f = lambda x: float(x.replace(",", ""))
            if r1:
                rows.append({"lane": r1.group(1), "band": "min", "lo": None, "hi": None, "rate": f(r1.group(3)), "currency": r1.group(2) or r1.group(4), "quote": ln})
            elif r2:
                rows.append({"lane": r2.group(1), "band": f"{r2.group(2)}-{r2.group(3)}", "lo": float(r2.group(2)), "hi": float(r2.group(3)), "rate": f(r2.group(5)), "currency": r2.group(4) or r2.group(6), "quote": ln})
            elif r3:
                rows.append({"lane": r3.group(1), "band": f"{r3.group(2)}+", "lo": float(r3.group(2)), "hi": None, "rate": f(r3.group(4)), "currency": r3.group(3) or r3.group(5), "quote": ln})
        if rows and mf:
            ref = re.search(r"(?:Reference|ref\.?|Notice ref\.?)\s*:?\s*([A-Z0-9][A-Z0-9/-]*/RN-[A-Z0-9-]+|[A-Z0-9][A-Z0-9-]*/RN[A-Z0-9-]*)", text)
            intro = next((x for x in sents if re.search(r"clause\s*3\.5|give notice|rates below apply", x, re.I)), "")
            facts.append({"type": "rate_notice", "ref": ref.group(1) if ref else None, "lane": lane.group(1) if lane else rows[0]["lane"],
                          "effective_from": mf.group(1), "effective_to": mt.group(1) if mt else None, "rows": rows,
                          "quote": intro, "quoted": quoted})
    return facts


def parse_email(doc: dict, vocab=None) -> dict:
    head, own, quoted = split_body(doc["pages"][0])
    facts = parse_text_facts(own, False, vocab) + parse_text_facts(quoted, True, vocab)
    return {"own_text": own, "quoted_text": quoted, "facts": facts}

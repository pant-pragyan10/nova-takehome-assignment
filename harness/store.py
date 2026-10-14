"""On-disk memory of everything the harness has read, plus the person's feedback. Lives entirely under --state-dir.

Layout:
    <state>/docs/<hash>.json      one file per document (an email, or one of its attachments): text pages + metadata
    <state>/facts/<hash>.json     what the model extracted from that document (see understand.py)
    <state>/batches.json          batches ingested, in order
    <state>/feedback.json         every feedback item received (see feedback.py)
    <state>/llm_cache/            cached model replies

Everything is keyed by the document `ref` the grader uses for evidence: 'batch_01/0014.eml' for an email and
'batch_01/0014.eml#INV-1.pdf' for an attachment. Re-ingesting a batch overwrites in place, so it is idempotent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import reader


def _h(ref: str) -> str:
    return hashlib.sha1(ref.encode()).hexdigest()[:16]


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


# ---- classification of attachments ------------------------------------------------------------------------------------
def classify_attachment(name: str, ext: str, pages: list[str]) -> str:
    """Cheap, layout-independent guess at what a document is. 'unknown' means: ask the model."""
    n = name.lower()
    head = " ".join(pages[0].split()).lower()[:900] if pages else ""
    ext = ext.lower()
    if ext == "xlsx":
        if "tracking" in n or ("awb" in head and "event" in head and "picked_up" in " ".join(pages).lower()[:4000]):
            return "tracking"
        if "register" in n or ("invoice no" in head and "status" in head and "paid" in head):
            return "ap_register"
        return "statement"            # a monthly account statement that carries its own number counts as an invoice
    if ext != "pdf":
        return "unknown"
    # Compare with all whitespace removed: OCR drops most spaces ("ChargesadviceCDI-3031-0306").
    c = re.sub(r"\s+", "", head)
    full = re.sub(r"\s+", "", " ".join(pages).lower())
    if "masterservicesagreement" in c and "agreementno" in c:
        return "msa"
    if re.search(r"amendmentno\.?\d+", c) or "-amd-" in n:
        return "amendment"
    if "creditnote" in c[:250] or "creditadvice" in c[:250]:
        return "credit_note"
    if "airwaybill" in c[:250] or ("waybill" in c[:150] and "chargeableweight" in c and "invoice" not in c[:200]):
        return "awb"
    if "declaration" in c[:250] and (re.search(r"un\d{4}", c) or "dryice" in c or "dangerous" in c or "hazard" in c):
        return "dgd"
    if "annexarev" in c[:300] or ("annexa" in c[:250] and "rev" in c[:250] and "invoice" not in c[:250]):
        return "annex"
    if re.search(r"(invoice|chargesadvice|taxinvoice|debitnote|freightbill|billno|billingstatement|statementofaccount)", c[:320]):
        return "invoice"
    # Layout-independent fallback: anything that looks like a bill (a total, a due/payable-by date, waybill refs).
    if "total" in full and re.search(r"(due|payableby|payabledate)", full) and re.search(r"(awb|waybill)", full) \
            and re.search(r"\d+\.\d{2}", full):
        return "invoice"
    return "unknown"


def classify_email(subject: str, body: str, has_attachments: bool) -> str:
    return "email"


# ---- ingest -------------------------------------------------------------------------------------------------------------
def ingest_batch(batch_dir: str | os.PathLike, state_dir: str | os.PathLike) -> list[str]:
    """Read every .eml in batch_dir (attachments included) into the state dir. Returns the refs written."""
    batch_dir = Path(batch_dir)
    state = Path(state_dir)
    batch = batch_dir.name
    refs: list[str] = []
    for f in sorted(batch_dir.glob("*.eml")):
        raw = f.read_bytes()
        msg = reader.parse_eml(raw)
        try:
            dt = parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc)
            received, received_ts = dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            received, received_ts = "1970-01-01", "1970-01-01T00:00:00Z"
        name, addr = parseaddr(str(msg["From"]))
        email_ref = f"{batch}/{f.name}"
        meta = {"email_ref": email_ref, "batch": batch, "received": received, "received_ts": received_ts,
                "from_name": name, "from_addr": addr.lower(), "to": str(msg["To"] or ""), "cc": str(msg["Cc"] or ""),
                "subject": str(msg["Subject"] or "")}
        email_pages = reader.pages_of(raw, "eml")
        _write(state / "docs" / f"{_h(email_ref)}.json",
               {**meta, "ref": email_ref, "att_name": None, "kind": "email", "scan": False, "pages": email_pages})
        refs.append(email_ref)
        for att_name, data in reader.attachments(raw):
            ext = att_name.rsplit(".", 1)[-1].lower() if "." in att_name else ""
            ref = f"{email_ref}#{att_name}"
            try:
                pages, scan = reader.smart_pages(data, ext)
            except Exception as e:  # unreadable attachment: keep a stub so the harness knows it exists
                pages, scan = [""], False
                reader_err = str(e)
            kind = classify_attachment(att_name, ext, pages)
            _write(state / "docs" / f"{_h(ref)}.json",
                   {**meta, "ref": ref, "att_name": att_name, "kind": kind, "scan": scan, "pages": pages})
            refs.append(ref)
    batches = _read(state / "batches.json", [])
    if batch not in batches:
        batches.append(batch)
    _write(state / "batches.json", batches)
    return refs


def load_docs(state_dir: str | os.PathLike, as_of: str | None = None) -> list[dict]:
    """All documents received on or before `as_of` (UTC date of the email's Date header), oldest first."""
    out = []
    for f in sorted((Path(state_dir) / "docs").glob("*.json")):
        d = _read(f)
        if d and (as_of is None or d["received"] <= as_of):
            out.append(d)
    out.sort(key=lambda d: (d["received_ts"], d["ref"]))
    return out


def save_doc(state_dir, doc: dict) -> None:
    _write(Path(state_dir) / "docs" / f"{_h(doc['ref'])}.json", doc)


def load_facts(state_dir) -> dict[str, dict]:
    out = {}
    for f in (Path(state_dir) / "facts").glob("*.json"):
        d = _read(f)
        if d:
            out[d["ref"]] = d
    return out


def save_facts(state_dir, ref: str, facts: dict) -> None:
    _write(Path(state_dir) / "facts" / f"{_h(ref)}.json", {"ref": ref, **facts})


def load_feedback(state_dir) -> list[dict]:
    return _read(Path(state_dir) / "feedback.json", [])


def save_feedback(state_dir, items: list[dict]) -> None:
    _write(Path(state_dir) / "feedback.json", items)

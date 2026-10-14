"""Document reader (standalone copy of grader/extract.py, plus OCR for scanned PDFs).

Read mailbox files back as text, page by page, the way the grader checks a citation.

Standalone on purpose (the candidate's copy of the grader imports nothing else from this repo). Same rules as the
generator's reader:
- text PDF: words grouped into lines by baseline, each line read left to right; one entry per PDF page;
- XLSX: one "page" per sheet (hidden sheets included), each row as its displayed cell texts joined by " | ";
- .eml: one page: From / To / Cc / Date (ISO, UTC) / Subject, a blank line, the plain-text body, then
  "Attachments: a.pdf, b.xlsx" when there are any.
Comparisons collapse whitespace, because a long line wraps at a space in a PDF.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

SEP = " | "


def norm(s: str) -> str:
    return " ".join(s.split())


# ---- PDF --------------------------------------------------------------------------------------------------------------
def _reading_order(page) -> str:
    words = page.get_text("words")
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    for w in words:
        mid = (w[1] + w[3]) / 2
        if lines and abs(lines[-1][0] - mid) <= 2.5:
            lines[-1][1].append(w)
        else:
            lines.append([mid, [w]])
    return "\n".join(" ".join(x[4] for x in sorted(ws, key=lambda w: w[0])) for _, ws in lines)


def pdf_pages(data: bytes) -> list[str]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [_reading_order(p) for p in doc]


def pdf_is_image_only(data: bytes) -> bool:
    """A scanned document: no text layer at all. Its quotes can't be checked from text."""
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return all(not p.get_text().strip() for p in doc)


# ---- XLSX -------------------------------------------------------------------------------------------------------------
def _cell_text(cell) -> str:
    v = cell.value
    if v is None:
        return ""
    fmt = cell.number_format or "General"
    prefix = ""
    m = re.match(r'^"([^"]*)"(.*)$', fmt)
    if m:
        prefix, fmt = m.group(1), m.group(2)
    if isinstance(v, datetime):
        return prefix + v.date().isoformat()
    if isinstance(v, date):
        return prefix + v.isoformat()
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        dp = len(fmt.split(".")[1]) if "." in fmt else 0
        return prefix + f"{Decimal(str(v)):.{dp}f}"
    return prefix + str(v)


def xlsx_pages(data: bytes) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    pages = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows():
            cells = [t for t in (_cell_text(c) for c in row) if t != ""]
            if cells:
                rows.append(SEP.join(cells))
        pages.append("\n".join(rows))
    return pages


# ---- email ------------------------------------------------------------------------------------------------------------
def parse_eml(data: bytes):
    return BytesParser(policy=policy.default).parsebytes(data)


def email_lines(msg) -> list[str]:
    name, addr = parseaddr(str(msg["From"]))
    out = [f"From: {name} <{addr}>", f"To: {msg['To']}"]
    if msg["Cc"]:
        out.append(f"Cc: {msg['Cc']}")
    dt = parsedate_to_datetime(str(msg["Date"]))
    out += [f"Date: {dt.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"Subject: {msg['Subject']}", ""]
    body = msg.get_body(preferencelist=("plain",))
    out += body.get_content().replace("\r\n", "\n").rstrip("\n").split("\n") if body else []
    names = [p.get_filename() for p in msg.iter_attachments()]
    if names:
        out.append("Attachments: " + ", ".join(names))
    return out


def attachments(data: bytes) -> list[tuple[str, bytes]]:
    return [(p.get_filename() or "", p.get_content()) for p in parse_eml(data).iter_attachments()]


# ---- one entry point --------------------------------------------------------------------------------------------------
def pages_of(data: bytes, ext: str) -> list[str]:
    ext = ext.lower().lstrip(".")
    if ext == "pdf":
        return pdf_pages(data)
    if ext == "xlsx":
        return xlsx_pages(data)
    if ext == "eml":
        return ["\n".join(email_lines(parse_eml(data)))]
    raise ValueError(f"can't read .{ext} files")


def found(pages: list[str], page: int, quote: str) -> bool:
    return 1 <= page <= len(pages) and norm(quote) in norm(pages[page - 1])


class Mailbox:
    """Resolves an evidence `doc` inside a mailbox directory (the folder holding batch_01/, batch_02/, ...).

    'batch_01/0014.eml'            the email itself
    'batch_01/0014.eml#INV-1.pdf'  an attachment of that email ('#attach/INV-1.pdf' works too)
    'INV-1.pdf'                    an attachment found by name, when exactly one attachment has that name
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._by_name: dict[str, list[tuple[str, bytes]]] | None = None
        self._pages: dict[str, tuple[list[str], bool]] = {}

    def _index(self):
        if self._by_name is None:
            self._by_name = {}
            for f in sorted(self.root.rglob("*.eml")):
                rel = f.relative_to(self.root).as_posix()
                for name, data in attachments(f.read_bytes()):
                    self._by_name.setdefault(name, []).append((f"{rel}#{name}", data))
        return self._by_name

    def resolve(self, doc: str) -> tuple[bytes, str, str]:
        """-> (bytes, extension, canonical doc path). Raises LookupError with a readable reason."""
        path, sep, att = doc.partition("#")
        att = att[len("attach/"):] if att.startswith("attach/") else att
        if sep:
            f = self.root / path
            if not f.is_file():
                raise LookupError(f"no email {path!r} in {self.root}")
            for name, data in attachments(f.read_bytes()):
                if name == att:
                    return data, name.rsplit(".", 1)[-1].lower(), f"{path}#{name}"
            raise LookupError(f"{path!r} has no attachment named {att!r}")
        f = self.root / doc
        if f.is_file() and f.suffix.lower() == ".eml":
            return f.read_bytes(), "eml", doc
        hits = self._index().get(doc, [])
        if len(hits) == 1:
            ref, data = hits[0]
            return data, doc.rsplit(".", 1)[-1].lower(), ref
        if len(hits) > 1:
            raise LookupError(f"{len(hits)} attachments are named {doc!r}; cite it as <email>#{doc}, "
                              f"e.g. {hits[0][0]!r}")
        raise LookupError(f"no email or attachment {doc!r} in {self.root}")

    def pages(self, doc: str) -> tuple[list[str], bool]:
        """-> (text per page, is_scan). A scan has no text layer; its quotes can't be checked."""
        if doc not in self._pages:
            data, ext, _ = self.resolve(doc)
            scan = ext == "pdf" and pdf_is_image_only(data)
            self._pages[doc] = (pages_of(data, ext), scan)
        return self._pages[doc]

    def check(self, doc: str, page: int, quote: str) -> str | None:
        """None when the citation resolves; otherwise the reason it doesn't."""
        try:
            pages, scan = self.pages(doc)
        except (LookupError, ValueError) as e:
            return str(e)
        if scan:
            return None if 1 <= page <= len(pages) else f"{doc} has {len(pages)} page(s), cited page {page}"
        if not 1 <= page <= len(pages):
            return f"{doc} has {len(pages)} page(s), cited page {page}"
        if not found(pages, page, quote):
            where = [i + 1 for i, p in enumerate(pages) if norm(quote) in norm(p)]
            hint = f" (it is on page {where[0]})" if where else " (not found on any page)"
            return f"quote not on page {page} of {doc}{hint}: {quote[:80]!r}"
        return None


# ---- OCR for scanned PDFs (no text layer) ------------------------------------------------------------------------------
_OCR = None


def _ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def ocr_pdf_pages(data: bytes) -> list[str]:
    """Rasterise each page and OCR it. Boxes are grouped into lines by vertical position and joined left to right.
    rapidocr drops most spaces inside a box, so downstream code compares labels with spaces removed."""
    import pymupdf
    out = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            res, _ = _ocr_engine()(pix.tobytes("png"))
            boxes = []
            for box, text, _conf in (res or []):
                ys = [pt[1] for pt in box]
                xs = [pt[0] for pt in box]
                boxes.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(xs), text))
            boxes.sort()
            lines: list[list] = []
            for cy, h, x, t in boxes:
                if lines and abs(lines[-1][0] - cy) <= max(8.0, 0.6 * h):
                    lines[-1][1].append((x, t))
                else:
                    lines.append([cy, [(x, t)]])
            out.append("\n".join(" ".join(t for _, t in sorted(ws)) for _, ws in lines))
    return out


def smart_pages(data: bytes, ext: str) -> tuple[list[str], bool]:
    """-> (pages, was_scanned). Falls back to OCR when a PDF has no text layer."""
    ext = ext.lower().lstrip(".")
    if ext == "pdf" and pdf_is_image_only(data):
        try:
            return ocr_pdf_pages(data), True
        except Exception as e:  # OCR unavailable: return empty pages so callers can degrade instead of crash
            import sys
            print(f"warning: OCR failed: {e}", file=sys.stderr)
            return [""], True
    return pages_of(data, ext), False

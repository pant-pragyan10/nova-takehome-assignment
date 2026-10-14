"""A local viewer for the harness.  python tools/ui.py  ->  open http://127.0.0.1:8500

Pick a mailbox (practice, exam, the 9 examples, or one of your own eval cases), a mode and a date; it runs the real
ingest + report commands in a fresh state folder and shows every finding with its evidence. Needs no extra packages.
Set NOVA_NO_LLM=1 before starting to force the rules-only path.
"""
from __future__ import annotations

import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import run_mailbox  # noqa: E402


def mailboxes() -> list[dict]:
    out = []
    for name, p in (("Practice mailbox (2 batches)", "data/scenarios"), ("Exam mailbox (2 batches)", "data/exam")):
        d = run_mailbox.batch_dates(ROOT / p)
        out.append({"id": p, "label": name, "path": p, "as_of": d[max(d)] if d else "", "feedback": {}, "note": ""})
    for e in sorted((ROOT / "data/examples").iterdir()):
        if (e / "docs").is_dir():
            note = (e / "EXPLANATION.md").read_text()[:600] if (e / "EXPLANATION.md").is_file() else ""
            out.append({"id": f"ex:{e.name}", "label": f"Example: {e.name}", "path": f"data/examples/{e.name}/docs",
                        "as_of": "2035-12-31", "feedback": {}, "note": note})
    for c in sorted((ROOT / "evals/cases").glob("*.yaml")):
        y = yaml.safe_load(c.read_text())
        out.append({"id": f"eval:{c.stem}", "label": f"My eval: {c.stem}", "path": y["mailbox"], "as_of": str(y["as_of"]),
                    "feedback": y.get("feedback") or {}, "note": f"{y['name']}. {y.get('why', '')}  Expected: {json.dumps(y['expect'])}",
                    "mode": y.get("mode", "supervised")})
    return out


def run(mid: str, mode: str, as_of: str, feedback: bool) -> dict:
    box = next(m for m in mailboxes() if m["id"] == mid)
    fb = {k: ROOT / v for k, v in box["feedback"].items()} if feedback else {}
    if not feedback and box["id"] == "data/scenarios":
        fb = {}
    elif feedback and box["id"] == "data/scenarios":
        fb = {"batch_01": ROOT / "data/scenarios/feedback/W1_batch_01.json"}
    log: list[str] = []
    with tempfile.TemporaryDirectory() as t:
        rep, errs = run_mailbox.run("harness/harness.py", ROOT / box["path"], as_of or box["as_of"], mode,
                                    Path(t) / "state", feedback=fb, log=log.append)
    return {"report": rep, "errors": errs, "log": log}


class H(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send((ROOT / "tools/ui.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/mailboxes":
            self._send(json.dumps(mailboxes()).encode(), "application/json")
        elif u.path == "/api/run":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                res = run(q["id"], q.get("mode", "supervised"), q.get("as_of", ""), q.get("feedback") == "1")
            except Exception as e:  # shown in the page
                res = {"report": None, "errors": [repr(e)], "log": []}
            self._send(json.dumps(res).encode(), "application/json")
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("open http://127.0.0.1:8500   (Ctrl+C to stop)")
    HTTPServer(("127.0.0.1", 8500), H).serve_forever()

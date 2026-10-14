"""DEV ONLY. A tiny local server that speaks the Anthropic Messages API and forwards to Groq's free tier.

The harness itself only contains Anthropic SDK code (that is what the grader runs). To develop with a Groq key:

    export GROQ_API_KEY=gsk_...                       # your Groq key
    python tools/groq_proxy.py &                       # listens on 127.0.0.1:8787
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
    export ANTHROPIC_API_KEY=anything                  # the SDK wants a value; the proxy ignores it
    make grade

Features: model mapping (GROQ_MODEL), on-disk response cache shared by every run (so reruns are free and don't burn
the free-tier quota), and server-side waiting/retry on 429s so the harness never sees a rate-limit error.
Never ship a key in the repo: this file reads it from the environment only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GROQ_URL = os.environ.get("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
CACHE = Path(os.environ.get("PROXY_CACHE", Path.home() / ".cache" / "nova_groq_proxy"))
CACHE.mkdir(parents=True, exist_ok=True)
GATE = threading.Semaphore(int(os.environ.get("PROXY_CONCURRENCY", "2")))


def to_openai(body: dict) -> dict:
    msgs = []
    system = body.get("system")
    if isinstance(system, list):
        system = "".join(b.get("text", "") for b in system)
    if system:
        msgs.append({"role": "system", "content": system})
    for m in body.get("messages", []):
        c = m["content"]
        if isinstance(c, str):
            msgs.append({"role": m["role"], "content": c})
            continue
        parts = []
        for b in c:
            if b.get("type") == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif b.get("type") == "image":
                s = b["source"]
                parts.append({"type": "image_url", "image_url": {"url": f"data:{s['media_type']};base64,{s['data']}"}})
        if all(p["type"] == "text" for p in parts):
            msgs.append({"role": m["role"], "content": "".join(p["text"] for p in parts)})
        else:
            msgs.append({"role": m["role"], "content": parts})
    return {"model": MODEL, "messages": msgs, "temperature": body.get("temperature", 0),
            "max_tokens": min(int(body.get("max_tokens", 4096)), 8000)}


def call_groq(payload: dict) -> dict:
    key = os.environ["GROQ_API_KEY"]
    delay = 5.0
    for attempt in range(12):
        req = urllib.request.Request(GROQ_URL, data=json.dumps(payload).encode(), method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "nova-dev-proxy/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503):
                wait = float(e.headers.get("retry-after") or delay)
                print(f"[proxy] {e.code}, waiting {wait:.0f}s ({txt[:120]})", file=sys.stderr)
                time.sleep(min(wait + 1, 90))
                delay = min(delay * 2, 60)
                continue
            raise RuntimeError(f"groq {e.code}: {txt[:400]}")
        except (urllib.error.URLError, TimeoutError):
            time.sleep(delay)
    raise RuntimeError("groq: gave up after retries")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        payload = to_openai(body)
        k = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        f = CACHE / f"{k}.json"
        try:
            if f.is_file():
                out = json.loads(f.read_text())
            else:
                with GATE:
                    out = call_groq(payload)
                f.write_text(json.dumps(out))
            ch = out["choices"][0]
            u = out.get("usage", {})
            self._send(200, {
                "id": "msg_proxy", "type": "message", "role": "assistant", "model": body.get("model", MODEL),
                "content": [{"type": "text", "text": ch["message"]["content"] or ""}],
                "stop_reason": "end_turn" if ch.get("finish_reason") == "stop" else "max_tokens",
                "stop_sequence": None,
                "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}})
        except Exception as e:
            self._send(500, {"type": "error", "error": {"type": "api_error", "message": str(e)}})


if __name__ == "__main__":
    port = int(os.environ.get("PROXY_PORT", "8787"))
    print(f"groq proxy on http://127.0.0.1:{port} -> {MODEL}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()

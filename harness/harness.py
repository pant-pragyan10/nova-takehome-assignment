"""Freight invoice audit harness. The grader calls exactly these three commands, in a fresh process each time:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

- ingest: read every .eml in <batch_dir> (attachments inside), keep the text on disk, and have the model turn each
  document into structured facts (understand.py). Everything is stored under --state-dir.
- report: deterministic. Rebuilds the world from the stored facts of documents received by --as-of and applies the
  glossary's rules (engine.py). No model calls, so the same state always gives the same report.
- feedback: store a person's decisions (feedback.py) so later reports use them.

Reads ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL from the environment (used by llm.py only).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_STATE = ".state"


def ingest(batch_dir: str, state_dir: str) -> None:
    import llm
    import store
    import understand
    os.makedirs(state_dir, exist_ok=True)
    llm.configure(state_dir)
    store.ingest_batch(batch_dir, state_dir)
    understand.understand_all(state_dir)


def report(as_of: str, mode: str, state_dir: str) -> dict:
    import engine
    return engine.build_report(state_dir, as_of, mode)


def feedback(feedback_file: str, state_dir: str) -> None:
    import feedback as fb
    os.makedirs(state_dir, exist_ok=True)
    fb.ingest_feedback(feedback_file, state_dir)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ingest":
            ingest(a.batch_dir, a.state_dir)
        elif a.cmd == "report":
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

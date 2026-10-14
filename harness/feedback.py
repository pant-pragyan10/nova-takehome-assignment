"""A person's decisions (feedback.json) -> stored facts that later reports use.

`ingest_feedback` stores every item with its source (file, batch, decided_at, who decided), so any report line that changed
because of feedback can name the item it learned from (`learned_at`). Nothing here touches the documents.

`Feedback` is the read side used by the engine. Its rules (GLOSSARY, 'How to use it without going too far'):
  * a `standing` fact applies to a later key only if every filter in scope.applies_to matches, inside valid_from..valid_to;
  * a `one_off` decision changes that one key and nothing else;
  * `close` / `reject` stop that key only.
"""
from __future__ import annotations

import json
from pathlib import Path

import store
from world import norm_name, norm_inv, pdate


def ingest_feedback(path: str, state_dir: str) -> int:
    raw = json.loads(Path(path).read_text())
    items = store.load_feedback(state_dir)
    have = {i["item_id"] for i in items}
    n = 0
    for it in raw.get("items", []):
        if it.get("item_id") in have:
            items = [i for i in items if i["item_id"] != it["item_id"]]
        rec = dict(it)
        rec["batch"] = raw.get("after_batch")
        rec["world"] = raw.get("world")
        rec["decided_at"] = raw.get("decided_at")
        rec["decided_by"] = raw.get("decided_by")
        rec["source_file"] = Path(path).name
        items.append(rec)
        n += 1
    store.save_feedback(state_dir, items)
    return n


class Feedback:
    def __init__(self, items: list[dict], as_of: str):
        self.items = [i for i in items if (i.get("decided_at") or "0000")[:10] <= as_of]

    # ------------------------------------------------------------------------------------------------
    @staticmethod
    def learned(it: dict) -> dict:
        return {"feedback_item_id": it["item_id"], "batch": it.get("batch") or ""}

    @staticmethod
    def _in_window(it: dict, when) -> bool:
        sc = it.get("scope") or {}
        vf, vt = pdate(sc.get("valid_from")), pdate(sc.get("valid_to"))
        if when is None:
            return True
        if vf and when < vf:
            return False
        if vt and when > vt:
            return False
        return True

    def key_items(self, invoice_id: str | None, cls: str | None, **keys) -> list[dict]:
        out = []
        for it in self.items:
            fk = it.get("finding_key") or {}
            if invoice_id is not None and norm_inv(fk.get("invoice_id") or "") == norm_inv(invoice_id) and (cls is None or fk.get("class") == cls):
                out.append(it)
            elif invoice_id is None and keys and all(fk.get(k) == v for k, v in keys.items()):
                out.append(it)
        return out

    def decision_for(self, invoice_id, cls, kinds: tuple[str, ...]) -> dict | None:
        """The latest decision on this exact key, of the given kinds. Standing close+rate_accepted is NOT a key decision."""
        for it in reversed(self.key_items(invoice_id, cls)):
            if it["decision"] in kinds:
                return it
        return None

    def keyed_decision(self, key: dict, kinds: tuple[str, ...]) -> dict | None:
        for it in reversed(self.items):
            fk = it.get("finding_key") or {}
            if it["decision"] in kinds and all(fk.get(k) == v for k, v in key.items()):
                return it
        return None

    # ------------------------------------------------------------------------------------------------
    def authority(self, person: str, category: str, contract_id: str, when) -> dict | None:
        """A confirmed sender_authority fact for this person, category and contract (valid at `when`)."""
        for it in self.items:
            f = it.get("fact") or {}
            if it["decision"] != "confirm" or f.get("type") != "sender_authority":
                continue
            if norm_name((f.get("subject") or {}).get("person")) != norm_name(person):
                continue
            if category not in ((f.get("value") or {}).get("categories") or []):
                continue
            ap = (it.get("scope") or {}).get("applies_to") or {}
            if ap.get("contract_id") and ap["contract_id"] != contract_id:
                continue
            if ap.get("category") and ap["category"] != category:
                continue
            if not self._in_window(it, when):
                continue
            return it
        return None

    def accepted_rate(self, contract_id: str, lane: str, when, sender: str | None = None, effective_from: str | None = None):
        for it in self.items:
            f = it.get("fact") or {}
            if f.get("type") != "rate_accepted" or it["decision"] not in ("close", "confirm"):
                continue
            sub = f.get("subject") or {}
            if sub.get("contract_id") != contract_id or sub.get("lane") != lane:
                continue
            ap = (it.get("scope") or {}).get("applies_to") or {}
            if ap.get("contract_id") and ap["contract_id"] != contract_id:
                continue
            if ap.get("lane") and ap["lane"] != lane:
                continue
            if not self._in_window(it, when):
                continue
            v = f.get("value") or {}
            if sender and v.get("sent_by") and norm_name(v["sent_by"]) != norm_name(sender):
                continue
            if effective_from and v.get("effective_from") and v["effective_from"] != effective_from:
                continue
            return it
        return None

    def precedence(self, contract_id: str, lane: str, when) -> dict | None:
        for it in reversed(self.items):
            f = it.get("fact") or {}
            if f.get("type") != "precedence" or it["decision"] != "resolve_conflict":
                continue
            ap = (it.get("scope") or {}).get("applies_to") or {}
            if ap.get("contract_id") != contract_id or (ap.get("lane") and ap["lane"] != lane):
                continue
            if not self._in_window(it, when):
                continue
            return it
        return None

    def waiver_scope(self, invoice_id: str) -> list[dict]:
        out = []
        for it in self.items:
            f = it.get("fact") or {}
            if f.get("type") == "waiver_scope" and it["decision"] in ("one_off_approval", "reject") \
                    and norm_inv(((it.get("scope") or {}).get("applies_to") or {}).get("invoice_id") or
                                 (it.get("finding_key") or {}).get("invoice_id") or "") == norm_inv(invoice_id):
                out.append(it)
        return out

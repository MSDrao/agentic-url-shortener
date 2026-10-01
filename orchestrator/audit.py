"""Append-only, hash-chained audit log (JSON Lines) with a separate completeness anchor.

Integrity model
  * Chain: each record embeds the SHA-256 of the previous record, so editing,
    reordering or deleting a record *inside* the log breaks verification.
  * Anchor: the chain alone cannot prove the log is complete - dropping the last
    N records leaves a valid chain. After every append the writer also stores the
    head (record count + last hash) in `audit.anchor.json`, and the engine copies
    the final head into `runs/history.jsonl`. `verify()` requires the log to end
    exactly at the anchored head.
  * Keyed anchor (optional): if ORCH_AUDIT_KEY is set, the anchor carries an
    HMAC-SHA256 over (run_id, seq, head). Without the key, a truncated log cannot
    be re-anchored to look complete.

Limits (stated, not hidden): someone who can rewrite the log AND the anchor AND
history - or who holds the HMAC key - can still forge a consistent history.
Production deployments should ship anchors to external append-only storage
(WORM bucket, transparency log) or sign them with a key held outside the host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
KEY_ENV = "ORCH_AUDIT_KEY"


def _digest(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _mac(key: bytes, run_id: str, seq: int, head: str) -> str:
    return hmac.new(key, f"{run_id}:{seq}:{head}".encode(), hashlib.sha256).hexdigest()


def anchor_path(log_path: Path) -> Path:
    return Path(log_path).with_suffix(".anchor.json")


def _resolve_key(key: bytes | None) -> bytes | None:
    """None -> read ORCH_AUDIT_KEY from the environment; b"" -> explicitly unkeyed."""
    if key is None:
        raw = os.environ.get(KEY_ENV)
        return raw.encode() if raw else None
    return key or None


class AuditLog:
    def __init__(self, path: Path, run_id: str, key: bytes | None = None):
        self.path = Path(path)
        self.run_id = run_id
        self.key = _resolve_key(key)
        self._lock = threading.Lock()
        self._seq, self._prev = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open() as fh:
            for line in fh:
                if line.strip():
                    last = json.loads(line)
        return (last["seq"], last["hash"]) if last else (0, GENESIS)

    @property
    def head(self) -> dict[str, Any]:
        return {"seq": self._seq, "hash": self._prev}

    def record(self, event: str, *, node: str | None = None, actor: str = "orchestrator", **data: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            rec = {
                "seq": self._seq,
                "ts": time.time(),
                "run_id": self.run_id,
                "event": event,
                "node": node,
                "actor": actor,
                "data": data,
                "prev_hash": self._prev,
            }
            rec["hash"] = _digest(rec)
            self._prev = rec["hash"]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            self._write_anchor()
            return rec

    def _write_anchor(self) -> None:
        anchor = {"run_id": self.run_id, "seq": self._seq, "head": self._prev, "updated_at": time.time(),
                  "keyed": self.key is not None}
        if self.key is not None:
            anchor["mac"] = _mac(self.key, self.run_id, self._seq, self._prev)
        tmp = anchor_path(self.path).with_suffix(".tmp")
        tmp.write_text(json.dumps(anchor, sort_keys=True))
        tmp.replace(anchor_path(self.path))  # atomic

    @staticmethod
    def verify(path: Path, key: bytes | None = None, expected_head: dict[str, Any] | None = None) -> tuple[bool, str]:
        """Verify chain integrity AND completeness against the anchor (and an optional
        independently stored head, e.g. from history.jsonl)."""
        key = _resolve_key(key)
        prev, expected_seq, run_id = GENESIS, 1, None
        with Path(path).open() as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                rec = json.loads(line)
                run_id = run_id or rec.get("run_id")
                if rec["seq"] != expected_seq:
                    return False, f"line {lineno}: sequence gap (expected {expected_seq}, got {rec['seq']})"
                if rec["prev_hash"] != prev:
                    return False, f"line {lineno}: chain broken (prev_hash mismatch)"
                if _digest(rec) != rec["hash"]:
                    return False, f"line {lineno}: record content was modified"
                prev, expected_seq = rec["hash"], expected_seq + 1
        count = expected_seq - 1

        ap = anchor_path(path)
        if not ap.exists():
            return False, f"{count} records chain-verified, but no anchor: completeness cannot be established"
        anchor = json.loads(ap.read_text())
        if key is not None and not anchor.get("keyed"):
            # Downgrade protection: with a key in hand, an unsigned anchor is never acceptable
            # (otherwise flipping "keyed" to false would bypass the MAC check).
            return False, "anchor is unsigned but a verification key was supplied (possible downgrade)"
        if anchor.get("keyed"):
            if key is None:
                return False, f"anchor is HMAC-protected; set {KEY_ENV} to verify it"
            if not hmac.compare_digest(anchor.get("mac", ""), _mac(key, anchor["run_id"], anchor["seq"], anchor["head"])):
                return False, "anchor MAC invalid: anchor was forged or the key is wrong"
        if anchor["seq"] != count or anchor["head"] != prev:
            return False, (f"log does not end at the anchored head: log has {count} records, anchor says "
                           f"{anchor['seq']} (records were truncated or appended out of band)")
        if expected_head is not None and (expected_head.get("seq") != count or expected_head.get("hash") != prev):
            return False, f"log head does not match the independently stored head (seq {expected_head.get('seq')})"
        detail = " + independent head" if expected_head is not None else ""
        mode = "keyed anchor" if anchor.get("keyed") else "anchor"
        return True, f"{count} records verified (chain + {mode}{detail})"

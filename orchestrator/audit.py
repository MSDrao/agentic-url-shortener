"""Append-only, hash-chained audit log (JSON Lines).

Each record embeds the SHA-256 of the previous record, so any edit, deletion
or reordering after the fact is detected by `verify()`.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _digest(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
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
            return rec

    @staticmethod
    def verify(path: Path) -> tuple[bool, str]:
        prev, expected_seq = GENESIS, 1
        with Path(path).open() as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec["seq"] != expected_seq:
                    return False, f"line {lineno}: sequence gap (expected {expected_seq}, got {rec['seq']})"
                if rec["prev_hash"] != prev:
                    return False, f"line {lineno}: chain broken (prev_hash mismatch)"
                if _digest(rec) != rec["hash"]:
                    return False, f"line {lineno}: record content was modified"
                prev, expected_seq = rec["hash"], expected_seq + 1
        return True, f"{expected_seq - 1} records verified"

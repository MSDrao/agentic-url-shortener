"""Reliability metrics: success rate, retry/rollback frequency, MTTR, latency."""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # Execution time excludes time spent waiting for humans (paused runs, approval prompts),
    # so the wall-clock budget measures agent work, not reviewer response time.
    active_s: float = 0.0
    approval_wait_s: float = 0.0
    attempts: int = 0
    attempt_successes: int = 0
    attempt_failures: int = 0
    retries: int = 0
    fallbacks: int = 0
    rollbacks: int = 0
    reworks: int = 0
    replans: int = 0
    approvals_requested: int = 0
    approvals_rejected: int = 0
    approvals_human: int = 0
    approvals_simulated: int = 0
    policy_violations: int = 0
    stage_latency: dict[str, float] = field(default_factory=dict)
    open_incidents: dict[str, float] = field(default_factory=dict)  # key -> opened_at
    repair_times: list[float] = field(default_factory=list)

    # incidents: opened on first failure, closed when the same node next succeeds
    def open_incident(self, key: str) -> None:
        self.open_incidents.setdefault(key, time.time())

    def close_incident(self, key: str) -> None:
        opened = self.open_incidents.pop(key, None)
        if opened is not None:
            self.repair_times.append(time.time() - opened)

    def add_latency(self, stage: str, seconds: float) -> None:
        self.stage_latency[stage] = self.stage_latency.get(stage, 0.0) + seconds

    def summary(self) -> dict[str, Any]:
        end = self.finished_at or time.time()
        return {
            "end_to_end_latency_s": round(end - self.started_at, 3),
            "active_execution_s": round(self.active_s, 3),
            "approval_wait_s": round(self.approval_wait_s, 3),
            "attempts": self.attempts,
            "attempt_success_rate": round(self.attempt_successes / self.attempts, 3) if self.attempts else None,
            "retries": self.retries,
            "retry_rate": round(self.retries / self.attempts, 3) if self.attempts else 0.0,
            "fallbacks": self.fallbacks,
            "rollbacks": self.rollbacks,
            "reworks": self.reworks,
            "replans": self.replans,
            "approvals_requested": self.approvals_requested,
            "approvals_rejected": self.approvals_rejected,
            "approvals_human": self.approvals_human,
            "approvals_simulated": self.approvals_simulated,
            "policy_violations": self.policy_violations,
            "incidents_recovered": len(self.repair_times),
            "incidents_unrecovered": len(self.open_incidents),
            "mttr_s": round(statistics.mean(self.repair_times), 3) if self.repair_times else None,
            "stage_latency_s": {k: round(v, 3) for k, v in sorted(self.stage_latency.items())},
        }

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Metrics":
        m = cls()
        m.__dict__.update(d)
        return m


def aggregate(history_file: Path) -> dict[str, Any]:
    """Fleet view across runs (reads runs/history.jsonl)."""
    if not history_file.exists():
        return {"runs": 0}
    latest: dict[str, dict] = {}
    for line in history_file.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            latest[rec["run_id"]] = rec  # a resumed run supersedes its earlier record
    runs = list(latest.values())
    ok = [r for r in runs if r["status"] == "succeeded"]
    mttrs = [r["metrics"]["mttr_s"] for r in runs if r["metrics"].get("mttr_s") is not None]
    lat = [r["metrics"]["end_to_end_latency_s"] for r in runs]
    attempts = sum(r["metrics"]["attempts"] for r in runs)
    return {
        "runs": len(runs),
        "run_success_rate": round(len(ok) / len(runs), 3),
        "status_breakdown": {s: sum(1 for r in runs if r["status"] == s) for s in sorted({r["status"] for r in runs})},
        "retry_rate": round(sum(r["metrics"]["retries"] for r in runs) / attempts, 3) if attempts else 0,
        "rollbacks_per_run": round(sum(r["metrics"]["rollbacks"] for r in runs) / len(runs), 3),
        "mttr_s_mean": round(statistics.mean(mttrs), 3) if mttrs else None,
        "latency_s_p50": round(statistics.median(lat), 3),
        "latency_s_max": round(max(lat), 3),
    }

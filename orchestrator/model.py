"""Core data types shared by the engine, agents, gates and policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"      # an upstream dependency failed / entry gate failed
    SKIPPED = "skipped"      # safe-stop happened before it ran


TERMINAL = {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.SKIPPED}


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    HALTED = "halted"            # safe-stop: resumable
    ROLLED_BACK = "rolled_back"  # release rejected: workspace restored to baseline


@dataclass
class RetryPolicy:
    max_attempts: int = 2
    backoff_seconds: float = 0.2
    backoff_multiplier: float = 2.0

    def delay(self, attempt: int) -> float:
        return self.backoff_seconds * (self.backoff_multiplier ** max(0, attempt - 1))


@dataclass
class NodeSpec:
    """Declarative definition of one SDLC step in the dependency graph."""

    id: str
    stage: str
    agent: str
    deps: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)   # artifact names read
    produces: list[str] = field(default_factory=list)   # artifact names written
    entry_gates: list[str] = field(default_factory=list)
    exit_gates: list[str] = field(default_factory=list)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    fallback_agent: str | None = None
    # What to do once retries + fallback are exhausted:
    #   "stop"          -> safe-stop the run
    #   "rework:<node>" -> roll the workspace back to before <node>, re-run it with feedback
    on_failure: str = "stop"
    description: str = ""
    dynamic: bool = False  # inserted at runtime by re-planning

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["retry"] = self.retry.__dict__.copy()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NodeSpec":
        d = dict(d)
        d["retry"] = RetryPolicy(**d.get("retry", {}))
        return cls(**d)


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str = ""


@dataclass
class AgentResult:
    """What an agent proposes. The engine - not the agent - commits it after gates."""

    artifacts: dict[str, Any] = field(default_factory=dict)
    file_changes: dict[str, str | None] = field(default_factory=dict)  # None = delete
    decisions: list[dict[str, Any]] = field(default_factory=list)
    plan_changes: list[dict[str, Any]] = field(default_factory=list)
    impact: set[str] = field(default_factory=set)  # e.g. {"schema_change", "pii"}
    summary: str = ""


def content_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]

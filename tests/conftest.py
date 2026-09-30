from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from orchestrator.agents.base import Agent, AgentTask  # noqa: E402
from orchestrator.approval import ScriptedApprover  # noqa: E402
from orchestrator.context import RunContext  # noqa: E402
from orchestrator.engine import Engine  # noqa: E402
from orchestrator.graph import WorkflowGraph  # noqa: E402
from orchestrator.llm import OfflineProvider  # noqa: E402
from orchestrator.model import AgentResult, NodeSpec, RetryPolicy  # noqa: E402
from orchestrator.policy import Policy  # noqa: E402
from orchestrator.workspace import Workspace  # noqa: E402

BASE_POLICY: dict[str, Any] = {
    "budgets": {"max_total_attempts": 50, "max_wall_seconds": 60, "max_rework_cycles": 2},
    "autonomy": {"write_scopes": {"writer": ["src/*.py"]}, "max_files_per_commit": 10},
    "approvals": {"always": [], "on_impact": ["schema_change"], "rollback_on_reject": ["release"],
                  "max_revision_rounds": 3},
    "change_control": {"protected_paths": []},
    "security": {"block_at_or_above": "high", "forbidden_calls": ["eval"], "secret_patterns": ["AKIA[0-9A-Z]{16}"]},
    "compliance": {"pii_fields": ["ip"]},
    "replanning": {"allowed_templates": ["extra"]},
    "quality": {"min_coverage": 80},
}


class FnAgent(Agent):
    """Test agent driven by a function(task, call_no) -> AgentResult (or raises)."""

    def __init__(self, name: str, fn):
        self.name = name
        self.fn = fn
        self.calls = 0
        self.lock = threading.Lock()
        self.spans: list[tuple[float, float]] = []

    def run(self, task: AgentTask) -> AgentResult:
        with self.lock:
            self.calls += 1
            n = self.calls
        start = time.time()
        try:
            return self.fn(task, n)
        finally:
            self.spans.append((start, time.time()))


def produce(name: str, value: Any = None, **kw) -> AgentResult:
    return AgentResult(artifacts={name: value if value is not None else {"ok": True}}, **kw)


@pytest.fixture
def make_engine(tmp_path):
    def _make(nodes: list[NodeSpec], agents: dict[str, Agent], policy: dict | None = None,
              approvals: dict | None = None, templates: dict | None = None) -> Engine:
        run_dir = tmp_path / "run"
        run_dir.mkdir(exist_ok=True)
        ws = Workspace.create(run_dir, None)
        scenario = {"id": "t", "title": "test", "type": "test", "requirement": "r"}
        return Engine(
            run_id="t", run_dir=run_dir, repo_root=REPO, scenario=scenario,
            graph=WorkflowGraph(nodes), ctx=RunContext(scenario), agents=agents,
            llm=OfflineProvider({}), policy=Policy(policy or BASE_POLICY),
            approver=ScriptedApprover("tester", approvals or {}), workspace=ws,
            node_templates=templates or {}, max_parallel=4, log=lambda *_: None,
        )
    return _make


__all__ = ["FnAgent", "produce", "NodeSpec", "RetryPolicy", "AgentResult", "BASE_POLICY"]

"""Agent contract. Agents read context and propose results; they never commit."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llm import LLMProvider
from ..model import AgentResult, NodeSpec
from ..policy import Policy
from ..workspace import Workspace


class AgentError(RuntimeError):
    pass


@dataclass
class AgentTask:
    node: NodeSpec
    attempt: int      # attempt within the current execution cycle (resets on rework)
    execution: int    # total executions of this node in the run (never resets)
    inputs: dict[str, Any]
    feedback: list[str]
    scenario: dict[str, Any]
    workspace: Workspace
    llm: LLMProvider
    policy: Policy
    run_dir: Path
    repo_root: Path
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def answers(self) -> dict[str, Any]:
        spec = self.inputs.get("requirements_spec") or {}
        return spec.get("answers", {})


class Agent(ABC):
    name: str = "agent"
    #: short description of the agent's autonomy boundary (shown in docs/report)
    boundary: str = ""

    @abstractmethod
    def run(self, task: AgentTask) -> AgentResult: ...

"""SDLC graph template and agent registry.

    intake ─┬─> [codebase_analysis] ─> design ──> {privacy_review}* ─┐
            └─> test_plan ─────────────────────────────────────────┴─> implement ─┬─> run_tests ─────┐
                                                                                   ├─> security_scan ─┼─> release_readiness
                                                                                   └─> docs ──────────┘
    * inserted at runtime by re-planning when the requirement involves personal data.
    run_tests / security_scan failures loop back (rework) to implement with rollback.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .agents.analysis import CodebaseAnalysisAgent
from .agents.base import Agent
from .agents.design import ArchitectAgent, PrivacyOfficerAgent, TestPlannerAgent
from .agents.docs import TechWriterAgent
from .agents.implement import ImplementationAgent
from .agents.release import ReleaseManagerAgent
from .agents.requirements import RequirementsAgent
from .agents.verify import SecurityScannerAgent, TestRunnerAgent
from .graph import WorkflowGraph
from .llm import LLMProvider
from .model import NodeSpec, RetryPolicy


def build_graph(scenario: dict[str, Any], with_fallbacks: bool) -> WorkflowGraph:
    brownfield = scenario.get("baseline") is not None
    fb = (lambda name: f"{name}_offline") if with_fallbacks else (lambda name: None)
    nodes = [
        NodeSpec("intake", "requirements", "requirements_analyst",
                 consumes=["?clarifications"], produces=["requirements_spec"],
                 exit_gates=["spec_complete"], retry=RetryPolicy(max_attempts=2),
                 fallback_agent=fb("requirements_analyst"),
                 description="Interpret intent, detect ambiguity, normalize into a testable spec"),
        NodeSpec("test_plan", "test-design", "test_planner", deps=["intake"],
                 consumes=["requirements_spec"], produces=["test_plan"],
                 exit_gates=["test_plan_covers_acs"],
                 description="Derive test cases from acceptance criteria"),
        NodeSpec("design", "architecture", "architect",
                 deps=["intake"] + (["codebase_analysis"] if brownfield else []),
                 consumes=["requirements_spec"] + (["impact_analysis"] if brownfield else []),
                 produces=["design"], exit_gates=["tasks_acyclic", "traceability"],
                 retry=RetryPolicy(max_attempts=3), fallback_agent=fb("architect"),
                 description="Approach, contracts, task DAG, risks, rollback plan"),
        NodeSpec("implement", "implementation", "implementer", deps=["design", "test_plan"],
                 consumes=["requirements_spec", "design", "test_plan"], produces=["change_set"],
                 exit_gates=["has_changes", "compiles"], retry=RetryPolicy(max_attempts=2),
                 fallback_agent=fb("implementer"),
                 description="Produce the code + test change set"),
        NodeSpec("run_tests", "testing", "test_runner", deps=["implement"],
                 consumes=["test_plan"], produces=["test_report"],
                 entry_gates=["workspace_has_code"],
                 exit_gates=["tests_pass", "coverage_min", "planned_tests_pass"],
                 retry=RetryPolicy(max_attempts=1), on_failure="rework:implement",
                 description="Run the full suite in the isolated workspace"),
        NodeSpec("security_scan", "security", "security_scanner", deps=["implement"],
                 produces=["security_report"], exit_gates=["no_blocking_findings"],
                 retry=RetryPolicy(max_attempts=1), on_failure="rework:implement",
                 description="Static security/compliance scan of the change"),
        NodeSpec("docs", "documentation", "tech_writer", deps=["implement"],
                 consumes=["requirements_spec", "design"], produces=["docs_report"],
                 exit_gates=["docs_cover_routes"],
                 description="API reference from OpenAPI, changelog, ADRs"),
        NodeSpec("release_readiness", "release", "release_manager",
                 deps=["run_tests", "security_scan", "docs"],
                 consumes=["requirements_spec", "design", "test_plan", "test_report", "security_report",
                           "docs_report", "?privacy_review"],
                 produces=["release_readiness"], exit_gates=["checklist_green"],
                 retry=RetryPolicy(max_attempts=1),
                 description="Aggregate evidence; human go/no-go"),
    ]
    if brownfield:
        nodes.insert(1, NodeSpec("codebase_analysis", "analysis", "codebase_analyst", deps=["intake"],
                                 consumes=["requirements_spec"], produces=["impact_analysis"],
                                 entry_gates=["workspace_has_code"], exit_gates=["impact_identified"],
                                 description="AST-based impact analysis of the existing codebase"))
    return WorkflowGraph(nodes)


def node_templates() -> dict[str, Callable[[], NodeSpec]]:
    """Nodes that re-planning may insert at runtime (must also be allowlisted in policy)."""
    return {
        "privacy_review": lambda: NodeSpec(
            "privacy_review", "compliance", "privacy_officer",
            consumes=["requirements_spec", "design"], produces=["privacy_review"],
            exit_gates=["privacy_compliant"], retry=RetryPolicy(max_attempts=1), dynamic=True,
            description="Privacy/compliance review (inserted because the requirement involves personal data)"),
    }


def build_agents(primary: LLMProvider, offline: LLMProvider) -> dict[str, Agent]:
    agents: dict[str, Agent] = {
        "requirements_analyst": RequirementsAgent(primary),
        "codebase_analyst": CodebaseAnalysisAgent(),
        "architect": ArchitectAgent(primary),
        "test_planner": TestPlannerAgent(),
        "privacy_officer": PrivacyOfficerAgent(),
        "implementer": ImplementationAgent(primary),
        "test_runner": TestRunnerAgent(),
        "security_scanner": SecurityScannerAgent(),
        "tech_writer": TechWriterAgent(),
        "release_manager": ReleaseManagerAgent(),
    }
    # Deterministic fallbacks, used when a live-LLM agent exhausts its retries.
    agents["requirements_analyst_offline"] = RequirementsAgent(offline)
    agents["architect_offline"] = ArchitectAgent(offline)
    agents["implementer_offline"] = ImplementationAgent(offline)
    return agents

"""End-to-end: every scenario and drill runs through the real CLI and ends in the expected state."""

from __future__ import annotations

import json
import shutil

import pytest

from orchestrator import cli
from orchestrator.audit import AuditLog


@pytest.fixture(autouse=True)
def isolated_runs_dir(tmp_path, monkeypatch):
    """Keep test runs (and their history.jsonl entries) out of the real runs/ directory."""
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")


CASES = [
    # scenario file, expected run status, extra assertions on metrics
    ("greenfield.yaml", "succeeded", lambda m: m["retries"] >= 1 and m["mttr_s"] is not None),
    ("brownfield.yaml", "succeeded", lambda m: m["rollbacks"] == 1 and m["reworks"] == 1),
    ("ambiguous.yaml", "succeeded", lambda m: m["replans"] >= 3),
    ("drills/release-rejected.yaml", "rolled_back", lambda m: m["approvals_rejected"] == 1),
    ("drills/policy-violation.yaml", "halted", lambda m: m["policy_violations"] >= 1),
    ("drills/rework-exhausted.yaml", "halted", lambda m: m["reworks"] == 2),
]


@pytest.mark.parametrize("scenario,expected,check", CASES, ids=[c[0] for c in CASES])
def test_scenario_end_to_end(scenario, expected, check):
    run_id = "pytest-" + scenario.replace("/", "-").removesuffix(".yaml")
    rc = cli.main(["run", str(cli.REPO / "scenarios" / scenario), "--run-id", run_id, "--approvals", "auto"])
    run_dir = cli.RUNS / run_id
    try:
        state = json.loads((run_dir / "state.json").read_text())
        metrics = json.loads((run_dir / "metrics.json").read_text())
        assert state["run_status"] == expected, state.get("stop_reason")
        assert check(metrics), metrics
        assert AuditLog.verify(run_dir / "audit.jsonl")[0]
        assert (run_dir / "SUMMARY.md").exists()
        assert rc == 0
        if expected == "succeeded":
            rr = state["context"]["artifacts"]["release_readiness"][-1]["content"]
            assert rr["recommendation"] == "GO"
            assert all(m["verified"] for m in rr["traceability"])
            assert (run_dir / "change.patch").read_text().strip()
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_ambiguous_revision_changes_the_delivered_code():
    run_id = "pytest-ambiguous-lineage"
    cli.main(["run", str(cli.REPO / "scenarios" / "ambiguous.yaml"), "--run-id", run_id, "--approvals", "auto"])
    run_dir = cli.RUNS / run_id
    try:
        state = json.loads((run_dir / "state.json").read_text())
        arts = state["context"]["artifacts"]
        spec = arts["requirements_spec"][-1]["content"]
        assert len(arts["requirements_spec"]) == 2
        assert spec["answers"]["Q2"] == "create_and_redirect"
        assert {a["id"]: a["status"] for a in spec["ambiguities"]}["Q2"] == "confirmed"
        assert "privacy_review" in state["status"] and state["status"]["privacy_review"] == "succeeded"
        assert "points to a blocked destination" in (run_dir / "change.patch").read_text()
        assert "clarifications@v1" in arts["requirements_spec"][-1]["derived_from"]
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_resume_after_policy_stop_completes():
    run_id = "pytest-resume"
    cli.main(["run", str(cli.REPO / "scenarios" / "drills" / "policy-violation.yaml"), "--run-id", run_id, "--approvals", "auto"])
    run_dir = cli.RUNS / run_id
    try:
        assert json.loads((run_dir / "state.json").read_text())["run_status"] == "halted"
        cli.main(["resume", run_id, "--reset-failed"])
        state = json.loads((run_dir / "state.json").read_text())
        assert state["run_status"] == "succeeded"
        assert json.loads((run_dir / "metrics.json").read_text())["incidents_recovered"] >= 1
        assert AuditLog.verify(run_dir / "audit.jsonl")[0]
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

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
    rc = cli.main(["run", str(cli.REPO / "scenarios" / scenario), "--run-id", run_id, "--approvals", "simulated"])
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
    cli.main(["run", str(cli.REPO / "scenarios" / "ambiguous.yaml"), "--run-id", run_id, "--approvals", "simulated"])
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
    cli.main(["run", str(cli.REPO / "scenarios" / "drills" / "policy-violation.yaml"), "--run-id", run_id, "--approvals", "simulated"])
    run_dir = cli.RUNS / run_id
    try:
        assert json.loads((run_dir / "state.json").read_text())["run_status"] == "halted"
        cli.main(["resume", run_id, "--reset-failed", "--approvals", "simulated"])
        state = json.loads((run_dir / "state.json").read_text())
        assert state["run_status"] == "succeeded"
        assert json.loads((run_dir / "metrics.json").read_text())["incidents_recovered"] >= 1
        assert AuditLog.verify(run_dir / "audit.jsonl")[0]
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _state(run_dir):
    return json.loads((run_dir / "state.json").read_text())


def test_default_non_interactive_run_pauses_for_a_human():
    """Regression: without a terminal the CLI used to fall back to scripted auto-approval.
    Now it pauses at the first high-impact gate and nothing is committed without a person."""
    run_id = "pytest-queue"
    rc = cli.main(["run", str(cli.REPO / "scenarios" / "brownfield.yaml"), "--run-id", run_id])  # no --approvals
    run_dir = cli.RUNS / run_id
    state = _state(run_dir)
    assert rc == 0 and state["run_status"] == "awaiting_approval"
    assert list(state["pending_approvals"]) == ["intake"]
    assert state["context"]["artifacts"] == {}  # the proposal was not committed
    assert state["status"]["implement"] == "pending"

    # A person works through every gate; resume commits exactly what was approved.
    for _ in range(12):
        from orchestrator.approval import pending_requests, record_decision
        for req in pending_requests(run_dir):
            record_decision(run_dir, req["node"], "approve", "alice", "reviewed")
        cli.main(["resume", run_id, "--approvals", "queue"])
        state = _state(run_dir)
        if state["run_status"] != "awaiting_approval":
            break
    assert state["run_status"] == "succeeded"
    decisions = [json.loads(l)["data"] for l in (run_dir / "audit.jsonl").read_text().splitlines()
                 if json.loads(l)["event"] == "approval_decision"]
    assert decisions and all(d["human"] for d in decisions)
    assert state["release_signoff"]["approver"] == "alice" and state["release_signoff"]["human"] is True
    assert state["executions"]["intake"] == 1  # approved proposal committed, agent not re-run
    assert AuditLog.verify(run_dir / "audit.jsonl")[0]


def test_decision_for_a_different_proposal_is_refused():
    run_id = "pytest-queue-tamper"
    cli.main(["run", str(cli.REPO / "scenarios" / "brownfield.yaml"), "--run-id", run_id, "--approvals", "queue"])
    run_dir = cli.RUNS / run_id
    from orchestrator.approval import record_decision
    out = record_decision(run_dir, "intake", "approve", "mallory")
    d = json.loads(out.read_text())
    d["request_hash"] = "0" * 16  # decision no longer matches what was shown for review
    out.write_text(json.dumps(d))
    cli.main(["resume", run_id, "--approvals", "queue"])
    state = _state(run_dir)
    assert state["run_status"] == "halted" and "requirements_spec" not in state["context"]["artifacts"]


def test_policy_can_forbid_simulated_approvals(tmp_path):
    import yaml
    pol = yaml.safe_load((cli.REPO / "policy" / "default.yaml").read_text())
    pol["approvals"]["allow_simulated_approvals"] = False
    strict = tmp_path / "strict.yaml"
    strict.write_text(yaml.safe_dump(pol))
    with pytest.raises(ValueError, match="forbids simulated approvals"):
        cli.main(["--policy", str(strict), "run", str(cli.REPO / "scenarios" / "greenfield.yaml"),
                  "--run-id", "pytest-strict", "--approvals", "simulated"])


def test_resume_restores_the_original_llm_backend_and_refuses_silent_switches():
    """Regression: `resume` used to default to offline, silently swapping a live run onto
    recorded responses."""
    run_id = "pytest-resume-config"
    cli.main(["run", str(cli.REPO / "scenarios" / "brownfield.yaml"), "--run-id", run_id, "--approvals", "queue"])
    run_dir = cli.RUNS / run_id
    state = _state(run_dir)
    assert state["config"] == {"llm": "offline", "approvals": "queue"}
    state["config"]["llm"] = "anthropic"  # pretend the run was started live
    (run_dir / "state.json").write_text(json.dumps(state))
    assert cli.main(["resume", run_id, "--llm", "offline"]) == 2  # refused without override
    assert cli.main(["resume", run_id, "--approvals", "simulated", "--llm", "anthropic"]) == 2
    from orchestrator.approval import record_decision
    record_decision(run_dir, "intake", "approve", "alice")
    cli.main(["resume", run_id, "--llm", "offline", "--override-config"])
    log = (run_dir / "audit.jsonl").read_text()
    assert '"config_override"' in log and '"from": "anthropic"' in log


def test_long_human_review_does_not_trip_the_budget_on_resume():
    """Regression: approving a queued run after > max_wall_seconds caused a budget safe-stop."""
    run_id = "pytest-slow-review"
    cli.main(["run", str(cli.REPO / "scenarios" / "brownfield.yaml"), "--run-id", run_id, "--approvals", "queue"])
    run_dir = cli.RUNS / run_id
    state = _state(run_dir)
    state["metrics"]["started_at"] -= 5000  # the reviewer took ~83 minutes (budget is 900 s)
    (run_dir / "state.json").write_text(json.dumps(state))
    from orchestrator.approval import pending_requests, record_decision
    for _ in range(12):
        for req in pending_requests(run_dir):
            record_decision(run_dir, req["node"], "approve", "alice")
        cli.main(["resume", run_id])
        state = _state(run_dir)
        if state["run_status"] != "awaiting_approval":
            break
    assert state["run_status"] == "succeeded", state.get("stop_reason")
    m = json.loads((run_dir / "metrics.json").read_text())
    assert m["end_to_end_latency_s"] > 5000 and m["active_execution_s"] < 900

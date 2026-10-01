"""Unit tests for graph, audit, policy, scanners, provider, approval, workspace."""

from __future__ import annotations

import json

import pytest
from conftest import BASE_POLICY

from orchestrator.approval import ApprovalRequest, InteractiveApprover, SimulatedApprover
from orchestrator.audit import AuditLog
from orchestrator.graph import GraphError, WorkflowGraph
from orchestrator.llm import LLMError, OfflineProvider
from orchestrator.model import AgentResult, NodeSpec
from orchestrator.policy import Policy
from orchestrator.scanners import scan_python
from orchestrator.workspace import Workspace, WorkspaceError


# --- graph ------------------------------------------------------------------

def test_graph_rejects_cycles_and_unknown_deps():
    with pytest.raises(GraphError):
        WorkflowGraph([NodeSpec("a", "s", "x", deps=["b"]), NodeSpec("b", "s", "x", deps=["a"])])
    with pytest.raises(GraphError):
        WorkflowGraph([NodeSpec("a", "s", "x", deps=["missing"])])


def test_rework_target_must_be_upstream():
    with pytest.raises(GraphError):
        WorkflowGraph([NodeSpec("a", "s", "x", on_failure="rework:b"), NodeSpec("b", "s", "x")])


def test_parallel_levels():
    g = WorkflowGraph([NodeSpec("a", "s", "x"), NodeSpec("b", "s", "x", deps=["a"]),
                       NodeSpec("c", "s", "x", deps=["a"]), NodeSpec("d", "s", "x", deps=["b", "c"])])
    assert g.parallel_levels() == [["a"], ["b", "c"], ["d"]]


def test_insert_node_rewires_and_reverts_on_cycle():
    g = WorkflowGraph([NodeSpec("a", "s", "x"), NodeSpec("b", "s", "x", deps=["a"])])
    g.insert_node(NodeSpec("m", "s", "x"), after=["a"], before=["b"])
    assert g.nodes["b"].deps == ["a", "m"]
    with pytest.raises(GraphError):
        g.insert_node(NodeSpec("bad", "s", "x"), after=["b"], before=["a"])  # would create a cycle
    assert "bad" not in g.nodes and "bad" not in g.nodes["a"].deps


# --- audit ------------------------------------------------------------------

def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", "r1")
    for i in range(5):
        log.record("evt", n=i)
    assert AuditLog.verify(tmp_path / "a.jsonl")[0]
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    rec = json.loads(lines[2])
    rec["data"]["n"] = 99
    lines[2] = json.dumps(rec, sort_keys=True)
    (tmp_path / "a.jsonl").write_text("\n".join(lines) + "\n")
    ok, msg = AuditLog.verify(tmp_path / "a.jsonl")
    assert not ok and "modified" in msg


def test_audit_detects_deleted_record(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", "r1")
    for i in range(3):
        log.record("evt", n=i)
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    (tmp_path / "a.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not AuditLog.verify(tmp_path / "a.jsonl")[0]


def test_audit_resumes_chain_across_instances(tmp_path):
    AuditLog(tmp_path / "a.jsonl", "r").record("one")
    AuditLog(tmp_path / "a.jsonl", "r").record("two")
    ok, msg = AuditLog.verify(tmp_path / "a.jsonl")
    assert ok and msg.startswith("2 records verified")


def _truncate_last(path):
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")


def test_audit_detects_truncated_tail(tmp_path):
    """Regression: dropping the final record left a valid chain and used to verify."""
    log = AuditLog(tmp_path / "a.jsonl", "r1", key=b"")
    for i in range(3):
        log.record("evt", n=i)
    _truncate_last(tmp_path / "a.jsonl")
    ok, msg = AuditLog.verify(tmp_path / "a.jsonl", key=b"")
    assert not ok and "truncated" in msg


def test_audit_independent_head_catches_rewritten_anchor(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", "r1", key=b"")
    for i in range(3):
        log.record("evt", n=i)
    head = dict(log.head)
    _truncate_last(tmp_path / "a.jsonl")
    AuditLog(tmp_path / "a.jsonl", "r1", key=b"")._write_anchor()  # attacker re-anchors the shortened log
    assert AuditLog.verify(tmp_path / "a.jsonl", key=b"")[0]  # anchor alone is fooled...
    ok, msg = AuditLog.verify(tmp_path / "a.jsonl", key=b"", expected_head=head)
    assert not ok and "independently stored head" in msg  # ...the independent copy is not


def test_keyed_anchor_cannot_be_forged_without_key(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", "r1", key=b"secret-key")
    for i in range(3):
        log.record("evt", n=i)
    assert AuditLog.verify(tmp_path / "a.jsonl", key=b"secret-key")[0]
    _truncate_last(tmp_path / "a.jsonl")
    AuditLog(tmp_path / "a.jsonl", "r1", key=b"wrong-key")._write_anchor()  # forged without the real key
    ok, msg = AuditLog.verify(tmp_path / "a.jsonl", key=b"secret-key")
    assert not ok and "MAC invalid" in msg


def test_audit_without_anchor_is_not_reported_complete(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", "r1", key=b"")
    log.record("evt")
    (tmp_path / "a.anchor.json").unlink()
    ok, msg = AuditLog.verify(tmp_path / "a.jsonl", key=b"")
    assert not ok and "completeness cannot be established" in msg


# --- policy -----------------------------------------------------------------

def test_policy_write_scope_and_secret_detection():
    pol = Policy(BASE_POLICY)
    node = NodeSpec("impl", "s", "writer")
    findings = pol.check_changes(node, "writer", {"src/a.py": "k = 'AKIAABCDEFGHIJKLMNOP'\n", "etc/x.py": "a=1\n"})
    rules = {(f.rule, f.severity) for f in findings}
    assert ("hardcoded-secret", "critical") in rules
    assert ("autonomy-boundary", "critical") in rules
    assert pol.check_changes(node, "reader", {"src/a.py": "a=1\n"})[0].rule == "autonomy-boundary"


def test_policy_approval_reasons():
    pol = Policy({**BASE_POLICY, "approvals": {"always": ["release"], "on_impact": ["schema_change"],
                                                "on_unresolved_ambiguity": True},
                  "change_control": {"protected_paths": ["src/db.py"]}})
    r = AgentResult(impact={"schema_change", "unresolved_ambiguity"}, file_changes={"src/db.py": "x"})
    reasons = pol.approval_reasons(NodeSpec("release", "s", "x"), r)
    assert len(reasons) == 4
    assert pol.approval_reasons(NodeSpec("other", "s", "x"), AgentResult()) == []


@pytest.mark.parametrize("src,rule", [
    ("eval('1')", "forbidden-call"),
    ("import subprocess\nsubprocess.run('ls', shell=True)", "shell-injection"),
    ("conn.execute(f'SELECT * FROM t WHERE id = {user_id}')", "sql-injection"),
    ("conn.execute('SELECT * FROM t WHERE id = ' + uid)", "sql-injection"),
    ("def broken(:\n", "syntax-error"),
])
def test_scanner_flags(src, rule):
    assert rule in {f.rule for f in scan_python("m.py", src, ["eval"])}


def test_scanner_allows_constant_interpolation_and_params():
    src = "COLS = 'a, b'\nconn.execute(f'SELECT {COLS} FROM t WHERE id = ?', (x,))\n"
    assert scan_python("m.py", src, ["eval"]) == []


# --- offline provider -------------------------------------------------------

def test_offline_provider_when_filter_and_derived_variants():
    pb = {
        "design": {"tasks": [{"id": "T1"}, {"when": {"Q2": "yes"}, "id": "T2"}],
                   "notes": ["always", {"when": {"Q2": "yes"}, "text": "only if yes"}]},
        "implement": {"summary": "s", "changes": [{"op": "create", "path": "a", "content": "x < y"}]},
        "implement__buggy": {"derive_from": "implement", "substitute": [["x < y", "x <= y"]]},
    }
    p = OfflineProvider(pb, faults={"implement": {"on_executions": [1], "variant": "buggy"},
                                    "design": {"on_executions": [3], "variant": "error"}})
    no = p.generate("design", prompt="", context={"answers": {"Q2": "no"}}, execution=1)
    yes = p.generate("design", prompt="", context={"answers": {"Q2": "yes"}}, execution=1)
    assert [t["id"] for t in no["tasks"]] == ["T1"] and no["notes"] == ["always"]
    assert [t["id"] for t in yes["tasks"]] == ["T1", "T2"] and yes["notes"] == ["always", "only if yes"]
    assert p.generate("implement", prompt="", context={}, execution=1)["changes"][0]["content"] == "x <= y"
    assert p.generate("implement", prompt="", context={}, execution=2)["changes"][0]["content"] == "x < y"
    with pytest.raises(LLMError):
        p.generate("design", prompt="", context={}, execution=3)


# --- approvals --------------------------------------------------------------

def test_simulated_approver_is_labelled_non_human():
    ap = SimulatedApprover("bot", {"design": [{"decision": "revise", "answers": {"Q1": "a"}}]})
    d1 = ap.request(ApprovalRequest("design", ["r"], "", {}, round=1))
    d2 = ap.request(ApprovalRequest("design", ["r"], "", {}, round=2))
    assert (d1.decision, d2.decision) == ("revise", "approve")
    assert d1.approver == "simulated:bot" and d1.human is False and d2.comment.startswith("[SIMULATED]")


def test_interactive_approver_revise_flow():
    answers = iter(["v", "intake", "narrow scope", '{"Q2": "x"}'])
    ap = InteractiveApprover("alice", input_fn=lambda _: next(answers), print_fn=lambda *_: None)
    d = ap.request(ApprovalRequest("design", ["schema change"], "summary", {"design": {"a": 1}}))
    assert (d.decision, d.approver, d.revise_target, d.answers) == ("revise", "alice", "intake", {"Q2": "x"})


# --- workspace --------------------------------------------------------------

def test_workspace_snapshot_restore_and_path_guard(tmp_path):
    ws = Workspace.create(tmp_path, None)
    ws.apply({"src/a.py": "one\n"})
    ws.snapshot("s1")
    ws.apply({"src/a.py": "two\n"})
    assert "+two" in ws.diff("s1")
    ws.restore("s1")
    assert ws.read("src/a.py") == "one\n"
    with pytest.raises(WorkspaceError):
        ws.apply({"../escape.py": "x"})


# --- live LLM adapter (HTTP mocked; no network) ------------------------------

def _provider(monkeypatch, handler):
    import httpx

    from orchestrator.llm import AnthropicProvider
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return AnthropicProvider({"design": {"tasks": [], "approach": ""}},
                             client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_anthropic_provider_parses_fenced_json(monkeypatch):
    import httpx
    seen = {}

    def handler(req):
        seen["headers"] = req.headers
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": '```json\n{"tasks": [], "approach": "x"}\n```'}]})
    out = _provider(monkeypatch, handler).generate("design", prompt="p", context={}, execution=1)
    assert out == {"tasks": [], "approach": "x"}
    assert seen["headers"]["x-api-key"] == "test-key" and seen["body"]["messages"][0]["role"] == "user"


@pytest.mark.parametrize("status,text", [(200, "not json"), (200, '{"tasks": []}'), (529, "overloaded")])
def test_anthropic_provider_rejects_bad_responses(monkeypatch, status, text):
    import httpx
    handler = lambda req: httpx.Response(status, json={"content": [{"type": "text", "text": text}]})
    with pytest.raises(LLMError):
        _provider(monkeypatch, handler).generate("design", prompt="p", context={}, execution=1)


# --- lineage -----------------------------------------------------------------

def test_lineage_traces_an_artifact_back_to_human_input():
    from orchestrator.context import RunContext
    ctx = RunContext({})
    ctx.put("clarifications", {"Q2": "yes"}, producer="human:alice", attempt=1, derived_from=[])
    ctx.put("spec", {"a": 1}, producer="intake", attempt=1, derived_from=["clarifications@v1"])
    ctx.put("design", {"b": 2}, producer="design", attempt=1, derived_from=["spec@v1"])
    tree = ctx.lineage("design")
    assert tree["ref"] == "design@v1" and tree["producer"] == "design"
    assert tree["derived_from"][0]["derived_from"][0]["producer"] == "human:alice"


def test_keyed_verification_rejects_unsigned_anchor_downgrade(tmp_path):
    """Regression: flipping `keyed` to false in the anchor used to skip the MAC check."""
    log = AuditLog(tmp_path / "a.jsonl", "r1", key=b"secret-key")
    for i in range(3):
        log.record("evt", n=i)
    _truncate_last(tmp_path / "a.jsonl")
    forged = AuditLog(tmp_path / "a.jsonl", "r1", key=b"")  # re-anchor without a MAC
    forged._write_anchor()
    assert json.loads((tmp_path / "a.anchor.json").read_text())["keyed"] is False
    ok, msg = AuditLog.verify(tmp_path / "a.jsonl", key=b"secret-key")
    assert not ok and "downgrade" in msg

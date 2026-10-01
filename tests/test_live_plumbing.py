"""Live-LLM path end to end, with the Anthropic HTTP API mocked (no network, no key needed).

This does NOT prove model quality - it proves the plumbing a real run relies on: requests are
built correctly, generated content passes the same gates, provenance marks it `generated`,
and reference-copying is refused in live mode (including via the offline fallback).
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import yaml

from orchestrator import cli, llm
from orchestrator.audit import AuditLog


@pytest.fixture(autouse=True)
def isolated_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def _mock_model(playbook_file: str, monkeypatch) -> list[dict]:
    """A fake model that answers each task with the recorded response for that task."""
    playbook = yaml.safe_load((cli.REPO / playbook_file).read_text())
    offline = llm.OfflineProvider(playbook)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        key = re.search(r"TASK \((\w+)\)", prompt).group(1)
        seen.append({"key": key, "body": body})
        data = offline.generate(key, prompt="", context={}, execution=1)
        return httpx.Response(200, json={"id": f"msg_{len(seen)}", "model": body["model"], "stop_reason": "end_turn",
                                         "usage": {"input_tokens": 100, "output_tokens": 50},
                                         "content": [{"type": "text", "text": json.dumps(data)}]})

    real = llm.AnthropicProvider
    monkeypatch.setattr(cli, "AnthropicProvider",
                        lambda pb: real(pb, client=httpx.Client(transport=httpx.MockTransport(handler))))
    return seen


def _state(run_id):
    return json.loads((cli.RUNS / run_id / "state.json").read_text())


def test_live_brownfield_run_records_generated_provenance(monkeypatch):
    seen = _mock_model("scenarios/playbooks/brownfield.yaml", monkeypatch)
    rc = cli.main(["run", str(cli.REPO / "scenarios" / "brownfield.yaml"), "--run-id", "live-bf",
                   "--llm", "anthropic", "--approvals", "simulated"])
    state = _state("live-bf")
    assert rc == 0 and state["run_status"] == "succeeded"
    assert {s["key"] for s in seen} == {"requirements", "design", "implement"}
    impl = state["context"]["artifacts"]["change_set"][-1]["provenance"][0]
    assert impl["content_origin"] == "generated" and impl["provider"] == "anthropic"
    assert impl["response_id"] and len(impl["prompt_sha256"]) == 64 and impl["usage"]["output_tokens"] == 50
    computed = state["context"]["artifacts"]["test_report"][-1]["provenance"][0]
    assert computed["content_origin"] == "computed"
    # the live model is shown the format, not the recorded answer
    impl_prompt = next(s for s in seen if s["key"] == "implement")["body"]["messages"][0]["content"]
    assert "<truncated example>" in impl_prompt or impl_prompt.count('"op"') <= 3
    events = [json.loads(l)["event"] for l in (cli.RUNS / "live-bf" / "audit.jsonl").read_text().splitlines()]
    assert events.count("reasoning_call") >= 3
    assert AuditLog.verify(cli.RUNS / "live-bf" / "audit.jsonl")[0]


def test_live_greenfield_cannot_copy_the_reference_implementation(monkeypatch):
    """The recorded greenfield 'implementation' copies service/. In live mode that is refused -
    for the model's output and for the offline fallback - so the run stops instead of passing
    off copied code as generated."""
    _mock_model("scenarios/playbooks/greenfield.yaml", monkeypatch)
    cli.main(["run", str(cli.REPO / "scenarios" / "greenfield.yaml"), "--run-id", "live-gf",
              "--llm", "anthropic", "--approvals", "simulated"])
    state = _state("live-gf")
    assert state["run_status"] == "halted"
    assert state["status"]["implement"] == "failed"
    assert "change_set" not in state["context"]["artifacts"]
    log = (cli.RUNS / "live-gf" / "audit.jsonl").read_text()
    assert "create_from_reference is disabled in live runs" in log

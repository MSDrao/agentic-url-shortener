"""Command-line entry point.

  python -m orchestrator run scenarios/brownfield.yaml [--approvals auto|interactive] [--llm offline|anthropic]
  python -m orchestrator resume <run_id> [--reset-failed]
  python -m orchestrator stop <run_id>
  python -m orchestrator verify-audit <run_id>
  python -m orchestrator metrics
  python -m orchestrator graph scenarios/greenfield.yaml
  python -m orchestrator demo            # all scenarios + failure drills, unattended
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from . import report
from .approval import (InteractiveApprover, QueuedApprover, SimulatedApprover, pending_requests,
                       record_decision)
from .audit import AuditLog
from .context import RunContext
from .engine import Engine
from .llm import AnthropicProvider, LLMError, OfflineProvider
from .metrics import aggregate
from .model import RunStatus
from .pipeline import build_agents, build_graph, node_templates
from .policy import Policy
from .workspace import Workspace

REPO = Path(__file__).resolve().parent.parent
MODES = ["interactive", "queue", "simulated"]
RUNS = Path(os.environ.get("ORCH_RUNS_DIR", REPO / "runs"))


def load_scenario(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if "extends" in data:
        base = load_scenario((path.parent / data.pop("extends")).resolve())
        base.update(data)
        data = base
    data["_path"] = str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)
    return data


def _setup(scenario: dict[str, Any], run_id: str, run_dir: Path, llm_mode: str, approvals: str,
           policy_path: Path, max_parallel: int, fresh: bool) -> Engine:
    playbook = yaml.safe_load((REPO / scenario["playbook"]).read_text())
    offline = OfflineProvider(playbook, faults=scenario.get("faults", {}), source=scenario["playbook"])
    primary = AnthropicProvider(playbook) if llm_mode == "anthropic" else offline
    policy = Policy.load(policy_path)
    if fresh:
        baseline = REPO / scenario["baseline"] if scenario.get("baseline") else None
        workspace = Workspace.create(run_dir, baseline)
    else:
        workspace = Workspace(run_dir / "workspace", run_dir / "snapshots")
    if approvals == "interactive":
        approver = InteractiveApprover()
    elif approvals == "queue":
        approver = QueuedApprover(run_dir)
    elif approvals == "simulated":
        approver = SimulatedApprover(role=scenario.get("simulated_role", "reviewer"),
                                     script=scenario.get("simulated_approvals", {}))
    else:
        raise ValueError(f"unknown approvals mode '{approvals}'")
    return Engine(
        run_id=run_id, run_dir=run_dir, repo_root=REPO, scenario=scenario,
        graph=build_graph(scenario, with_fallbacks=llm_mode != "offline"),
        ctx=RunContext(scenario), agents=build_agents(primary, offline), llm=primary,
        policy=policy, approver=approver, workspace=workspace, node_templates=node_templates(),
        max_parallel=max_parallel,
    )


def cmd_run(args) -> int:
    scenario = load_scenario(Path(args.scenario).resolve())
    run_id = args.run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{scenario['id']}"
    run_dir = RUNS / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    print(f"\n▶ {scenario['title']}  [{scenario['type']}]  run={run_id}  llm={args.llm}  approvals={args.approvals}")
    if args.approvals == "simulated":
        print("  ⚠ SIMULATED approvals: scripted stand-ins, recorded as human=false. NOT a release authorization.")
    engine = _setup(scenario, run_id, run_dir, args.llm, args.approvals, Path(args.policy), args.max_parallel, fresh=True)
    print("  waves: " + " | ".join(", ".join(w) for w in engine.graph.parallel_levels()))
    status = engine.run()
    return _finish(run_dir, status)


def cmd_resume(args) -> int:
    run_dir = RUNS / args.run_id
    state = json.loads((run_dir / "state.json").read_text())
    (run_dir / "STOP").unlink(missing_ok=True)
    scenario = state["scenario"]
    saved = state.get("config", {})
    llm_mode, approvals, changes = _resume_config(saved, args)
    if changes and not args.override_config:
        print("error: resume would change the run's execution configuration: "
              + "; ".join(f"{k}: {a} -> {b}" for k, a, b in changes)
              + ". Re-run with --override-config to do this deliberately (it is recorded in the audit log).",
              file=sys.stderr)
        return 2
    engine = _setup(scenario, args.run_id, run_dir, llm_mode, approvals, Path(args.policy), args.max_parallel, fresh=False)
    if changes:
        engine.audit.record("config_override", actor=getpass.getuser(),
                            changes=[{"setting": k, "from": a, "to": b} for k, a, b in changes])
    engine.ctx = RunContext.from_dict(scenario, state["context"])
    engine.restore_state(state, reset_failed=args.reset_failed)
    print(f"\n▶ resuming {args.run_id} (previous stop: {state.get('stop_reason')})")
    return _finish(run_dir, engine.run())


def _resume_config(saved: dict, args) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Resume with the run's original LLM backend and approval mode. Moving between the two human
    modes (interactive <-> queue) is allowed; anything else - e.g. live -> offline, or
    human -> simulated approvals - is a configuration change that needs an explicit override."""
    saved_llm = saved.get("llm", "offline")
    saved_appr = saved.get("approvals")
    llm_mode = args.llm or saved_llm
    human_default = "interactive" if sys.stdin.isatty() else "queue"
    if args.approvals:
        approvals = args.approvals
    elif saved_appr == "simulated":
        approvals = "simulated"
    else:
        approvals = human_default
    changes = []
    if llm_mode != saved_llm:
        changes.append(("llm", saved_llm, llm_mode))
    if saved_appr and (approvals == "simulated") != (saved_appr == "simulated"):
        changes.append(("approvals", saved_appr, approvals))
    return llm_mode, approvals, changes


def _finish(run_dir: Path, status: RunStatus) -> int:
    summary = report.write(run_dir)
    metrics = json.loads((run_dir / "metrics.json").read_text())
    print(f"\n■ run {status.value.upper()}  latency={metrics['end_to_end_latency_s']}s  attempts={metrics['attempts']}  "
          f"retries={metrics['retries']}  rollbacks={metrics['rollbacks']}  replans={metrics['replans']}  mttr={'n/a' if metrics['mttr_s'] is None else str(metrics['mttr_s']) + 's'}")
    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(REPO))
        except ValueError:  # runs dir outside the repo (ORCH_RUNS_DIR)
            return str(p)
    print(f"  summary: {rel(summary)}\n  patch:   {rel(run_dir / 'change.patch')}")
    if status == RunStatus.AWAITING_APPROVAL:
        for req in pending_requests(run_dir):
            print(f"\n  ⏸ awaiting human decision on '{req['node']}' (round {req['round']}, request {req['request_hash']})")
            for r in req["reasons"]:
                print(f"     - {r}")
        rid = run_dir.name
        print(f"\n  review:  python -m orchestrator pending {rid}\n"
              f"  decide:  python -m orchestrator approve {rid} <node> --decision approve|reject|revise [--comment ...]\n"
              f"  resume:  python -m orchestrator resume {rid}")
    return 0 if status in (RunStatus.SUCCEEDED, RunStatus.ROLLED_BACK, RunStatus.HALTED,
                           RunStatus.AWAITING_APPROVAL) else 1


def cmd_pending(args) -> int:
    reqs = pending_requests(RUNS / args.run_id)
    if not reqs:
        print("no pending approvals")
    for req in reqs:
        print(f"\n=== {req['node']} (round {req['round']}, request {req['request_hash']}) ===")
        for r in req["reasons"]:
            print(f"  - {r}")
        print(f"\n{req['summary']}\n")
        for k, v in req["evidence"].items():
            text = v if isinstance(v, str) else json.dumps(v, indent=2, default=str)
            print(f"--- {k} ---\n{text[:4000]}")
        print(f"\n(full request: {req['file']})")
    return 0


def cmd_approve(args) -> int:
    answers = json.loads(args.answers) if args.answers else {}
    out = record_decision(RUNS / args.run_id, args.node, args.decision, args.approver or getpass.getuser(),
                          args.comment or "", args.target, answers)
    print(f"recorded '{args.decision}' for '{args.node}' ({out.name}); continue with: "
          f"python -m orchestrator resume {args.run_id}")
    return 0


def cmd_stop(args) -> int:
    (RUNS / args.run_id / "STOP").write_text(f"stop requested at {time.ctime()}\n")
    print("kill switch set; the run will safe-stop before dispatching further work")
    return 0


def cmd_verify(args) -> int:
    expected = None
    history = RUNS / "history.jsonl"
    if history.exists():
        for line in history.read_text().splitlines():
            rec = json.loads(line) if line.strip() else {}
            if rec.get("run_id") == args.run_id and rec.get("audit_head"):
                expected = rec["audit_head"]  # last entry wins (resumed runs append again)
    ok, msg = AuditLog.verify(RUNS / args.run_id / "audit.jsonl", expected_head=expected)
    print(("OK: " if ok else "TAMPERED: ") + msg)
    return 0 if ok else 2


def cmd_metrics(args) -> int:
    print(json.dumps(aggregate(RUNS / "history.jsonl"), indent=2))
    return 0


def cmd_graph(args) -> int:
    scenario = load_scenario(Path(args.scenario).resolve())
    print(build_graph(scenario, with_fallbacks=False).to_mermaid())
    return 0


def cmd_demo(args) -> int:
    scen = REPO / "scenarios"
    plan = [("greenfield.yaml", "demo-greenfield"), ("brownfield.yaml", "demo-brownfield"),
            ("ambiguous.yaml", "demo-ambiguous"),
            ("drills/release-rejected.yaml", "drill-release-rejected"),
            ("drills/policy-violation.yaml", "drill-policy-violation"),
            ("drills/rework-exhausted.yaml", "drill-rework-exhausted")]
    results = []
    for file, run_id in plan:
        ns = argparse.Namespace(scenario=str(scen / file), run_id=run_id, llm="offline", approvals="simulated",
                                policy=args.policy, max_parallel=args.max_parallel)
        rc = cmd_run(ns)
        state = json.loads((RUNS / run_id / "state.json").read_text())
        results.append((run_id, state["run_status"], rc))
    # Recovery drill: resume the safe-stopped run after the (one-off) violation.
    print("\n▶ recovering drill-policy-violation via resume --reset-failed")
    cmd_resume(argparse.Namespace(run_id="drill-policy-violation", reset_failed=True, llm=None,
                                  approvals=None, override_config=False, policy=args.policy, max_parallel=args.max_parallel))
    results = [(r, json.loads((RUNS / r / "state.json").read_text())["run_status"], rc) for r, _, rc in results]
    print("\n=== demo results ===")
    for run_id, st, rc in results:
        print(f"  {run_id:<28} {st}")
    cmd_metrics(args)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator", description="Governed agentic SDLC orchestrator")
    p.add_argument("--policy", default=str(REPO / "policy" / "default.yaml"))
    p.add_argument("--max-parallel", type=int, default=4)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("scenario")
    r.add_argument("--run-id")
    r.add_argument("--llm", choices=["offline", "anthropic"], default="offline")
    r.add_argument("--approvals", choices=MODES, default="interactive" if sys.stdin.isatty() else "queue",
                   help="interactive: decide at the terminal; queue: pause at each gate for "
                        "'orchestrator approve'; simulated: scripted demo decisions (never a real sign-off)")
    r.set_defaults(fn=cmd_run)
    rs = sub.add_parser("resume")
    rs.add_argument("run_id")
    rs.add_argument("--reset-failed", action="store_true")
    rs.add_argument("--llm", choices=["offline", "anthropic"], default=None,
                    help="default: the backend the run started with")
    rs.add_argument("--approvals", choices=MODES, default=None,
                    help="default: the run's original mode (human modes may switch freely)")
    rs.add_argument("--override-config", action="store_true",
                    help="allow resuming with a different LLM backend / simulated-vs-human approvals (audited)")
    rs.set_defaults(fn=cmd_resume)
    ap = sub.add_parser("approve", help="record a human decision for a paused run")
    ap.add_argument("run_id")
    ap.add_argument("node")
    ap.add_argument("--decision", choices=["approve", "reject", "revise"], required=True)
    ap.add_argument("--comment")
    ap.add_argument("--target", help="revise: node to send the work back to")
    ap.add_argument("--answers", help='revise: JSON answers, e.g. \'{"Q2": "create_and_redirect"}\'')
    ap.add_argument("--as", dest="approver", help="approver identity (default: OS user)")
    ap.set_defaults(fn=cmd_approve)
    for name, fn in (("stop", cmd_stop), ("verify-audit", cmd_verify), ("pending", cmd_pending)):
        s = sub.add_parser(name)
        s.add_argument("run_id")
        s.set_defaults(fn=fn)
    sub.add_parser("metrics").set_defaults(fn=cmd_metrics)
    g = sub.add_parser("graph")
    g.add_argument("scenario")
    g.set_defaults(fn=cmd_graph)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except LLMError as exc:  # e.g. --llm anthropic without ANTHROPIC_API_KEY
        print(f"error: {exc}", file=sys.stderr)
        return 2

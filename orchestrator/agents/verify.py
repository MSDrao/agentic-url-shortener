"""Verification agents: run the test suite for real, and scan the change for security issues."""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET

from .. import sandbox
from ..model import AgentResult
from ..sandbox import SandboxConfig, SandboxError
from ..scanners import Finding, max_severity, scan_pii_columns, scan_python, scan_secrets
from .base import Agent, AgentError, AgentTask, InfrastructureError


class TestRunnerAgent(Agent):
    name = "test_runner"
    boundary = "executes pytest in the isolated workspace; read-only"

    def run(self, task: AgentTask) -> AgentResult:
        out = task.run_dir / "test-output" / f"exec{task.execution}-attempt{task.attempt}"
        out.mkdir(parents=True, exist_ok=True)
        junit, cov = out / "junit.xml", out / "coverage.json"
        cmd = [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--junitxml={junit}", "--cov=shortener", f"--cov-report=json:{cov}", "tests",
        ]
        # Generated code is untrusted: run it in the sandbox (no inherited secrets, no network,
        # resource limits; container isolation with the docker backend).
        cfg = SandboxConfig.from_policy(task.policy.section("sandbox"))
        try:
            proc = sandbox.run(cmd, workspace=task.workspace.root, out_dir=out, cfg=cfg,
                               extra_env={"COVERAGE_FILE": str(out / ".coverage"),
                                          "SHORTENER_DB_PATH": str(out / "unused.db")})
        except SandboxError as exc:
            raise InfrastructureError(str(exc)) from exc
        if proc.timed_out:
            raise AgentError(f"test run exceeded the sandbox timeout ({cfg.timeout_seconds}s)")
        (out / "stdout.txt").write_text(proc.stdout + proc.stderr)
        if not junit.exists():
            raise InfrastructureError(f"pytest produced no report (exit {proc.returncode}): {proc.stdout[-500:]}{proc.stderr[-500:]}")

        root = ET.parse(junit).getroot()
        cases, failures = [], []
        for tc in root.iter("testcase"):
            file = tc.get("classname", "").replace(".", "/") + ".py"
            name = re.sub(r"\[.*\]$", "", tc.get("name", ""))
            ref = f"{file}::{name}"
            bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
            skipped = tc.find("skipped") is not None
            cases.append((ref, bad is None and not skipped))
            if bad is not None:
                failures.append({"test": f"{ref}", "message": (bad.get("message") or "")[:400]})
        failed_refs = {r for r, ok in cases if not ok}
        passed_tests = sorted({r for r, ok in cases if ok} - failed_refs)
        coverage = None
        if cov.exists():
            coverage = round(json.loads(cov.read_text())["totals"]["percent_covered"], 2)
        n_fail = sum(1 for tc in root.iter("testcase") if tc.find("failure") is not None)
        n_err = sum(1 for tc in root.iter("testcase") if tc.find("error") is not None)
        report = {
            "total": len(cases),
            "passed": sum(ok for _, ok in cases),
            "failed": n_fail,
            "errors": n_err,
            "coverage": coverage,
            "failures": failures,
            "passed_tests": passed_tests,
            "command": " ".join(cmd[2:]).replace(str(task.run_dir), "<run_dir>"),
            "exit_code": proc.returncode,
            "sandbox": proc.isolation,
        }
        return AgentResult(
            artifacts={"test_report": report},
            summary=f"{report['passed']}/{report['total']} passed, coverage={coverage}%",
        )


class SecurityScannerAgent(Agent):
    name = "security_scanner"
    boundary = "read-only static analysis of changed files"

    def run(self, task: AgentTask) -> AgentResult:
        ws = task.workspace
        sec = task.policy.section("security")
        comp = task.policy.section("compliance")
        changed = [p for p in ws.changed_files("baseline") if not p.startswith("docs/")]
        findings: list[Finding] = []
        for path in changed:
            content = ws.read(path)
            if content is None:
                continue
            findings += scan_secrets(path, content, sec.get("secret_patterns", []))
            if path.endswith(".py"):
                # Tests execute too, so dangerous calls in tests matter as much as in app code.
                findings += scan_python(path, content, sec.get("forbidden_calls", []))
                if not path.startswith("tests/"):
                    findings += scan_pii_columns(path, content, comp.get("pii_fields", []))
            if path in ("requirements.txt", "pyproject.toml"):
                findings.append(Finding("dependency-change", "medium", path, "dependency manifest changed: supply-chain review"))
        # Security-relevant tests must still exist (defence against a change deleting them).
        required = sec.get("required_security_tests", [])
        test_src = "\n".join(ws.read(p) or "" for p in ws.files("tests/*.py"))
        for name in required:
            if f"def {name}" not in test_src:
                findings.append(Finding("security-test-missing", "high", "tests/", f"required security test '{name}' not found"))
        report = {
            "files_scanned": changed,
            "findings": [f.to_dict() for f in findings],
            "max_severity": max_severity(findings),
            "rules": ["hardcoded-secret", "forbidden-call", "shell-injection", "sql-injection", "pii-column",
                      "dependency-change", "security-test-missing"],
        }
        return AgentResult(
            artifacts={"security_report": report},
            summary=f"{len(changed)} files scanned, {len(findings)} findings (max={report['max_severity']})",
        )

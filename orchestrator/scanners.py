"""Static checks shared by the policy engine (pre-commit) and the security agent."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    detail: str
    line: int | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def scan_secrets(path: str, content: str, patterns: list[str]) -> list[Finding]:
    out = []
    for lineno, line in enumerate(content.splitlines(), 1):
        for pat in patterns:
            if re.search(pat, line):
                out.append(Finding("hardcoded-secret", "critical", path, f"matches /{pat}/", lineno))
    return out


def _call_name(node: ast.Call) -> str:
    f = node.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def scan_python(path: str, content: str, forbidden_calls: list[str]) -> list[Finding]:
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError as exc:
        return [Finding("syntax-error", "high", path, str(exc.msg), exc.lineno)]
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in forbidden_calls:
            out.append(Finding("forbidden-call", "high", path, f"call to {name}()", node.lineno))
        if name.endswith("subprocess.run") or name.endswith("Popen"):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    out.append(Finding("shell-injection", "high", path, "subprocess with shell=True", node.lineno))
        # SQL built by string interpolation of non-constant values -> injection risk.
        if name.endswith(".execute") and node.args:
            q = node.args[0]
            if isinstance(q, ast.JoinedStr):
                for v in q.values:
                    if isinstance(v, ast.FormattedValue):
                        ok = isinstance(v.value, ast.Name) and v.value.id.isupper()
                        if not ok:
                            out.append(Finding("sql-injection", "high", path, "SQL f-string interpolates a non-constant value", node.lineno))
            elif isinstance(q, ast.BinOp) and isinstance(q.op, (ast.Add, ast.Mod)):
                out.append(Finding("sql-injection", "high", path, "SQL built by string concatenation/formatting", node.lineno))
    return out


def scan_pii_columns(path: str, content: str, pii_fields: list[str]) -> list[Finding]:
    """Flag new schema columns whose names suggest personal data."""
    out = []
    for lineno, line in enumerate(content.splitlines(), 1):
        m = re.search(r"ADD COLUMN\s+(\w+)|^\s+(\w+)\s+(TEXT|INTEGER|BLOB)", line, re.IGNORECASE)
        if not m:
            continue
        col = (m.group(1) or m.group(2) or "").lower()
        if col in pii_fields:
            out.append(Finding("pii-column", "high", path, f"column '{col}' stores personal data", lineno))
    return out


def max_severity(findings: list[Finding]) -> str:
    if not findings:
        return "info"
    return max((f.severity for f in findings), key=lambda s: SEVERITY_ORDER[s])

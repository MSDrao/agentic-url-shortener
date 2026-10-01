"""Documentation agent: API reference from the live OpenAPI contract, changelog, ADRs."""

from __future__ import annotations

import json
import re
import sys
from datetime import date

from .. import sandbox
from ..model import AgentResult
from ..sandbox import SandboxConfig, SandboxError
from .base import Agent, AgentError, AgentTask, InfrastructureError

OPENAPI_SNIPPET = (
    "import json, tempfile, os\n"
    "from shortener.api import create_app\n"
    "from shortener.config import Settings\n"
    "d = tempfile.mkdtemp()\n"
    "print(json.dumps(create_app(Settings(database_path=os.path.join(d, 'x.db'))).openapi()))\n"
)


def _schema_fields(openapi: dict, ref: str | None) -> list[str]:
    if not ref:
        return []
    name = ref.split("/")[-1]
    schema = openapi.get("components", {}).get("schemas", {}).get(name, {})
    req = set(schema.get("required", []))
    out = []
    for field, spec in schema.get("properties", {}).items():
        typ = spec.get("type") or "/".join(s.get("type", s.get("$ref", "?").split("/")[-1]) for s in spec.get("anyOf", []))
        out.append(f"`{field}` ({typ}{', required' if field in req else ''}) {spec.get('description', '')}".rstrip())
    return out


def render_api_md(openapi: dict) -> tuple[str, list[str]]:
    lines = [f"# {openapi['info']['title']} API reference", "",
             f"Version {openapi['info']['version']}. Generated from the OpenAPI contract - do not edit by hand.", ""]
    routes = []
    for path, ops in openapi["paths"].items():
        for method, op in ops.items():
            routes.append(f"{method.upper()} {path}")
            lines += [f"## `{method.upper()} {path}`", ""]
            if op.get("tags"):
                lines.append(f"Tags: {', '.join(op['tags'])}")
            params = [f"`{p['name']}` ({p['in']})" for p in op.get("parameters", [])]
            if params:
                lines.append(f"Parameters: {', '.join(params)}")
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
            fields = _schema_fields(openapi, body.get("$ref"))
            if fields:
                lines += ["", "Request body:"] + [f"- {f}" for f in fields]
            lines += ["", "Responses: " + ", ".join(f"`{c}`" for c in op.get("responses", {})), ""]
    return "\n".join(lines) + "\n", routes


class TechWriterAgent(Agent):
    name = "tech_writer"
    boundary = "may propose writes only within policy write_scopes.tech_writer (docs/, CHANGELOG.md)"

    def run(self, task: AgentTask) -> AgentResult:
        ws = task.workspace
        # Importing the app executes generated code, so it runs in the sandbox too.
        cfg = SandboxConfig.from_policy(task.policy.section("sandbox"))
        out_dir = task.run_dir / "docs-output" / f"exec{task.execution}-attempt{task.attempt}"
        try:
            proc = sandbox.run([sys.executable, "-c", OPENAPI_SNIPPET], workspace=ws.root, out_dir=out_dir, cfg=cfg)
        except SandboxError as exc:
            raise InfrastructureError(str(exc)) from exc
        if proc.returncode != 0 or proc.timed_out:
            raise AgentError(f"could not load OpenAPI contract: {proc.stderr[-600:]}")
        openapi = json.loads(proc.stdout)
        api_md, routes = render_api_md(openapi)

        spec = task.inputs["requirements_spec"]
        design = task.inputs["design"]
        title = design.get("title", spec["problem_statement"][:60])
        entry = [f"## [Unreleased] - {date.today().isoformat()}", "", f"### {title}", ""]
        entry += [f"- {c}" for c in design.get("changelog", spec.get("functional", []))]
        assumed = [a for a in spec.get("ambiguities", []) if a["status"] == "assumed"]
        if assumed:
            entry += ["", "Assumptions (not confirmed by stakeholders):"] + [f"- {a['id']}: {a['answer']}" for a in assumed]
        existing = ws.read("CHANGELOG.md") or "# Changelog\n\n"
        head, _, rest = existing.partition("\n\n")
        changelog = f"{head}\n\n" + "\n".join(entry) + "\n\n" + rest

        changes = {"docs/API.md": api_md, "CHANGELOG.md": changelog.rstrip() + "\n"}
        existing_adrs = [p for p in ws.files("docs/adr/*.md")]
        n = len(existing_adrs) + 1
        for alt in design.get("alternatives_considered", []):
            slug = re.sub(r"[^a-z0-9]+", "-", alt["option"].lower()).strip("-")[:40]
            changes[f"docs/adr/ADR-{n:03d}-{slug}.md"] = (
                f"# ADR-{n:03d}: {alt['option']}\n\nStatus: accepted\n\n## Decision\n{alt['decision']}\n\n"
                f"## Rationale\n{alt.get('why', '')}\n\n## Alternatives\n"
                + "\n".join(f"- {o}" for o in alt.get("rejected", [])) + "\n"
            )
            n += 1
        documented = set(re.findall(r"^## `(\w+ \S+)`", api_md, re.M))
        report = {"routes": len(routes), "undocumented_routes": sorted(set(routes) - documented),
                  "files": sorted(changes)}
        return AgentResult(file_changes=changes, artifacts={"docs_report": report},
                           summary=f"API.md ({len(routes)} routes), CHANGELOG, {n - len(existing_adrs) - 1} ADRs")

"""Codebase analysis agent (brownfield): real static analysis of the workspace.

Builds a module import graph, extracts HTTP routes, DB tables and symbols via
the Python AST, then scores modules against the requirement's domain terms and
expands to reverse dependents to estimate blast radius.
"""

from __future__ import annotations

import ast
import math
import re
from collections import defaultdict

from ..model import AgentResult
from .base import Agent, AgentTask

STOP = set(
    "the a an and or to of for in on with must should can be is are it its this that when once from by as at "
    "not no new existing all any each their get has have after before use used may which return returns without "
    "keep make add show only first other than then also into over under".split()
)
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _split_ident(name: str) -> set[str]:
    parts = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower().split("_")
    return {p for p in parts if len(p) >= 3}


def _stem(word: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word


class CodebaseAnalysisAgent(Agent):
    name = "codebase_analyst"
    boundary = "read-only static analysis of the workspace"

    def run(self, task: AgentTask) -> AgentResult:
        ws = task.workspace
        spec = task.inputs["requirements_spec"]
        text = " ".join(spec.get("functional", []) + [spec.get("problem_statement", "")])
        terms = {_stem(w) for w in re.findall(r"[a-zA-Z_]{3,}", text.lower()) if w not in STOP}
        terms |= {_stem(t.lower()) for t in spec.get("domain_terms", [])}

        modules: dict[str, dict] = {}
        imports: dict[str, set[str]] = defaultdict(set)
        routes, tables = [], {}
        for path in ws.files("*.py"):
            src = ws.read(path) or ""
            tree = ast.parse(src)
            mod = path[:-3].replace("/", ".")
            idents: set[str] = set()
            symbols = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    symbols.append(node.name)
                    idents |= _split_ident(node.name)
                    for dec in getattr(node, "decorator_list", []):
                        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                                and dec.func.attr in HTTP_METHODS and dec.args
                                and isinstance(dec.args[0], ast.Constant)):
                            routes.append({"method": dec.func.attr.upper(), "path": dec.args[0].value,
                                           "handler": f"{mod}.{node.name}"})
                elif isinstance(node, ast.Name):
                    idents |= _split_ident(node.id)
                elif isinstance(node, ast.Attribute):
                    idents |= _split_ident(node.attr)
                elif isinstance(node, ast.arg):
                    idents |= _split_ident(node.arg)
                elif isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
                    pkg = mod.rsplit(".", 1)[0]
                    imports[mod].add(f"{pkg}.{node.module}")
                elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("shortener"):
                    imports[mod].add(node.module)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for m in re.finditer(r"CREATE TABLE\s+(\w+)\s*\(([^;]*?)\)\s*$", node.value, re.S | re.M):
                        cols = [c.strip().split()[0] for c in m.group(2).split(",\n") if c.strip()]
                        tables[m.group(1)] = cols
                    for w in re.findall(r"[a-z_]{3,}", node.value.lower()):
                        idents |= _split_ident(w)
            stems = {_stem(i) for i in idents}
            modules[mod] = {"path": path, "symbols": symbols, "matched": sorted(stems & terms)}

        reverse: dict[str, set[str]] = defaultdict(set)
        for src_mod, targets in imports.items():
            for t in targets:
                reverse[t].add(src_mod)

        # Relevance = TF-IDF-style score: terms present in every module (e.g. "link") carry
        # little signal; the spec's domain terms are weighted up.
        src_mods = [m for m in modules if not m.startswith("tests.")]
        df: dict[str, int] = defaultdict(int)
        for m in src_mods:
            for t in modules[m]["matched"]:
                df[t] += 1
        domain = {_stem(t.lower()) for t in spec.get("domain_terms", [])}
        n = len(src_mods)
        for m in src_mods:
            modules[m]["score"] = round(sum(
                math.log((n + 1) / (df[t] + 1)) * (2.0 if t in domain else 1.0) for t in modules[m]["matched"]
            ), 2)
        top = max((modules[m]["score"] for m in src_mods), default=0.0)
        threshold = 0.35 * top
        direct = {m for m in src_mods if top > 0 and modules[m]["score"] >= threshold}
        impacted = []
        for m in sorted(direct, key=lambda k: -modules[k]["score"]):
            key_terms = sorted(modules[m]["matched"], key=lambda t: df[t])[:8]
            impacted.append({"module": m, "path": modules[m]["path"], "reason": "direct",
                             "matched_terms": key_terms, "score": modules[m]["score"]})
        indirect = set()
        for m in direct:
            indirect |= reverse.get(m, set())
        for m in sorted(indirect - direct):
            impacted.append({"module": m, "path": modules[m]["path"], "reason": f"imports {sorted(imports[m] & direct)}",
                             "matched_terms": [], "score": modules[m].get("score", 0)})

        # data flow: follow imports from the HTTP layer down to persistence
        flow, cur, seen = [], "shortener.api", set()
        order = ["shortener.service", "shortener.repository", "shortener.db"]
        while cur in modules and cur not in seen:
            seen.add(cur)
            flow.append(cur)
            nxt = [o for o in order if o in imports.get(cur, set()) and o not in seen]
            cur = nxt[0] if nxt else ""

        tests_touching = {m: sorted(t for t in reverse.get(m, set()) if t.startswith("tests.")) for m in direct}
        schema_hit = any(i["module"] == "shortener.db" for i in impacted)
        api_hit = any(i["module"] in ("shortener.api", "shortener.schemas") for i in impacted)
        risk = "high" if schema_hit and api_hit else "medium" if (schema_hit or api_hit) else "low"
        analysis = {
            "terms": sorted(terms),
            "modules_scanned": len(modules),
            "routes": routes,
            "tables": tables,
            "import_graph": {k: sorted(v) for k, v in sorted(imports.items())},
            "impacted": impacted,
            "relevance_threshold": round(threshold, 2),
            "not_impacted": sorted(set(src_mods) - direct - indirect),
            "data_flow": flow,
            "tests_touching": tests_touching,
            "risk": risk,
            "risk_rationale": ("schema and public API both impacted" if risk == "high"
                               else "one contract surface impacted" if risk == "medium" else "internal only"),
        }
        return AgentResult(
            artifacts={"impact_analysis": analysis},
            summary=f"{len(modules)} modules, {len(routes)} routes, {len(tables)} tables; "
                    f"{len(direct)} directly + {len(indirect - direct)} indirectly impacted; risk={risk}",
        )

"""Explicit dependency graph (DAG) of SDLC nodes, with runtime mutation for re-planning."""

from __future__ import annotations

from collections import deque

from .model import NodeSpec


class GraphError(ValueError):
    pass


class WorkflowGraph:
    def __init__(self, nodes: list[NodeSpec] | None = None):
        self.nodes: dict[str, NodeSpec] = {}
        for n in nodes or []:
            self.nodes[n.id] = n
        self.validate()

    # --- structure ------------------------------------------------------
    def validate(self) -> None:
        for n in self.nodes.values():
            for d in n.deps:
                if d not in self.nodes:
                    raise GraphError(f"node '{n.id}' depends on unknown node '{d}'")
            if n.on_failure.startswith("rework:"):
                target = n.on_failure.split(":", 1)[1]
                if target not in self.nodes:
                    raise GraphError(f"node '{n.id}' reworks unknown node '{target}'")
                if n.id not in self.descendants(target):
                    raise GraphError(f"rework target '{target}' must be upstream of '{n.id}'")
        self.topological_order()  # raises on cycles

    def topological_order(self) -> list[str]:
        indeg = {k: 0 for k in self.nodes}
        for n in self.nodes.values():
            for _ in n.deps:
                indeg[n.id] += 1
        queue = deque(sorted(k for k, v in indeg.items() if v == 0))
        order: list[str] = []
        while queue:
            k = queue.popleft()
            order.append(k)
            for child in sorted(self.children(k)):
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
        if len(order) != len(self.nodes):
            raise GraphError("dependency graph contains a cycle")
        return order

    def children(self, node_id: str) -> list[str]:
        return [n.id for n in self.nodes.values() if node_id in n.deps]

    def descendants(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            for c in self.children(stack.pop()):
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        return seen

    def parallel_levels(self) -> list[list[str]]:
        """Group nodes into waves that can run concurrently (for docs / planning)."""
        level: dict[str, int] = {}
        for k in self.topological_order():
            deps = self.nodes[k].deps
            level[k] = 1 + max((level[d] for d in deps), default=-1)
        waves: dict[int, list[str]] = {}
        for k, lv in level.items():
            waves.setdefault(lv, []).append(k)
        return [sorted(waves[i]) for i in sorted(waves)]

    # --- re-planning ----------------------------------------------------
    def insert_node(self, spec: NodeSpec, after: list[str], before: list[str]) -> None:
        """Insert a node between `after` and `before`, rewiring edges. Validates the result."""
        if spec.id in self.nodes:
            raise GraphError(f"node '{spec.id}' already exists")
        for a in after + before:
            if a not in self.nodes:
                raise GraphError(f"unknown anchor node '{a}'")
        spec.deps = sorted(set(spec.deps) | set(after))
        self.nodes[spec.id] = spec
        for b in before:
            self.nodes[b].deps = sorted(set(self.nodes[b].deps) | {spec.id})
        try:
            self.validate()
        except GraphError:
            for b in before:
                self.nodes[b].deps = [d for d in self.nodes[b].deps if d != spec.id]
            del self.nodes[spec.id]
            raise

    def to_mermaid(self, statuses: dict[str, str] | None = None) -> str:
        lines = ["flowchart LR"]
        for k in self.topological_order():
            n = self.nodes[k]
            label = f"{k}<br/><small>{n.stage}</small>"
            if statuses and k in statuses:
                label += f"<br/><small>[{statuses[k]}]</small>"
            shape = f'{{{{"{label}"}}}}' if n.dynamic else f'["{label}"]'
            lines.append(f"    {k}{shape}")
        for n in self.nodes.values():
            for d in n.deps:
                lines.append(f"    {d} --> {n.id}")
            if n.on_failure.startswith("rework:"):
                lines.append(f"    {n.id} -. rework .-> {n.on_failure.split(':', 1)[1]}")
        return "\n".join(lines)

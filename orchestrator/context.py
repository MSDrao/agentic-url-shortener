"""Cross-stage run context: versioned artifacts plus decision lineage.

Every artifact version records the exact upstream artifact versions it was
derived from, so any output can be traced back to the requirement and the
human decisions that shaped it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .model import content_hash


@dataclass
class ArtifactVersion:
    name: str
    version: int
    hash: str
    producer: str
    attempt: int
    derived_from: list[str]  # ["requirements_spec@v2", ...]
    created_at: float
    content: Any
    # How the content was produced: recorded playbook / live model (+ hashes) / computed by agent code.
    provenance: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Decision:
    id: str
    node: str
    kind: str  # assumption | design_choice | approval | replan | rollback | fallback | safe_stop
    summary: str
    actor: str  # agent name or human approver id
    rationale: str = ""
    refs: list[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)


class RunContext:
    def __init__(self, scenario: dict[str, Any]):
        self.scenario = scenario
        self.artifacts: dict[str, list[ArtifactVersion]] = {}
        self.decisions: list[Decision] = []
        self.feedback: dict[str, list[str]] = {}  # node -> feedback for its next attempt
        self._lock = threading.Lock()

    # --- artifacts ------------------------------------------------------
    def put(self, name: str, content: Any, producer: str, attempt: int, derived_from: list[str],
            provenance: list[dict[str, Any]] | None = None) -> ArtifactVersion:
        with self._lock:
            versions = self.artifacts.setdefault(name, [])
            av = ArtifactVersion(
                name=name,
                version=len(versions) + 1,
                hash=content_hash(content),
                producer=producer,
                attempt=attempt,
                derived_from=derived_from,
                created_at=time.time(),
                content=content,
                provenance=provenance or [],
            )
            versions.append(av)
            return av

    def latest(self, name: str) -> ArtifactVersion | None:
        versions = self.artifacts.get(name)
        return versions[-1] if versions else None

    def get(self, name: str, default: Any = None) -> Any:
        av = self.latest(name)
        return av.content if av else default

    def ref(self, name: str) -> str | None:
        av = self.latest(name)
        return f"{name}@v{av.version}" if av else None

    def lineage(self, name: str) -> dict[str, Any]:
        """Recursive provenance tree for the latest version of an artifact."""

        def walk(ref: str, depth: int) -> dict[str, Any]:
            art, _, ver = ref.partition("@v")
            versions = self.artifacts.get(art, [])
            av = versions[int(ver) - 1] if ver and len(versions) >= int(ver) else None
            if av is None or depth > 10:
                return {"ref": ref}
            return {
                "ref": ref,
                "hash": av.hash,
                "producer": av.producer,
                "derived_from": [walk(r, depth + 1) for r in av.derived_from],
            }

        r = self.ref(name)
        return walk(r, 0) if r else {}

    # --- decisions ------------------------------------------------------
    def decide(self, node: str, kind: str, summary: str, actor: str, rationale: str = "", refs: list[str] | None = None) -> Decision:
        with self._lock:
            d = Decision(
                id=f"D{len(self.decisions) + 1:03d}",
                node=node,
                kind=kind,
                summary=summary,
                actor=actor,
                rationale=rationale,
                refs=refs or [],
            )
            self.decisions.append(d)
            return d

    # --- persistence ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "artifacts": {k: [asdict(v) for v in vs] for k, vs in self.artifacts.items()},
            "decisions": [asdict(d) for d in self.decisions],
            "feedback": self.feedback,
        }

    @classmethod
    def from_dict(cls, scenario: dict[str, Any], data: dict[str, Any]) -> "RunContext":
        ctx = cls(scenario)
        ctx.artifacts = {k: [ArtifactVersion(**v) for v in vs] for k, vs in data.get("artifacts", {}).items()}
        ctx.decisions = [Decision(**d) for d in data.get("decisions", [])]
        ctx.feedback = data.get("feedback", {})
        return ctx

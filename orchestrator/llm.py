"""Pluggable reasoning backends for agents.

OfflineProvider (default): replays recorded agent responses from the scenario
playbook, so demos and CI are deterministic and need no API key. Playbook list
items may carry `when: {Q1: "..."}` and are kept only if the clarified
requirement answers match - so a human revising an answer changes the plan.

AnthropicProvider: calls the Messages API and asks for JSON shaped like the
recorded example. Its output goes through the same exit gates as the offline
path; on failure the engine retries and then falls back to the offline agent.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from typing import Any, Protocol


class LLMError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str

    def generate(self, key: str, *, prompt: str, context: dict[str, Any], execution: int) -> Any: ...

    def generate_with_meta(self, key: str, *, prompt: str, context: dict[str, Any],
                           execution: int) -> tuple[Any, dict[str, Any]]: ...


def sha256(value: Any) -> str:
    blob = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class RecordingLLM:
    """Per-dispatch wrapper that records the provenance of every reasoning call
    (who produced the content, from what prompt, with which response hash)."""

    def __init__(self, inner: Any, sink: list[dict[str, Any]]):
        self.inner = inner
        self.name = inner.name
        self.sink = sink

    def generate(self, key: str, *, prompt: str, context: dict[str, Any], execution: int) -> Any:
        started = time.time()
        base = {"key": key, "provider": self.inner.name, "execution": execution,
                "prompt_sha256": sha256({"prompt": prompt, "context": context})}
        try:
            data, meta = self.inner.generate_with_meta(key, prompt=prompt, context=context, execution=execution)
        except Exception as exc:
            self.sink.append({**base, "error": str(exc)[:300], "latency_s": round(time.time() - started, 3)})
            raise
        self.sink.append({**base, **meta, "response_sha256": sha256(data), "latency_s": round(time.time() - started, 3)})
        return data


def _filter_when(value: Any, answers: dict[str, Any]) -> Any:
    if isinstance(value, list):
        kept = []
        for item in value:
            if isinstance(item, dict) and "when" in item:
                cond = item["when"]
                if any(answers.get(k) != v for k, v in cond.items()):
                    continue
                item = {k: v for k, v in item.items() if k != "when"}
                if set(item) == {"text"}:  # conditional scalar list item
                    item = item["text"]
            kept.append(_filter_when(item, answers))
        return kept
    if isinstance(value, dict):
        return {k: _filter_when(v, answers) for k, v in value.items()}
    return value


class OfflineProvider:
    """Replays recorded responses. Provenance marks the content as `recorded`, never `generated`."""

    name = "offline"

    def __init__(self, playbook: dict[str, Any], faults: dict[str, Any] | None = None, source: str = "playbook"):
        self.playbook = playbook
        self.faults = faults or {}
        self.source = source

    def generate(self, key: str, *, prompt: str, context: dict[str, Any], execution: int) -> Any:
        return self.generate_with_meta(key, prompt=prompt, context=context, execution=execution)[0]

    def generate_with_meta(self, key: str, *, prompt: str, context: dict[str, Any],
                           execution: int) -> tuple[Any, dict[str, Any]]:
        fault = self.faults.get(key)
        variant_key = key
        if fault and execution in fault.get("on_executions", []):
            variant_key = f"{key}__{fault['variant']}"
            if fault.get("variant") == "error":
                raise LLMError(f"injected fault: provider error for '{key}' (execution {execution})")
        if variant_key not in self.playbook:
            raise LLMError(f"no recorded response for '{variant_key}'")
        response = _filter_when(self._resolve(variant_key), context.get("answers", {}))
        meta = {"content_origin": "recorded", "source": f"{self.source}#{variant_key}",
                "fault_injected": variant_key != key}
        return response, meta

    def _resolve(self, key: str) -> Any:
        """A variant may be declared as a delta of another response (keeps playbooks DRY)."""
        entry = self.playbook[key]
        if not (isinstance(entry, dict) and "derive_from" in entry):
            return copy.deepcopy(entry)
        text = json.dumps(self._resolve(entry["derive_from"]))
        for old, new in entry.get("substitute", []):
            if json.dumps(old)[1:-1] not in text:
                raise LLMError(f"variant '{key}': substitution anchor not found")
            text = text.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
        base = json.loads(text)
        base["changes"] = base.get("changes", []) + entry.get("append_changes", [])
        if "summary" in entry:
            base["summary"] = entry["summary"]
        return base


def _exemplar(example: Any, answers: dict[str, Any]) -> Any:
    """Shape exemplar for the live model: recorded content is trimmed so the model is shown the
    format, not handed the answer - and reference-copy ops are never shown."""
    example = _filter_when(copy.deepcopy(example), answers)
    if isinstance(example, dict) and isinstance(example.get("changes"), list):
        shown = []
        for op in example["changes"][:3]:
            if op.get("op") == "create_from_reference":
                op = {"op": "create", "path": op["path"], "content": "<complete file content>"}
            for k in ("content", "search", "replace"):
                if isinstance(op.get(k), str) and len(op[k]) > 200:
                    op[k] = op[k][:200] + "...<truncated example>"
            shown.append(op)
        example["changes"] = shown
    return example


class AnthropicProvider:
    """Live reasoning via the Anthropic Messages API. Output goes through the same gates as
    recorded content; failures retry, then fall back to the offline agent."""

    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, playbook: dict[str, Any], model: str | None = None, timeout: float = 300.0, client=None):
        import httpx  # local import: optional dependency path

        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.model = model or os.environ.get("ORCH_LLM_MODEL", "claude-sonnet-5-5")
        self.max_tokens = int(os.environ.get("ORCH_LLM_MAX_TOKENS", "16000"))
        self.playbook = playbook  # used only as a (trimmed) format exemplar
        self.client = client or httpx.Client(timeout=timeout)

    def generate(self, key: str, *, prompt: str, context: dict[str, Any], execution: int) -> Any:
        return self.generate_with_meta(key, prompt=prompt, context=context, execution=execution)[0]

    def generate_with_meta(self, key: str, *, prompt: str, context: dict[str, Any],
                           execution: int) -> tuple[Any, dict[str, Any]]:
        example = self.playbook.get(key)
        shape = _exemplar(example, context.get("answers", {}))
        system = (
            "You are one agent in a governed SDLC pipeline. Respond with a single JSON "
            "value only - no prose, no markdown fences. Match the structure (keys and "
            "types) of the example exactly. File edits must be complete and self-contained: "
            "use op 'create' with the full file content, or 'replace' with a 'search' string "
            "that occurs exactly once in the current file."
        )
        user = (
            f"TASK ({key}):\n{prompt}\n\nCONTEXT:\n{json.dumps(context, default=str)[:120000]}\n\n"
            f"EXAMPLE OF THE REQUIRED JSON SHAPE:\n{json.dumps(shape, default=str)[:20000]}"
        )
        resp = self.client.post(
            self.URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        if resp.status_code != 200:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("stop_reason") == "max_tokens":
            raise LLMError(f"LLM output truncated at max_tokens={self.max_tokens} (raise ORCH_LLM_MAX_TOKENS)")
        text = "".join(b.get("text", "") for b in body.get("content", []))
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM returned invalid JSON: {exc}") from exc
        if isinstance(example, dict) and isinstance(data, dict):
            missing = [k for k in example if k not in data]
            if missing:
                raise LLMError(f"LLM response missing keys: {missing}")
        meta = {"content_origin": "generated", "model": body.get("model", self.model),
                "response_id": body.get("id"), "usage": body.get("usage", {})}
        return data, meta

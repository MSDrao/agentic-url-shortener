"""Requirements agent: interpret intent, detect ambiguity, normalize into a spec.

Deterministic analysis (ambiguity lexicon, PII signal detection) runs on every
path; the reasoning backend (recorded or live LLM) supplies the structured
spec. Any ambiguous term the backend did not turn into a question is still
surfaced, so ambiguity cannot be silently dropped.
"""

from __future__ import annotations

import re

from ..model import AgentResult
from .base import Agent, AgentTask

AMBIGUITY_LEXICON = {
    "safer": "which threat: malicious destinations, link enumeration, abuse, or account security?",
    "secure": "which threat model and controls are in scope?",
    "quickly": "delivery deadline, or runtime latency?",
    "fast": "what latency target (p50/p95) and at what load?",
    "slowing": "what latency budget must be preserved?",
    "who": "identity of individuals (personal data) or aggregate audience?",
    "better": "better by which measurable criterion?",
    "scalable": "what throughput / data volume target?",
    "some": "which exact items?",
    "etc": "the list is open-ended; what is in scope?",
    "appropriate": "who decides what is appropriate, by which rule?",
    "simple": "simple for whom (API users, operators)?",
    "soon": "what date?",
}


def detect_ambiguities(text: str) -> list[dict]:
    found = []
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    for term, why in AMBIGUITY_LEXICON.items():
        for s in sentences:
            if re.search(rf"\b{re.escape(term)}\b", s, re.IGNORECASE):
                found.append({"term": term, "sentence": s, "why": why})
                break
    return found


def detect_pii(text: str, signals: list[str]) -> list[str]:
    return [sig for sig in signals if re.search(sig, text, re.IGNORECASE)]


class RequirementsAgent(Agent):
    name = "requirements_analyst"
    boundary = "read-only; produces requirements_spec; may request policy-allowlisted plan changes"

    def __init__(self, llm=None):
        self.llm = llm

    def run(self, task: AgentTask) -> AgentResult:
        text = task.scenario["requirement"].strip()
        clar = task.inputs.get("clarifications") or {}
        detected = detect_ambiguities(text)
        comp = task.policy.section("compliance")
        pii_hits = detect_pii(text, comp.get("pii_signals", []))

        rec = task.llm.generate(
            "requirements",
            prompt="Normalize this requirement into a testable engineering spec. Raise "
                   "clarifying questions for every ambiguity, each with a safe default.",
            context={"requirement": text, "clarifications": clar, "feedback": task.feedback,
                     "detected_ambiguities": detected, "answers": clar},
            execution=task.execution,
        )

        questions = list(rec.get("questions", []))
        covered = {q.get("about") for q in questions}
        for d in detected:  # never drop a detected ambiguity
            if d["term"] not in covered:
                questions.append({
                    "id": f"Q{len(questions) + 1}",
                    "about": d["term"],
                    "question": f"'{d['term']}' is ambiguous: {d['why']}",
                    "default": "interpret conservatively; keep current behaviour",
                    "options": [],
                })

        answers, ambiguities, decisions = {}, [], []
        for q in questions:
            if q["id"] in clar:
                value, status = clar[q["id"]], "confirmed"
            else:
                value, status = q["default"], "assumed"
                decisions.append({"kind": "assumption", "summary": f"{q['id']}: assumed '{value}'",
                                  "rationale": q["question"]})
            answers[q["id"]] = value
            ambiguities.append({**q, "answer": value, "status": status})

        spec = {
            "problem_statement": rec["problem_statement"],
            "functional": rec.get("functional", []),
            "non_functional": rec.get("non_functional", []),
            "acceptance_criteria": rec.get("acceptance_criteria", []),
            "out_of_scope": rec.get("out_of_scope", []),
            "domain_terms": rec.get("domain_terms", []),
            "ambiguities": ambiguities,
            "answers": answers,
            "detected_terms": [d["term"] for d in detected],
            "pii_involved": bool(pii_hits),
            "pii_signals": pii_hits,
        }
        impact: set[str] = set()
        if any(a["status"] == "assumed" for a in ambiguities):
            impact.add("unresolved_ambiguity")
        plan_changes = []
        if pii_hits:
            impact.add("pii")
            if comp.get("require_privacy_review_on_pii", True):
                plan_changes.append({
                    "template": "privacy_review",
                    "after": ["design"],
                    "before": ["implement"],
                    "reason": f"requirement involves personal data (signals: {pii_hits})",
                })
        n_assumed = sum(a["status"] == "assumed" for a in ambiguities)
        return AgentResult(
            artifacts={"requirements_spec": spec},
            decisions=decisions,
            plan_changes=plan_changes,
            impact=impact,
            summary=f"{len(spec['functional'])} functional reqs, {len(spec['acceptance_criteria'])} ACs, "
                    f"{len(ambiguities)} ambiguities ({n_assumed} assumed), pii={bool(pii_hits)}",
        )

# Final engineering summary

## 1. Plan and rationale

The assignment asks for a system that turns a requirement into a reviewable engineering outcome. It stresses orchestration that is governed and stateful, and that goes beyond chaining tasks in a line. The plan followed from that emphasis:

1. **Build the target system first, properly.** The URL shortener in `service/` is a production-style baseline: layered, validated, rate-limited, tested to 99% coverage. Brownfield work only means something against a codebase that is worth changing.
2. **Make orchestration the core, with governance built in rather than added later.** The engine is a DAG scheduler whose central rule is that *agents propose and the engine commits*. Gates, policy, approvals, lineage and audit sit on that commit path, so no route skips them.
3. **Make results reproducible by default and intelligent when enabled.** Offline replay of recorded responses makes every demo deterministic and reviewable. A live-LLM provider can be switched in, and it goes through the same gates and falls back to offline if it fails.
4. **Prove behaviour with real execution.** Tests really run in the workspace. The AST analysis, security scan and OpenAPI-derived docs are computed from the actual code. Failure paths are shown through fault injection, not just described.
5. **Show three distinct shapes of work.** Greenfield exercises breadth. Brownfield exercises change safety: impact analysis, migrations, rework. Ambiguous exercises judgment: questions, PII policy, human revision, re-planning.

## 2. Requirement traceability (assignment → implementation → evidence)

| # | Assignment requirement | Where it lives | Evidence |
|---|---|---|---|
| 4.1 | Requirement understanding: interpret, find ambiguity, normalize | `agents/requirements.py` (lexicon, PII signals, forced questions, clarification merge) | Ambiguous scenario: 4 terms detected → 6 questions; section 1 of each SUMMARY |
| 4.2 | Task decomposition with dependencies and sequencing | `agents/design.py` (task DAG → waves); gates `tasks_acyclic`, `traceability` | Greenfield: 7 tasks / 5 waves; brownfield T5 runs in parallel with T2 |
| 4.3 | Codebase reasoning: modules, APIs, data flows | `agents/analysis.py` (AST, import graph, routes, tables, TF-IDF, blast radius) | Brownfield section 2 of SUMMARY; `unpredicted_files` shows what the analysis missed |
| 4.4 | Orchestration: DAG with entry/exit gates | `graph.py`, `gates.py`, `pipeline.py` | `graph.mmd`; gate table in each SUMMARY |
| | – sequential and parallel paths with synchronization | `engine.py` worker pool + join semantics | `test_parallel_branches_overlap_and_join_waits` |
| | – cross-stage context and decision lineage | `context.py` (versions, `derived_from`, decisions) | Ambiguous: `requirements_spec@v2 ← clarifications@v1 ← human` |
| | – human approval for high-impact actions | `policy.approval_reasons`, `approval.py` | 4–7 approvals per run, recorded with approver identity |
| | – bounded retries, fallback, rollback, safe-stop | `engine._attempt_failed/_rework/_safe_stop`, `workspace.py` | Greenfield retry; brownfield rework+rollback; drills |
| | – policy guardrails: security, compliance, change control | `policy/default.yaml`, `policy.py`, `scanners.py` | policy-violation drill; privacy_review; protected paths |
| | – audit-grade observability and traceability | `audit.py` (hash chain), `report.py` | `verify-audit`; tamper tests |
| | – reliability metrics: success rate, retry/rollback, MTTR, latency | `metrics.py`, `orchestrator metrics` | `metrics.json`, `history.jsonl` |
| | – dynamic re-plan when upstream changes | hash invalidation + policy-allowlisted node insertion | Ambiguous: 3 re-plans; `test_unchanged_upstream_does_not_invalidate_downstream` |
| 4.5 | Production-quality code, API/schema, tests, docs | `service/`; generated `docs/API.md`, ADRs, CHANGELOG | 53 baseline tests → 63 after the brownfield change; OpenAPI contract |
| 4.6 | Validation and risk control | exit gates, security scan, release checklist, risk register | Section 6–7 of each SUMMARY |
| 4.7 | Controlled autonomy | per-agent write scopes, budgets, approvals, kill switch | Architecture §5; drills |
| 4.8 | Final engineering summary | this document + per-run `SUMMARY.md` | — |
| 5 | Prototype, architecture, 3 scenarios, setup, testing, limitations | README, ARCHITECTURE.md, SCENARIOS.md, this file | `make test`, `make demo` |

## 3. Artifacts

- `service/`: the baseline URL shortener and its 53 tests.
- `orchestrator/`: the engine, 10 agents, policy engine, audit, metrics and CLI.
- `policy/default.yaml`: governance.
- `scenarios/`: 3 scenarios, 3 drills, and recorded playbooks.
- `tests/`: orchestrator unit tests, engine behaviour tests and end-to-end scenario tests.
- `sample-runs/`: demo outputs. Each run contains `SUMMARY.md`, `change.patch`, `audit.jsonl`, `metrics.json`, `state.json` and `graph.mmd`.

## 4. Risks, trade-offs and validation

**Validation strategy (defence in depth):**

1. **Exit gates on every node.** These are structural checks (spec completeness, acyclic tasks, AC → task traceability, AC → test coverage) and outcome checks (tests pass, coverage ≥ 85%, *planned* tests exist and pass, no blocking findings, docs cover every route, checklist green).
2. **Policy before commit.** Checks cover autonomy scope, change size, secrets, dangerous calls, SQL interpolation, PII columns and file deletion.
3. **Humans at impact points.** Approval is required for schema, API, dependency and PII changes, protected paths, unconfirmed assumptions, and always for release.
4. **Release checklist on real evidence**, including a migration-safety scan of the actual diff.

**Engineering trade-offs:**

| Choice | Benefit | Cost / mitigation |
|---|---|---|
| Recorded-response default | Deterministic, secret-free, reviewable demos and CI | Does not show generative behaviour offline; the live provider exists and is covered by mocked tests |
| Single-writer engine, thread pool | No races on state; simple reasoning | Soft timeouts only; a process-based pool or a distributed queue is the scale path |
| Full-copy snapshots | Exact, simple rollback | Cost grows with repo size; switch to git worktrees for large repos |
| Hash-based invalidation | Re-work only when outputs change | Depends on deterministic serialization (sorted JSON) |
| Approval by impact, not per step | Oversight focused where risk is | Impact detection must be trustworthy; flags come from the design **and** are re-derived from touched paths |
| Soft-delete links, 307 redirects | Reversible deletes; accurate analytics | Extra hop cost vs 301; deleted codes are not reusable |
| SQLite + per-instance rate limiter | Zero-ops v1 | Postgres/Redis upgrade paths, isolated behind repository/limiter classes |

**Main risks:**

| Risk | Mitigation |
|---|---|
| An agent produces plausible but wrong code | Planned tests plus the full regression suite gate every change; the rework loop is bounded; a human reviews the patch at release |
| An agent exceeds its mandate | Write scopes; a critical finding safe-stops with nothing committed |
| Assumptions nobody has confirmed reach production | Assumptions are flagged in the spec, need approval, appear in the changelog, and go into the release risk register |
| Personal data creeps into the product | Policy PII signals; the privacy review node is inserted automatically; PII column scan |
| Runaway loops or cost | Attempt and wall-clock budgets, rework cap, revision cap, kill switch |
| Audit tampering | Hash chain with verification |

## 5. Assumptions

- **Replayed responses.** "Agentic execution" is shown by governed multi-agent orchestration whose agents, in offline mode, replay recorded reasoning outputs. That choice keeps evaluation deterministic. Running with `--llm anthropic` swaps in live reasoning.
- **Scripted approvals.** Unattended runs use decision scripts recorded under a named approver identity. That stands in for a human; `--approvals interactive` puts a real person at each gate.
- **Workspace scope.** The brownfield and ambiguous scenarios change a *copy* of `service/`. The deliverable is a reviewable patch plus the workspace. Merging to the main branch is left to humans, in line with the rule that humans own final quality.
- **Choice of URL shortener features.** The requirement ("core APIs, analytics, reliability features") was read as: create, redirect, metadata, stats, deactivate, expiry, aliases, auth, rate limiting, health checks, request IDs and migrations.
- **Timeline.** The "2–3 days" in the brief describes the effort envelope, not a feature the system has to implement.

## 6. Limitations

- **The live-LLM path has not been run against the real API here.** No API key was available in the build environment. The adapter's request format, response parsing and error handling are unit-tested with a mocked transport, and failures fall back to the offline agents.
- **Offline content is authored, not generated.** In offline mode the spec, design and code edits come from playbooks. Everything around them is computed from the real code: gates, policy, tests, coverage, AST analysis, security scan, docs, traceability and metrics.
- **Heuristic scanners.** The regex and AST checks are illustrative, not a replacement for Bandit, Semgrep or dependency auditing. They would be plugged into the same `security_scan` node.
- **Impact analysis is lexical and structural.** It can miss modules; the brownfield run shows `models.py` as an unpredicted file. Type-aware call-graph analysis would reduce that.
- **Brownfield coverage.** The brownfield scenario is a feature enhancement. Bug-fix and refactor requests go through the same pipeline (analysis → design → implement → rework loop), but there is no dedicated scenario file for them; the rework loop in the brownfield run, where the agent fixes its own off-by-one, is the nearest example.
- **Engine distribution.** The engine runs in one process with soft timeouts. Resume works from persisted state, but there is no distributed locking or multi-run coordination.
- **Service scale limits.** The rate limiter is per instance, SQLite allows a single writer, clicks are recorded synchronously, and there is no caching layer. All of these are documented.
- **Visitor-hash secret.** In the ambiguous scenario, the secret defaults to a per-process random value. Production must set `SHORTENER_VISITOR_SECRET`, as the risk register notes.

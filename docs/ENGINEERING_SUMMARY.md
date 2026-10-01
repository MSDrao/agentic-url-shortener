# Final engineering summary

## 1. Plan and rationale

The assignment asks for a system that turns a requirement into a reviewable engineering outcome. It stresses orchestration that is governed and stateful, and that goes beyond chaining tasks in a line. The plan followed from that emphasis:

1. **Build the target system first, properly.** The URL shortener in `service/` is a production-style baseline: layered, validated, rate-limited, tested to 98% coverage. Brownfield work only means something against a codebase that is worth changing.
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
| | – human approval for high-impact actions | `policy.approval_reasons`, `approval.py` (interactive / queued pause-and-resume / opt-in simulated) | Non-interactive runs pause for a person; decisions are bound to the proposal hash; `human` flag on every approval |
| | – bounded retries, fallback, rollback, safe-stop | `engine._attempt_failed/_rework/_safe_stop`, `workspace.py` | Greenfield retry; brownfield rework+rollback; drills |
| | – policy guardrails: security, compliance, change control | `policy/default.yaml`, `policy.py`, `scanners.py` | policy-violation drill; privacy_review; protected paths |
| | – audit-grade observability and traceability | `audit.py` (hash chain + completeness anchor, optional HMAC), provenance on every artifact, `report.py` | `verify-audit`; tamper, truncation and forged-anchor tests |
| | – reliability metrics: success rate, retry/rollback, MTTR, latency | `metrics.py`, `orchestrator metrics` | `metrics.json`, `history.jsonl` |
| | – dynamic re-plan when upstream changes | hash invalidation + policy-allowlisted node insertion | Ambiguous: 3 re-plans; `test_unchanged_upstream_does_not_invalidate_downstream` |
| 4.5 | Production-quality code, API/schema, tests, docs | `service/`; generated `docs/API.md`, ADRs, CHANGELOG | 67 baseline tests → 77 after the brownfield change; OpenAPI contract |
| 4.6 | Validation and risk control | exit gates, security scan, release checklist, risk register | Section 6–7 of each SUMMARY |
| 4.7 | Controlled autonomy | per-agent write scopes, budgets, approvals, kill switch | Architecture §5; drills |
| 4.8 | Final engineering summary | this document + per-run `SUMMARY.md` | — |
| 5 | Prototype, architecture, 3 scenarios, setup, testing, limitations | README, ARCHITECTURE.md, SCENARIOS.md, this file | `make test`, `make demo` |

## 3. Artifacts

- `service/`: the baseline URL shortener and its 67 tests.
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
- **Approvals in the demo.** `make demo` uses explicitly requested *simulated* approvals so it can finish unattended. Every such decision is recorded as `simulated:<role>` with `human: false`, and each report is stamped "not authorized for release". Real runs use `interactive`, or `queue` (the default without a terminal), where the run pauses until a person decides. The approver identity is asserted (the OS user or `--as`), not authenticated; a real deployment would put the `approve` command behind SSO.
- **Workspace scope.** The brownfield and ambiguous scenarios change a *copy* of `service/`. The deliverable is a reviewable patch plus the workspace. Merging to the main branch is left to humans, in line with the rule that humans own final quality.
- **Choice of URL shortener features.** The requirement ("core APIs, analytics, reliability features") was read as: create, redirect, metadata, stats, deactivate, expiry, aliases, auth, rate limiting, health checks, request IDs and migrations.
- **Timeline.** The "2–3 days" in the brief describes the effort envelope, not a feature the system has to implement.

## 6. Limitations

- **Live-LLM evidence is limited to one run.** **A live run has been captured.** `sample-runs/live-brownfield/` is a brownfield run with `claude-sonnet-5-5` (the model generated the spec, design and code). There were 4 human approvals, and 85/85 tests passed at 99% coverage, with no rework needed. The audit verifies, and provenance marks the spec, design and change set as `generated`. That is one successful run, not a measure of reliability across runs or scenarios; live greenfield (generating the whole service) has not been attempted and may need `ORCH_LLM_MAX_TOKENS` raised. Truncated output is detected and fails the attempt rather than being accepted.
- **Sandbox scope.** The default `process` backend strips secrets, applies resource limits, and cuts off the network on Linux, but it does not isolate the filesystem. The `docker` backend does. It was tested with a locally assembled image equivalent to `Dockerfile.sandbox`; the Dockerfile itself could not be built here because registry access was blocked.
- **Audit completeness.** The anchor and the independent head catch truncation. A party that controls the run directory, the history file and the HMAC key can still forge a consistent log. External append-only storage or off-host signing is the production answer.
- **Offline content is authored, not generated.** In offline mode the spec, design and code edits come from playbooks. Everything around them is computed from the real code: gates, policy, tests, coverage, AST analysis, security scan, docs, traceability and metrics.
- **Heuristic scanners.** The regex and AST checks are illustrative, not a replacement for Bandit, Semgrep or dependency auditing. They would be plugged into the same `security_scan` node.
- **Impact analysis is lexical and structural.** It can miss modules; the brownfield run shows `models.py` as an unpredicted file. Type-aware call-graph analysis would reduce that.
- **Brownfield coverage.** The brownfield scenario is a feature enhancement. Bug-fix and refactor requests go through the same pipeline (analysis → design → implement → rework loop), but there is no dedicated scenario file for them; the rework loop in the brownfield run, where the agent fixes its own off-by-one, is the nearest example.
- **Engine distribution.** The engine runs in one process with soft timeouts. Resume works from persisted state, but there is no distributed locking or multi-run coordination.
- **Service scale limits.** The rate limiter is per instance, SQLite allows a single writer, clicks are recorded synchronously, and there is no caching layer. All of these are documented.
- **Visitor-hash secret.** In the ambiguous scenario, the secret defaults to a per-process random value. Production must set `SHORTENER_VISITOR_SECRET`, as the risk register notes.

## 7. External review: findings and resolution

An independent review of the first version raised six issues. I reproduced each one before changing anything.

| # | Finding | Reproduced? | Resolution | Regression tests |
|---|---|---|---|---|
| 1 | Approvals could be bypassed: non-interactive runs auto-approved everything under a made-up identity | Yes | Three explicit modes. Without a terminal the default is **queue**: the run pauses, a person records the decision, resume commits the stored proposal (hash-bound). Simulated mode is opt-in, labelled `human: false`, and policy can forbid it | `test_default_non_interactive_run_pauses_for_a_human`, `test_decision_for_a_different_proposal_is_refused`, `test_policy_can_forbid_simulated_approvals`, `test_simulated_approver_is_labelled_non_human` |
| 2 | No real LLM engineering; greenfield copies the reference implementation | Yes (by design, but under-disclosed) | Provenance on every artifact (`recorded` / `generated` / `computed`, with hashes); reference copying refused in live runs, including fallbacks; the live model gets a trimmed exemplar and truncation is detected. Live run captured in `sample-runs/live-brownfield/` (see §6) | `test_live_brownfield_run_records_generated_provenance`, `test_live_greenfield_cannot_copy_the_reference_implementation` |
| 3 | The workspace is not a sandbox: secrets are inherited, there are no limits, and tests are not scanned | Yes | `sandbox.py`: a `process` backend (environment allowlist, rlimits, process-group kill, no network on Linux) and a `docker` backend (full isolation). Test files are now scanned for dangerous calls. Environment failures safe-stop instead of triggering rework | `tests/test_sandbox.py` (run on both backends), `test_infrastructure_failure_safe_stops_instead_of_reworking` |
| 4 | The test gate ignored pytest's exit code | Yes | `tests_pass` and the release checklist require `exit_code == 0` | `test_tests_pass_gate_requires_zero_exit_code` |
| 5 | Truncating the audit log still verified | Yes | Completeness anchor after every record, an independent head in `history.jsonl`, optional HMAC; limits documented | `test_audit_detects_truncated_tail`, `test_audit_independent_head_catches_rewritten_anchor`, `test_keyed_anchor_cannot_be_forged_without_key` |
| 6 | URL edge cases: `https://[invalid` returned 500; `127.1` and `2130706433` were accepted | Yes, plus `0x7f.0.0.1` and `0177.0.0.1` | Parse errors now return 422. Any all-numeric host that is not a canonical dotted-quad is rejected. (The service never fetches targets, so the risk is redirect abuse, not server-side SSRF) | 11 new cases in `test_rejected_urls`, `test_numeric_looking_domain_names_are_allowed`, and API-level cases |

### Second review round

A second review of the fixed version raised four more defects. Each was confirmed in the code and fixed with a regression test. A fifth point, the live run, has since been completed.

| # | Finding | Resolution | Regression test |
|---|---|---|---|
| 1 | A slow human review exhausted `max_wall_seconds` on resume | The budget measures active execution time; approval waiting is excluded and reported as `approval_wait_s` | `test_time_waiting_for_a_human_does_not_consume_the_execution_budget`, `test_long_human_review_does_not_trip_the_budget_on_resume` |
| 2 | `resume` defaulted to offline, silently switching live runs to recorded responses | The run's LLM backend and approval mode are persisted and restored; switching needs `--override-config` and is audited | `test_resume_restores_the_original_llm_backend_and_refuses_silent_switches` |
| 3 | A Docker timeout did not stop the container | Each container gets a unique name and is `docker rm -f`'d on timeout or cancellation | `test_docker_timeout_force_removes_the_container_mocked`, and against real Docker `test_docker_timeout_leaves_no_container_running` |
| 4 | The HMAC check could be bypassed by setting `keyed: false` | With a key supplied, an unsigned anchor is rejected | `test_keyed_verification_rejects_unsigned_anchor_downgrade` |
| 5 | No real live-model run has been captured | **Done.** `sample-runs/live-brownfield/`: `claude-sonnet-5-5` generated the spec, design and code; 4 human approvals; 85/85 tests; GO | audit verified (`verify-audit`) |


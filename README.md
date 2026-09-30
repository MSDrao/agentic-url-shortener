# Agentic SDLC Orchestrator: URL Shortener

A working prototype that takes a written requirement and turns it into a **reviewable engineering outcome**: a normalized spec, a design with a task graph, a code change, tests that actually run, generated docs, a security scan, a release-readiness verdict and an audit trail. The work is done by agents inside a **governed, stateful orchestration engine**. Humans approve high-impact steps and make the final release decision.

The target system is a production-style **URL shortener** (FastAPI + SQLite). The same engine runs it through three scenarios:

| Scenario | Starts from | What it demonstrates |
|---|---|---|
| **Greenfield** | an empty workspace | full lifecycle, parallel branches, a transient agent failure recovered by bounded retry |
| **Brownfield** (`max_clicks`) | a copy of `service/` | AST-based impact analysis, an additive migration, a buggy first attempt caught by tests, then rollback and rework |
| **Ambiguous** ("make links safer, show who's clicking") | a copy of `service/` | ambiguity detection, PII guardrail, a compliance node added at runtime, a human revising the scope at the design review, and re-planning driven by artifact hashes |

There are also three **failure drills**:

- **Release rejected:** the workspace rolls back to the baseline.
- **Policy violation:** the run safe-stops, then resumes.
- **Rework budget exhausted:** the run stops within its bounds instead of looping.

## Quick start

Requires Python 3.11+. Tested with Python 3.11, FastAPI 0.142, Pydantic 2.13 and pytest 9.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make test          # service suite (53 tests) + orchestrator suite (unit + end-to-end scenarios)
make demo          # runs all 3 scenarios + 3 drills unattended, then prints fleet metrics
```

Every run writes to `runs/<run_id>/`:

| File | What it is |
|---|---|
| `SUMMARY.md` | engineering summary: requirement, ambiguities, impact analysis, design and task waves, orchestration trace, gates, decisions and lineage, traceability matrix, risks, metrics |
| `change.patch` | the reviewable unified diff against the baseline |
| `workspace/` | the isolated working copy with the change applied |
| `audit.jsonl` | hash-chained, tamper-evident audit log |
| `state.json` | resumable run state (statuses, versioned artifacts, decisions) |
| `metrics.json`, `graph.mmd`, `test-output/` | reliability metrics, the graph with final statuses, raw pytest/coverage output |

Pre-generated outputs from the demo are in [`sample-runs/`](sample-runs/).

## Running things individually

```bash
# Run a scenario. You make the approval decisions at the terminal:
python -m orchestrator run scenarios/brownfield.yaml --approvals interactive

# Unattended. Decisions come from the scenario's `approvals:` script and are recorded under its approver identity:
python -m orchestrator run scenarios/ambiguous.yaml --approvals auto

# Use a live LLM for the reasoning agents. Falls back to the recorded responses if the LLM fails.
export ANTHROPIC_API_KEY=...  ORCH_LLM_MODEL=<model id>
python -m orchestrator run scenarios/greenfield.yaml --llm anthropic

python -m orchestrator stop <run_id>                    # kill switch: safe-stop before the next dispatch
python -m orchestrator resume <run_id> --reset-failed   # continue a halted run from persisted state
python -m orchestrator verify-audit <run_id>            # check the audit hash chain
python -m orchestrator metrics                          # success rate, retry rate, rollbacks, MTTR, latency across runs
python -m orchestrator graph scenarios/brownfield.yaml  # print the dependency graph (Mermaid)
```

Run the service directly:

```bash
make serve     # uvicorn on :8000; OpenAPI docs at http://localhost:8000/docs
curl -X POST localhost:8000/api/v1/links -H 'content-type: application/json' -d '{"url":"https://example.com"}'
```

Service configuration uses environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `SHORTENER_DB_PATH` | `shortener.db` | SQLite file |
| `SHORTENER_BASE_URL` | `http://localhost:8000` | used to build `short_url` and to reject self-links |
| `SHORTENER_API_KEY` | unset (open) | when set, create/delete require `X-API-Key` |
| `SHORTENER_RATE_LIMIT_PER_MINUTE` | `60` | per-client creation limit |
| `SHORTENER_ALLOW_PRIVATE_TARGETS` | `false` | allow private/loopback destinations (dev only) |

## Repository layout

```
service/            URL shortener (the codebase under change). Baseline for the brownfield/ambiguous runs
  shortener/        api -> service -> repository -> db, plus validation, codes, ratelimit, schemas
  tests/            53 unit + integration tests (99% coverage)
orchestrator/       the agentic SDLC engine
  engine.py         scheduler, gates, approvals, retries/fallback/rework, re-planning, safe-stop, persistence
  graph.py          explicit DAG: validation, topological waves, runtime node insertion
  context.py        versioned artifacts + decision lineage
  policy.py         guardrails (autonomy scopes, security, compliance, change control) from policy/default.yaml
  gates.py          named entry/exit gates
  audit.py          hash-chained audit log
  metrics.py        reliability metrics
  workspace.py      isolated working copy, snapshots, rollback, diff
  llm.py            offline (recorded) and Anthropic providers
  agents/           requirements, codebase analysis, architect, test planner, privacy officer,
                    implementer, test runner, security scanner, tech writer, release manager
policy/default.yaml governance as data
scenarios/          greenfield / brownfield / ambiguous + drills/, and their recorded playbooks
tests/              orchestrator unit tests + end-to-end scenario tests
docs/               ARCHITECTURE.md, SCENARIOS.md, ENGINEERING_SUMMARY.md
```

## Testing approach

- **Service:** unit tests for pure components (code generation, URL validation, rate limiter, collision handling, migrations) and integration tests through HTTP with real SQLite and a fake clock. Coverage is 99%. The orchestration also runs this suite inside each workspace and enforces a coverage floor (85%).
- **Orchestrator:** unit tests for the graph, audit chain (tamper and deletion detection), policy and scanners, the provider (`when` filtering, fault variants), approvals, the workspace, and the live-LLM adapter with HTTP mocked. Engine behaviour tests use scripted agents to cover:
  - parallel overlap and join, retry, fallback, rework with rollback, and bounded rework
  - critical policy stops and approval rejection
  - revise followed by selective invalidation, and no invalidation when the artifact hash is unchanged
  - policy-gated node insertion, the kill switch, budget stops, and audit completeness
- **End-to-end:** every scenario and drill runs through the real CLI and is checked for its final state, metrics, audit integrity, a GO verdict, and a verified AC→test traceability matrix. Resume-after-halt is also tested.

## Limitations and trade-offs (summary)

The full discussion is in [docs/ENGINEERING_SUMMARY.md](docs/ENGINEERING_SUMMARY.md#6-limitations).

- **Offline mode replays recorded agent responses.** This keeps demos deterministic and free of API keys. The orchestration, gates, policy, tests, scans, docs and analysis are all real, but in offline mode the *content* of the spec, design and code edits is recorded rather than generated. The live-LLM path exists and is unit-tested with HTTP mocked. It was **not** exercised against the real API in this environment.
- Single-process engine with thread-based parallelism. Agent threads cannot be force-killed, so timeouts are soft. Snapshots are full directory copies; git worktrees are the path to larger repos.
- The service targets a single SQLite writer and uses a per-instance rate limiter. Both are documented upgrade paths (Postgres, Redis).

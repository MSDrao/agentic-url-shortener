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

Requires Python 3.9+. Tested on Python 3.9 (the macOS built-in `python3`), 3.10 and 3.11, with FastAPI 0.128–0.142 and Pydantic 2.13.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make test          # service suite (67 tests) + orchestrator suite (unit, sandbox, live-plumbing, end-to-end)
make demo          # all 3 scenarios + 3 drills with SIMULATED approvals (clearly labelled), then fleet metrics
```

`make demo` is the only thing that uses simulated approvals, and it asks for them explicitly. Every other run needs a real person at each high-impact gate (see [Human approvals](#human-approvals)).

Every run writes to `runs/<run_id>/`:

| File | What it is |
|---|---|
| `SUMMARY.md` | engineering summary: requirement, ambiguities, impact analysis, design and task waves, orchestration trace, gates, decisions and lineage, traceability matrix, risks, metrics |
| `change.patch` | the reviewable unified diff against the baseline |
| `workspace/` | the isolated working copy with the change applied |
| `audit.jsonl`, `audit.anchor.json` | hash-chained audit log plus its completeness anchor |
| `approvals/` | pending approval requests and the recorded human decisions (queue mode) |
| `state.json` | resumable run state (statuses, versioned artifacts, decisions) |
| `metrics.json`, `graph.mmd`, `test-output/` | reliability metrics, the graph with final statuses, raw pytest/coverage output |

Pre-generated outputs from the demo are in [`sample-runs/`](sample-runs/).

## Running things individually

```bash
python -m orchestrator run scenarios/brownfield.yaml    # you decide at each gate (terminal), or it pauses (no terminal)
python -m orchestrator run scenarios/ambiguous.yaml --approvals simulated   # scripted demo decisions, human=false

# Live model for the reasoning agents (requirements, design, implementation):
export ANTHROPIC_API_KEY=...  ORCH_LLM_MODEL=<model id>   # optional: ORCH_LLM_MAX_TOKENS (default 16000)
python -m orchestrator run scenarios/brownfield.yaml --llm anthropic

python -m orchestrator stop <run_id>                    # kill switch: safe-stop before the next dispatch
python -m orchestrator resume <run_id> --reset-failed   # continue a halted run with its ORIGINAL llm/approval config
                                                        # (switching e.g. live -> offline needs --override-config; audited)
python -m orchestrator verify-audit <run_id>            # chain + completeness anchor + independent head
python -m orchestrator metrics                          # success rate, retry rate, rollbacks, MTTR, latency across runs
python -m orchestrator graph scenarios/brownfield.yaml  # print the dependency graph (Mermaid)
```

### Human approvals

Approval gates are triggered by policy: schema, public API, dependency and PII changes, protected paths, unconfirmed assumptions, and release (always). There are three modes, and the mode is always explicit:

| `--approvals` | Who decides | Recorded as |
|---|---|---|
| `interactive` (default with a terminal) | you, at the terminal prompt | your OS user, `human: true` |
| `queue` (default without a terminal) | the run **pauses** (`awaiting_approval`); a person decides later with `approve` | the `--as` identity, `human: true` |
| `simulated` (opt-in only) | the scenario's `simulated_approvals` script | `simulated:<role>`, `human: false`; reports say "not authorized for release" |

```bash
python -m orchestrator pending <run_id>                                      # what is waiting, with the evidence
python -m orchestrator approve <run_id> design --decision approve --as alice --comment "schema reviewed"
python -m orchestrator approve <run_id> design --decision revise --target intake --answers '{"Q2": "create_and_redirect"}'
python -m orchestrator resume <run_id>
```

In queue mode, each decision is tied to the hash of the exact proposal the reviewer saw. On resume, that stored proposal is committed as-is; the agent is not re-run, so what was approved is exactly what lands. If a decision was recorded for a different proposal, the run safe-stops. Setting `approvals.allow_simulated_approvals: false` in the policy forbids simulated mode entirely.

### Sandbox for generated code

Tests and the OpenAPI import execute agent-written code, so they run in a sandbox (`policy.sandbox`, or override with `ORCH_SANDBOX`):

- **`process`** (default, portable):
  - only allowlisted environment variables reach the code, so API keys and other secrets are stripped;
  - HOME and TMP are private;
  - CPU, file-size, open-file and memory limits apply, and the whole process group is killed on timeout;
  - on Linux the code also gets no network, using an unprivileged network namespace.
  - It does **not** isolate the filesystem.
- **`docker`**: `--network none`, read-only root filesystem and read-only workspace, a non-root user, all capabilities dropped, `no-new-privileges`, and memory, CPU and process limits. Only the output directory is writable. Build the image first:

```bash
docker build -f Dockerfile.sandbox -t orchestrator-sandbox:py311 .
ORCH_SANDBOX=docker python -m orchestrator run scenarios/brownfield.yaml
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
  tests/            67 unit + integration tests (98% coverage)
orchestrator/       the agentic SDLC engine
  engine.py         scheduler, gates, approvals, retries/fallback/rework, re-planning, safe-stop, persistence
  graph.py          explicit DAG: validation, topological waves, runtime node insertion
  context.py        versioned artifacts + decision lineage
  policy.py         guardrails (autonomy scopes, security, compliance, change control) from policy/default.yaml
  gates.py          named entry/exit gates
  audit.py          hash-chained audit log + completeness anchor (optional HMAC via ORCH_AUDIT_KEY)
  approval.py       interactive / queued (pause-and-resume) / simulated approvers
  sandbox.py        process and docker sandboxes for generated code
  metrics.py        reliability metrics
  workspace.py      isolated working copy, snapshots, rollback, diff
  llm.py            offline (recorded) and Anthropic providers, provenance recording
  agents/           requirements, codebase analysis, architect, test planner, privacy officer,
                    implementer, test runner, security scanner, tech writer, release manager
policy/default.yaml governance as data
scenarios/          greenfield / brownfield / ambiguous + drills/, and their recorded playbooks
tests/              orchestrator unit, sandbox, live-plumbing (mocked API) and end-to-end tests
Dockerfile.sandbox  image for the docker sandbox backend
docs/               ARCHITECTURE.md, SCENARIOS.md, ENGINEERING_SUMMARY.md
```

## Testing approach

- **Service:** unit tests for pure components (code generation, URL validation, rate limiter, collision handling, migrations) and integration tests through HTTP with real SQLite and a fake clock. Coverage is 98%. The orchestration also runs this suite inside each workspace and enforces a coverage floor (85%).
- **Orchestrator:** unit tests for the graph, the audit chain and anchor (tampering, deletion, tail truncation, forged anchors), policy and scanners, the provider (`when` filtering, fault variants), approvals, and the workspace. Engine behaviour tests use scripted agents to cover:
  - parallel overlap and join, retry, fallback, rework with rollback, and bounded rework
  - critical policy stops and approval rejection
  - revise followed by selective invalidation, and no invalidation when the artifact hash is unchanged
  - policy-gated node insertion, the kill switch, budget stops, and audit completeness
  - the test gate requiring pytest exit code 0, and infrastructure failures safe-stopping rather than triggering rework
- **Human approvals:** a run without a terminal pauses, a person approves every gate through the queue, and the approved proposal is committed without re-running the agent. A decision recorded for a different proposal is refused, and policy can forbid simulated mode.
- **Sandbox:** secrets are not inherited, network is denied, grandchild processes are killed on timeout, and the file-size limit holds. With Docker available, the workspace is read-only. Each of these runs against both backends when Docker is present.
- **Live plumbing (mocked API):** a full brownfield run through the Anthropic adapter records `generated` provenance with response ids and hashes, and live greenfield refuses to copy the reference implementation.
- **End-to-end:** every scenario and drill runs through the real CLI and is checked for its final state, metrics, audit integrity, a GO verdict, and a verified AC→test traceability matrix. Resume-after-halt is also tested.

## Limitations and trade-offs (summary)

The full discussion is in [docs/ENGINEERING_SUMMARY.md](docs/ENGINEERING_SUMMARY.md#6-limitations).

- **Offline mode replays recorded agent responses.** This keeps demos deterministic and free of API keys. The orchestration, gates, policy, tests, scans, docs and analysis are all real, but in offline mode the *content* of the spec, design and code edits is authored. Every artifact's provenance says so (`recorded`, with the playbook source), and reports state it at the top.
- **A live run has been captured.** `sample-runs/live-brownfield/` is a brownfield run with `claude-sonnet-5-5` (the model generated the spec, design and code). There were 4 human approvals, and 85/85 tests passed at 99% coverage, with no rework needed. The audit verifies, and provenance marks the spec, design and change set as `generated`. That is one successful run, not a measure of reliability across runs or scenarios; live greenfield (generating the whole service) has not been attempted and may need `ORCH_LLM_MAX_TOKENS` raised. Mocked-API tests cover the same path in CI. In live mode, copying the reference implementation is refused.
- **The `process` sandbox does not isolate the filesystem, and it only isolates the network on Linux.** Use `ORCH_SANDBOX=docker` for container isolation. The Docker backend was tested with a locally assembled image equivalent to `Dockerfile.sandbox`; the Dockerfile itself could not be built in the build environment (registry access was blocked).
- Single-process engine with thread-based parallelism. Agent threads cannot be force-killed, so timeouts are soft. Snapshots are full directory copies; git worktrees are the path to larger repos.
- The service targets a single SQLite writer and uses a per-instance rate limiter. Both are documented upgrade paths (Postgres, Redis).

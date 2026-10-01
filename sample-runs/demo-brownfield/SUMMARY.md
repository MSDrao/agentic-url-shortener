# Engineering summary: Brownfield: add limited-use links (max_clicks) to the existing service

> **SIMULATED APPROVALS.** Gate decisions in this run were scripted stand-ins recorded as `human: false`. This run demonstrates the workflow; it is **not** authorized for release.

- Run: `demo-brownfield`  |  scenario: `brownfield` (brownfield)
- Outcome: **SUCCEEDED**
- Release recommendation: **GO**
- Release sign-off: **simulated:eng-manager** (SIMULATED - not an authorization)
- Approvals: 0 human, 5 simulated
- Reasoning backend: `offline` (recorded playbook responses - content is authored, not generated); content origins in this run: computed, recorded
- Sandbox for generated code: process (network: none (netns); filesystem: not isolated (use docker backend))

## 1. Requirement understanding

> Marketing wants limited-use links for promotions, for example a code that is valid only for the
> first 100 visits. Add an optional maximum number of clicks when creating a link. Once the limit
> is reached, the short link must stop redirecting and respond 410 Gone. Link metadata must show the
> limit and the remaining clicks. Existing links, and clients that do not send the new field, must
> keep working unchanged.

**Normalized problem:** Support limited-use short links: a link may carry a maximum number of successful redirects, after which it stops redirecting - without changing behaviour for existing links or clients.

**Functional requirements**

- Accept an optional max_clicks (integer 1..1,000,000) when creating a link
- Once a link has served max_clicks redirects, GET /{code} returns 410 Gone and records nothing
- Link metadata exposes max_clicks and remaining_clicks (null when unlimited)
- Links without max_clicks behave exactly as before

**Non-functional requirements**

- Limit must hold under concurrent redirects (no overshoot)
- Schema change must be additive and backward compatible (old code can run on the new schema)
- No extra network calls on the redirect path

**Acceptance criteria**

| id | criterion |
|---|---|
| AC1 | Creating a link with max_clicks returns it with max_clicks and remaining_clicks; remaining decreases per redirect |
| AC2 | After max_clicks redirects the link returns 410 and further attempts are not counted |
| AC3 | max_clicks outside 1..1,000,000 or non-integer is rejected with 422 |
| AC4 | Links created without max_clicks are unlimited and existing behaviour is unchanged (regression) |
| AC5 | Under 20 concurrent redirects a max_clicks=5 link records exactly 5 clicks |
| AC6 | An existing v1 database migrates to v2 and existing links stay unlimited |

**Ambiguities -> resolution**

| id | term | question | resolution | status |
|---|---|---|---|---|
| Q1 | limit-accounting | Do rejected (over-limit) visits count in analytics? | no: only successful redirects are recorded as clicks | assumed |
| Q2 | mutability | Can max_clicks be changed after creation? | no: immutable in this change (out of scope) | assumed |
| Q3 | concurrency | Is small overshoot under concurrent load acceptable? | no: the limit is enforced atomically | assumed |

**Out of scope:** Changing max_clicks after creation; Custom landing page for exhausted links

## 2. Codebase reasoning (impact analysis)

Scanned 16 modules; data flow: `shortener.api -> shortener.service -> shortener.repository -> shortener.db`; risk **high** (schema and public API both impacted).

| module | reason | matched terms |
|---|---|---|
| shortener.db | direct | integer, migration, null, record, creat, click, link, code |
| shortener.api | direct | client, limit, redirect, creat, click, link, code |
| shortener.schemas | direct | chang, optional, short, creat, click, link, code |
| shortener.service | direct | gone, record, redirect, click, link, code |
| shortener.repository | direct | limit, record, creat, click, link, code |
| shortener.main | imports ['shortener.api'] |  |
| tests.conftest | imports ['shortener.api', 'shortener.db', 'shortener.repository', 'shortener.service'] |  |
| tests.test_api | imports ['shortener.api'] |  |
| tests.test_units | imports ['shortener.db'] |  |

Routes: `GET /healthz`, `GET /readyz`, `POST /api/v1/links`, `GET /api/v1/links/{code}`, `GET /api/v1/links/{code}/stats`, `DELETE /api/v1/links/{code}`, `GET /{code}`

Tables: `links`(id, code, target_url, created_at, expires_at, is_active); `clicks`(id, link_id, clicked_at, referrer, user_agent)

## 3. Design and task decomposition

Add a nullable links.max_clicks column (migration v2). Enforce the limit in the repository with a single conditional INSERT ... SELECT so check-and-record is atomic. The service maps 'not recorded' to 410 Gone (same semantics as expiry). The API contract change is additive: an optional request field and two nullable response fields, so existing clients are unaffected.

**API changes:** POST /api/v1/links: optional max_clicks (1..1,000,000); GET /api/v1/links/{code} and POST response: + max_clicks, remaining_clicks (nullable); GET /{code}: 410 when the click limit is reached

**Schema changes:** links.max_clicks (add nullable INTEGER (migration v2))

| task | title | deps | satisfies | files |
|---|---|---|---|---|
| T1 | Migration v2: nullable links.max_clicks | - | AC6 | shortener/db.py |
| T2 | Model + repository: persist max_clicks, atomic record_click_if_allowed | T1 | AC5 | shortener/models.py, shortener/repository.py |
| T3 | Service: validate range, enforce limit (410), remaining_clicks | T2 | AC2, AC3 | shortener/service.py |
| T4 | API contract: request/response fields | T3 | AC1, AC3, AC4 | shortener/schemas.py, shortener/api.py |
| T5 | Tests written from the ACs (test-first, parallel with T2) | T1 | AC1, AC2, AC3, AC4, AC5, AC6 | tests/test_max_clicks.py |

Files in the design that impact analysis did not predict (review focus): `shortener/models.py`

Execution waves (parallelizable): [T1] -> [T2, T5] -> [T3] -> [T4]

**Key decisions**

- **Limit enforcement** -> Single conditional INSERT ... SELECT ... WHERE count < max_clicks. Check-then-act in application code races under concurrency; one statement is atomic under SQLite's single writer and maps to a row lock / counter column on Postgres.
- **Response when exhausted** -> 410 Gone. Same semantics as expiry, which clients already handle.
- **remaining_clicks source** -> Derived on read via COUNT(*) over the (link_id, clicked_at) index. No second source of truth to keep consistent.

## 4. Orchestration trace

```mermaid
flowchart LR
    intake["intake<br/><small>requirements</small><br/><small>[succeeded]</small>"]
    codebase_analysis["codebase_analysis<br/><small>analysis</small><br/><small>[succeeded]</small>"]
    test_plan["test_plan<br/><small>test-design</small><br/><small>[succeeded]</small>"]
    design["design<br/><small>architecture</small><br/><small>[succeeded]</small>"]
    implement["implement<br/><small>implementation</small><br/><small>[succeeded]</small>"]
    docs["docs<br/><small>documentation</small><br/><small>[succeeded]</small>"]
    run_tests["run_tests<br/><small>testing</small><br/><small>[succeeded]</small>"]
    security_scan["security_scan<br/><small>security</small><br/><small>[succeeded]</small>"]
    release_readiness["release_readiness<br/><small>release</small><br/><small>[succeeded]</small>"]
    intake --> codebase_analysis
    intake --> test_plan
    intake --> design
    codebase_analysis --> design
    design --> implement
    test_plan --> implement
    implement --> run_tests
    run_tests -. rework .-> implement
    implement --> security_scan
    security_scan -. rework .-> implement
    implement --> docs
    run_tests --> release_readiness
    security_scan --> release_readiness
    docs --> release_readiness
```

| seq | node | event | detail |
|---|---|---|---|
| 3 | intake | running | agent=requirements_analyst attempt=1 |
| 6 | intake | waiting_approval | requirement contains ambiguities resolved only by assumptions |
| 7 | intake | **approval_decision** | {"answers": {}, "comment": "[SIMULATED] scripted demo decision - not a human sign-off", "decision": "approve", "human": false, "mode": "simulated", "request_hash": "6b1bb9a381c8d38c", "revise_target": null, "round": 1, " |
| 9 | intake | succeeded | 4 functional reqs, 6 ACs, 3 ambiguities (3 assumed), pii=False |
| 11 | codebase_analysis | running | agent=codebase_analyst attempt=1 |
| 13 | test_plan | running | agent=test_planner attempt=1 |
| 16 | test_plan | succeeded | 10 planned cases ({'unit': 4, 'integration': 6}) |
| 19 | codebase_analysis | succeeded | 16 modules, 7 routes, 2 tables; 5 directly + 4 indirectly impacted; risk=high |
| 21 | design | running | agent=architect attempt=1 |
| 24 | design | waiting_approval | high-impact change: public_api_change; high-impact change: schema_change |
| 25 | design | **approval_decision** | {"answers": {}, "comment": "[SIMULATED] scripted demo decision - not a human sign-off", "decision": "approve", "human": false, "mode": "simulated", "request_hash": "da15472302d50d1b", "revise_target": null, "round": 1, " |
| 27 | design | succeeded | 5 tasks in 4 waves; 3 API / 1 schema changes |
| 29 | implement | running | agent=implementer attempt=1 |
| 32 | implement | waiting_approval | high-impact change: public_api_change; high-impact change: schema_change; change to protected path: shortener/db.py |
| 33 | implement | **approval_decision** | {"answers": {}, "comment": "[SIMULATED] scripted demo decision - not a human sign-off", "decision": "approve", "human": false, "mode": "simulated", "request_hash": "b9f7c8654039bba0", "revise_target": null, "round": 1, " |
| 36 | implement | succeeded | 7 files (6 source, 1 test): max_clicks (first attempt) |
| 38 | docs | running | agent=tech_writer attempt=1 |
| 40 | run_tests | running | agent=test_runner attempt=1 |
| 42 | security_scan | running | agent=security_scanner attempt=1 |
| 45 | security_scan | succeeded | 7 files scanned, 0 findings (max=info) |
| 49 | docs | succeeded | API.md (7 routes), CHANGELOG, 3 ADRs |
| 51 | run_tests | **attempt_failed** | {"attempt": 1, "error": "exit gate failed: tests_pass (75/77 passed, 2 failed, 0 errors, exit_code=1); planned_tests_pass (planned tests not passing: ['tests/test_max_clicks.py::test_http_limit_reached_returns_410', 'tes |
| 52 | implement | **rollback** | {"to_snapshot": "pre-implement", "trigger": "run_tests"} |
| 58 | implement | running | agent=implementer attempt=1 |
| 61 | implement | waiting_approval | high-impact change: public_api_change; high-impact change: schema_change; change to protected path: shortener/db.py |
| 62 | implement | **approval_decision** | {"answers": {}, "comment": "[SIMULATED] scripted demo decision - not a human sign-off", "decision": "approve", "human": false, "mode": "simulated", "request_hash": "2a68d764631346bc", "revise_target": null, "round": 2, " |
| 65 | implement | succeeded | 7 files (6 source, 1 test): max_clicks: migration v2, atomic limit enforcement, additive API fields, tests |
| 67 | docs | running | agent=tech_writer attempt=1 |
| 69 | run_tests | running | agent=test_runner attempt=1 |
| 71 | security_scan | running | agent=security_scanner attempt=1 |
| 74 | security_scan | succeeded | 7 files scanned, 0 findings (max=info) |
| 78 | docs | succeeded | API.md (7 routes), CHANGELOG, 3 ADRs |
| 81 | run_tests | succeeded | 77/77 passed, coverage=98.94% |
| 83 | release_readiness | running | agent=release_manager attempt=1 |
| 85 | release_readiness | waiting_approval | 'release_readiness' always requires human sign-off |
| 86 | release_readiness | **approval_decision** | {"answers": {}, "comment": "[SIMULATED] scripted demo decision - not a human sign-off", "decision": "approve", "human": false, "mode": "simulated", "request_hash": "c0ed6c56a952aaed", "revise_target": null, "round": 1, " |
| 88 | release_readiness | succeeded | GO: 7/7 checks green |

**Gates (last evaluation per node)**

| node | gate | result | detail |
|---|---|---|---|
| intake | entry:inputs_available | pass | all inputs present |
| intake | exit:spec_complete | pass | 6 acceptance criteria |
| codebase_analysis | entry:inputs_available | pass | all inputs present |
| codebase_analysis | entry:workspace_has_code | pass | 16 python files |
| codebase_analysis | exit:impact_identified | pass | 9 impacted modules |
| test_plan | entry:inputs_available | pass | all inputs present |
| test_plan | exit:test_plan_covers_acs | pass | every AC has tests |
| design | entry:inputs_available | pass | all inputs present |
| design | exit:tasks_acyclic | pass | 5 tasks, acyclic |
| design | exit:traceability | pass | all ACs traced to tasks |
| implement | entry:inputs_available | pass | all inputs present |
| implement | exit:has_changes | pass | 7 files changed |
| implement | exit:compiles | pass | all python files compile |
| docs | entry:inputs_available | pass | all inputs present |
| docs | exit:docs_cover_routes | pass | 7 routes documented |
| run_tests | entry:inputs_available | pass | all inputs present |
| run_tests | entry:workspace_has_code | pass | 17 python files |
| run_tests | exit:tests_pass | pass | 77/77 passed, 0 failed, 0 errors, exit_code=0 |
| run_tests | exit:coverage_min | pass | 98.9% (min 85.0%) |
| run_tests | exit:planned_tests_pass | pass | 10 planned tests passed |
| security_scan | entry:inputs_available | pass | all inputs present |
| security_scan | exit:no_blocking_findings | pass | 0 blocking of 0 findings |
| release_readiness | entry:inputs_available | pass | all inputs present |
| release_readiness | exit:checklist_green | pass | 7 checks green |

## 5. Decisions and lineage

| id | node | kind | actor | summary |
|---|---|---|---|---|
| D001 | intake | approval | simulated:eng-manager | approve: requirement contains ambiguities resolved only by assumptions |
| D002 | intake | assumption | requirements_analyst | Q1: assumed 'no: only successful redirects are recorded as clicks' |
| D003 | intake | assumption | requirements_analyst | Q2: assumed 'no: immutable in this change (out of scope)' |
| D004 | intake | assumption | requirements_analyst | Q3: assumed 'no: the limit is enforced atomically' |
| D005 | design | approval | simulated:eng-manager | approve: high-impact change: public_api_change, high-impact change: schema_change |
| D006 | design | design_choice | architect | Limit enforcement: Single conditional INSERT ... SELECT ... WHERE count < max_clicks |
| D007 | design | design_choice | architect | Response when exhausted: 410 Gone |
| D008 | design | design_choice | architect | remaining_clicks source: Derived on read via COUNT(*) over the (link_id, clicked_at) index |
| D009 | implement | approval | simulated:eng-manager | approve: high-impact change: public_api_change, high-impact change: schema_change, change to protected path: shortener/db.py |
| D010 | run_tests | rollback | orchestrator | rolled back 'implement' and re-running it with failure feedback |
| D011 | implement | approval | simulated:eng-manager | approve: high-impact change: public_api_change, high-impact change: schema_change, change to protected path: shortener/db.py |
| D012 | release_readiness | approval | simulated:eng-manager | approve: 'release_readiness' always requires human sign-off |

**Artifact versions and content provenance**

| artifact | version | hash | producer | derived from | content origin | source / model | response sha256 |
|---|---|---|---|---|---|---|---|
| requirements_spec | 1 | 9fd815822c7c2d98 | intake | - | recorded | scenarios/playbooks/brownfield.yaml#requirements | 9ca219d2d7f0 |
| test_plan | 1 | 9e78b7d5379edbe8 | test_plan | requirements_spec@v1 | computed | test_planner | - |
| impact_analysis | 1 | 41a24b096fc81ad4 | codebase_analysis | requirements_spec@v1 | computed | codebase_analyst | - |
| design | 1 | 5270bc98ff5e0673 | design | requirements_spec@v1, impact_analysis@v1 | recorded | scenarios/playbooks/brownfield.yaml#design | 6bc058dcddee |
| change_set | 1 | 8fd45aa3f07fff2e | implement | requirements_spec@v1, design@v1, test_plan@v1 | recorded | scenarios/playbooks/brownfield.yaml#implement__buggy | 587d5b6a66b5 |
| change_set | 2 | 24ef5fd46f694a19 | implement | requirements_spec@v1, design@v1, test_plan@v1 | recorded | scenarios/playbooks/brownfield.yaml#implement | 34d675075d4a |
| security_report | 1 | d5a617e4a8458b6c | security_scan | - | computed | security_scanner | - |
| security_report | 2 | d5a617e4a8458b6c | security_scan | - | computed | security_scanner | - |
| docs_report | 1 | ebf4a25b946ba813 | docs | requirements_spec@v1, design@v1 | computed | tech_writer | - |
| docs_report | 2 | ebf4a25b946ba813 | docs | requirements_spec@v1, design@v1 | computed | tech_writer | - |
| test_report | 1 | ba1d7dfa3455f606 | run_tests | test_plan@v1 | computed | test_runner | - |
| release_readiness | 1 | 0d0591d4569e2c5c | release_readiness | requirements_spec@v1, design@v1, test_plan@v1, test_report@v1, security_report@v2, docs_report@v2 | computed | release_manager | - |

## 6. Validation

Tests: **77/77 passed**, coverage **98.94%** (`pytest -q -p no:cacheprovider --junitxml=<run_dir>/test-output/exec2-attempt1/junit.xml --cov=shortener --cov-report=json:<run_dir>/test-output/exec2-attempt1/coverage.json tests`)

**Traceability: acceptance criterion -> tasks -> tests -> result**

| AC | tasks | tests | verified |
|---|---|---|---|
| AC1 | T4, T5 | tests/test_max_clicks.py::test_http_create_with_max_clicks_shows_remaining | yes |
| AC2 | T3, T5 | tests/test_max_clicks.py::test_http_limit_reached_returns_410 | yes |
| AC3 | T3, T4, T5 | tests/test_max_clicks.py::test_http_invalid_max_clicks_rejected<br>tests/test_max_clicks.py::test_service_rejects_out_of_range_max_clicks | yes |
| AC4 | T4, T5 | tests/test_max_clicks.py::test_http_unlimited_links_unchanged<br>tests/test_api.py::test_redirect_is_307_and_counts_click<br>tests/test_api.py::test_stats_aggregate_by_day_and_referrer | yes |
| AC5 | T2, T5 | tests/test_max_clicks.py::test_repository_limit_is_atomic | yes |
| AC6 | T1, T5 | tests/test_max_clicks.py::test_migration_upgrades_existing_v1_database<br>tests/test_units.py::test_migrations_are_idempotent_and_versioned | yes |

Security scan: 7 files, 0 findings (max severity info).

**Release checklist**

| item | passed | evidence |
|---|---|---|
| full test suite green (pytest exit code 0) | True | 77/77, exit_code=0 |
| coverage >= 85.0% | True | 98.94% |
| every acceptance criterion verified by a passing test | True | 6/6 ACs |
| no blocking security findings | True | 0 findings, max=info |
| migrations forward-only and additive | True | no destructive statements |
| API reference covers all routes | True | 7 routes |
| rollback plan defined | True | Redeploy the previous version; migration v2 only adds a nullable column, so v1 code runs unchanged on the v2 schema and  |

## 7. Risks and trade-offs

| risk | likelihood | impact | mitigation | source |
|---|---|---|---|---|
| COUNT(*) per redirect on very hot limited links | low | medium | covered by index; switch to counter column if redirect p95 regresses | design |
| Old code (after rollback) ignores limits on links created with max_clicks | low | medium | documented in rollback plan; notify marketing on rollback | design |
| Clients with strict response schemas see two new fields | low | low | fields are additive and nullable | design |
| assumption Q1 unconfirmed: no: only successful redirects are recorded as clicks | medium | medium | confirm with product owner before GA | requirements |
| assumption Q2 unconfirmed: no: immutable in this change (out of scope) | medium | medium | confirm with product owner before GA | requirements |
| assumption Q3 unconfirmed: no: the limit is enforced atomically | medium | medium | confirm with product owner before GA | requirements |

**Rollback plan:** Redeploy the previous version; migration v2 only adds a nullable column, so v1 code runs unchanged on the v2 schema and no down-migration is needed. While rolled back, limited links behave as unlimited.

## 8. Reliability metrics

```json
{
  "end_to_end_latency_s": 5.04,
  "attempts": 13,
  "attempt_success_rate": 0.923,
  "retries": 0,
  "retry_rate": 0.0,
  "fallbacks": 0,
  "rollbacks": 1,
  "reworks": 1,
  "replans": 0,
  "approvals_requested": 5,
  "approvals_rejected": 0,
  "approvals_human": 0,
  "approvals_simulated": 5,
  "policy_violations": 0,
  "incidents_recovered": 1,
  "incidents_unrecovered": 0,
  "mttr_s": 2.562,
  "stage_latency_s": {
    "analysis": 0.03,
    "architecture": 0.001,
    "documentation": 0.846,
    "implementation": 0.006,
    "release": 0.006,
    "requirements": 0.0,
    "security": 0.07,
    "test-design": 0.0,
    "testing": 4.91
  }
}
```

## 9. Artifacts

- Reviewable change: `change.patch` (435 lines, 12 files)
- Workspace with the change applied: `workspace/`
- Hash-chained audit log: `audit.jsonl` (verify with `python -m orchestrator verify-audit <run_id>`)
- Resumable state: `state.json`; metrics: `metrics.json`; graph: `graph.mmd`; test output: `test-output/`

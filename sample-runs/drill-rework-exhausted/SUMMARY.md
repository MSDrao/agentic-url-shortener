# Engineering summary: Drill: repeated test failures exhaust the rework budget -> safe-stop

- Run: `drill-rework-exhausted`  |  scenario: `drill-rework-exhausted` (brownfield)
- Outcome: **HALTED** (reason: 'run_tests' failed after retries/fallback)
- Release recommendation: **n/a**

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
    run_tests["run_tests<br/><small>testing</small><br/><small>[failed]</small>"]
    security_scan["security_scan<br/><small>security</small><br/><small>[succeeded]</small>"]
    release_readiness["release_readiness<br/><small>release</small><br/><small>[pending]</small>"]
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
| 5 | intake | waiting_approval | requirement contains ambiguities resolved only by assumptions |
| 6 | intake | **approval_decision** | {"answers": {}, "comment": "auto-approved under scripted policy", "decision": "approve", "revise_target": null, "round": 1, "wait_s": 0.0} |
| 8 | intake | succeeded | 4 functional reqs, 6 ACs, 3 ambiguities (3 assumed), pii=False |
| 10 | codebase_analysis | running | agent=codebase_analyst attempt=1 |
| 12 | test_plan | running | agent=test_planner attempt=1 |
| 15 | test_plan | succeeded | 10 planned cases ({'unit': 4, 'integration': 6}) |
| 18 | codebase_analysis | succeeded | 16 modules, 7 routes, 2 tables; 5 directly + 4 indirectly impacted; risk=high |
| 20 | design | running | agent=architect attempt=1 |
| 22 | design | waiting_approval | high-impact change: public_api_change; high-impact change: schema_change |
| 23 | design | **approval_decision** | {"answers": {}, "comment": "auto-approved under scripted policy", "decision": "approve", "revise_target": null, "round": 1, "wait_s": 0.0} |
| 25 | design | succeeded | 5 tasks in 4 waves; 3 API / 1 schema changes |
| 27 | implement | running | agent=implementer attempt=1 |
| 29 | implement | waiting_approval | high-impact change: public_api_change; high-impact change: schema_change; change to protected path: shortener/db.py |
| 30 | implement | **approval_decision** | {"answers": {}, "comment": "auto-approved under scripted policy", "decision": "approve", "revise_target": null, "round": 1, "wait_s": 0.0} |
| 33 | implement | succeeded | 7 files (6 source, 1 test): max_clicks (first attempt) |
| 35 | docs | running | agent=tech_writer attempt=1 |
| 37 | run_tests | running | agent=test_runner attempt=1 |
| 39 | security_scan | running | agent=security_scanner attempt=1 |
| 42 | security_scan | succeeded | 7 files scanned, 0 findings (max=info) |
| 46 | docs | succeeded | API.md (7 routes), CHANGELOG, 3 ADRs |
| 48 | run_tests | **attempt_failed** | {"attempt": 1, "error": "exit gate failed: tests_pass (61/63 passed, 2 failed, 0 errors); planned_tests_pass (planned tests not passing: ['tests/test_max_clicks.py::test_http_limit_reached_returns_410', 'tests/test_max_c |
| 49 | implement | **rollback** | {"to_snapshot": "pre-implement", "trigger": "run_tests"} |
| 55 | implement | running | agent=implementer attempt=1 |
| 57 | implement | waiting_approval | high-impact change: public_api_change; high-impact change: schema_change; change to protected path: shortener/db.py |
| 58 | implement | **approval_decision** | {"answers": {}, "comment": "auto-approved under scripted policy", "decision": "approve", "revise_target": null, "round": 2, "wait_s": 0.0} |
| 61 | implement | succeeded | 7 files (6 source, 1 test): max_clicks (first attempt) |
| 63 | docs | running | agent=tech_writer attempt=1 |
| 65 | run_tests | running | agent=test_runner attempt=1 |
| 67 | security_scan | running | agent=security_scanner attempt=1 |
| 70 | security_scan | succeeded | 7 files scanned, 0 findings (max=info) |
| 74 | docs | succeeded | API.md (7 routes), CHANGELOG, 3 ADRs |
| 76 | run_tests | **attempt_failed** | {"attempt": 1, "error": "exit gate failed: tests_pass (61/63 passed, 2 failed, 0 errors); planned_tests_pass (planned tests not passing: ['tests/test_max_clicks.py::test_http_limit_reached_returns_410', 'tests/test_max_c |
| 77 | implement | **rollback** | {"to_snapshot": "pre-implement", "trigger": "run_tests"} |
| 83 | implement | running | agent=implementer attempt=1 |
| 85 | implement | waiting_approval | high-impact change: public_api_change; high-impact change: schema_change; change to protected path: shortener/db.py |
| 86 | implement | **approval_decision** | {"answers": {}, "comment": "auto-approved under scripted policy", "decision": "approve", "revise_target": null, "round": 3, "wait_s": 0.0} |
| 89 | implement | succeeded | 7 files (6 source, 1 test): max_clicks (first attempt) |
| 91 | docs | running | agent=tech_writer attempt=1 |
| 93 | run_tests | running | agent=test_runner attempt=1 |
| 95 | security_scan | running | agent=security_scanner attempt=1 |
| 98 | security_scan | succeeded | 7 files scanned, 0 findings (max=info) |
| 102 | docs | succeeded | API.md (7 routes), CHANGELOG, 3 ADRs |
| 104 | run_tests | **attempt_failed** | {"attempt": 1, "error": "exit gate failed: tests_pass (61/63 passed, 2 failed, 0 errors); planned_tests_pass (planned tests not passing: ['tests/test_max_clicks.py::test_http_limit_reached_returns_410', 'tests/test_max_c |
| 105 | run_tests | failed | rework budget exhausted for 'implement': exit gate failed: tests_pass (61/63 passed, 2 failed, 0 errors); planned_tests_pass (planned tests not passing: ['tests/test_max_clicks.py::test_http_limit_reached_returns_410', 'tests/test_max_clicks.py::test_repository_limit_is_atomic']) |
| 106 | - | **safe_stop** | {"reason": "'run_tests' failed after retries/fallback"} |

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
| run_tests | exit:tests_pass | FAIL | 61/63 passed, 2 failed, 0 errors |
| run_tests | exit:coverage_min | pass | 98.7% (min 85.0%) |
| run_tests | exit:planned_tests_pass | FAIL | planned tests not passing: ['tests/test_max_clicks.py::test_http_limit_reached_returns_410', 'tests/test_max_clicks.py::test_repository_limit_is_atomic'] |
| security_scan | entry:inputs_available | pass | all inputs present |
| security_scan | exit:no_blocking_findings | pass | 0 blocking of 0 findings |

## 5. Decisions and lineage

| id | node | kind | actor | summary |
|---|---|---|---|---|
| D001 | intake | approval | eng-manager@example.com | approve: requirement contains ambiguities resolved only by assumptions |
| D002 | intake | assumption | requirements_analyst | Q1: assumed 'no: only successful redirects are recorded as clicks' |
| D003 | intake | assumption | requirements_analyst | Q2: assumed 'no: immutable in this change (out of scope)' |
| D004 | intake | assumption | requirements_analyst | Q3: assumed 'no: the limit is enforced atomically' |
| D005 | design | approval | eng-manager@example.com | approve: high-impact change: public_api_change, high-impact change: schema_change |
| D006 | design | design_choice | architect | Limit enforcement: Single conditional INSERT ... SELECT ... WHERE count < max_clicks |
| D007 | design | design_choice | architect | Response when exhausted: 410 Gone |
| D008 | design | design_choice | architect | remaining_clicks source: Derived on read via COUNT(*) over the (link_id, clicked_at) index |
| D009 | implement | approval | eng-manager@example.com | approve: high-impact change: public_api_change, high-impact change: schema_change, change to protected path: shortener/db.py |
| D010 | run_tests | rollback | orchestrator | rolled back 'implement' and re-running it with failure feedback |
| D011 | implement | approval | eng-manager@example.com | approve: high-impact change: public_api_change, high-impact change: schema_change, change to protected path: shortener/db.py |
| D012 | run_tests | rollback | orchestrator | rolled back 'implement' and re-running it with failure feedback |
| D013 | implement | approval | eng-manager@example.com | approve: high-impact change: public_api_change, high-impact change: schema_change, change to protected path: shortener/db.py |
| D014 | - | safe_stop | orchestrator | 'run_tests' failed after retries/fallback |

**Artifact versions**

| artifact | version | hash | producer | derived from |
|---|---|---|---|---|
| requirements_spec | 1 | 9fd815822c7c2d98 | intake | - |
| test_plan | 1 | 9e78b7d5379edbe8 | test_plan | requirements_spec@v1 |
| impact_analysis | 1 | 41a24b096fc81ad4 | codebase_analysis | requirements_spec@v1 |
| design | 1 | 5270bc98ff5e0673 | design | requirements_spec@v1, impact_analysis@v1 |
| change_set | 1 | 8fd45aa3f07fff2e | implement | requirements_spec@v1, design@v1, test_plan@v1 |
| change_set | 2 | 8fd45aa3f07fff2e | implement | requirements_spec@v1, design@v1, test_plan@v1 |
| change_set | 3 | 8fd45aa3f07fff2e | implement | requirements_spec@v1, design@v1, test_plan@v1 |
| security_report | 1 | d5a617e4a8458b6c | security_scan | - |
| security_report | 2 | d5a617e4a8458b6c | security_scan | - |
| security_report | 3 | d5a617e4a8458b6c | security_scan | - |
| docs_report | 1 | ebf4a25b946ba813 | docs | requirements_spec@v1, design@v1 |
| docs_report | 2 | ebf4a25b946ba813 | docs | requirements_spec@v1, design@v1 |
| docs_report | 3 | ebf4a25b946ba813 | docs | requirements_spec@v1, design@v1 |

## 6. Validation

Security scan: 7 files, 0 findings (max severity info).

## 7. Risks and trade-offs

| risk | mitigation |
|---|---|
| COUNT(*) per redirect on very hot limited links | covered by index; switch to counter column if redirect p95 regresses |
| Old code (after rollback) ignores limits on links created with max_clicks | documented in rollback plan; notify marketing on rollback |
| Clients with strict response schemas see two new fields | fields are additive and nullable |

**Rollback plan:** Redeploy the previous version; migration v2 only adds a nullable column, so v1 code runs unchanged on the v2 schema and no down-migration is needed. While rolled back, limited links behave as unlimited.

## 8. Reliability metrics

```json
{
  "end_to_end_latency_s": 7.595,
  "attempts": 16,
  "attempt_success_rate": 0.812,
  "retries": 0,
  "retry_rate": 0.0,
  "fallbacks": 0,
  "rollbacks": 2,
  "reworks": 2,
  "replans": 0,
  "approvals_requested": 5,
  "approvals_rejected": 0,
  "policy_violations": 0,
  "incidents_recovered": 0,
  "incidents_unrecovered": 1,
  "mttr_s": null,
  "stage_latency_s": {
    "analysis": 0.023,
    "architecture": 0.0,
    "documentation": 1.211,
    "implementation": 0.008,
    "requirements": 0.0,
    "security": 0.061,
    "test-design": 0.0,
    "testing": 7.491
  }
}
```

## 9. Artifacts

- Reviewable change: `change.patch` (435 lines, 0 files)
- Workspace with the change applied: `workspace/`
- Hash-chained audit log: `audit.jsonl` (verify with `python -m orchestrator verify-audit <run_id>`)
- Resumable state: `state.json`; metrics: `metrics.json`; graph: `graph.mmd`; test output: `test-output/`

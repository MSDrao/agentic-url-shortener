# Scenarios

Each scenario is a YAML file under `scenarios/` containing:

- the raw requirement;
- the baseline (`null` for an empty workspace, or `service` for a copy of the current codebase);
- the playbook (the recorded agent responses);
- optional fault injection;
- an optional `simulated_approvals` script, used **only** with `--approvals simulated`, where the decisions are recorded as `human: false`.

The numbers below come from `make demo`, which runs with **simulated approvals** so it can finish unattended. Every report is stamped "not authorized for release" for that reason. To make the decisions yourself, see [section 5](#5-running-a-scenario-with-your-own-decisions). The full reports are in `sample-runs/<run>/SUMMARY.md`. Timings vary between machines.

---

## 1. Greenfield: build the service from scratch

**Requirement (well defined):** a URL shortener with create, redirect with click recording, metadata and analytics, deactivation, optional API key, rate limiting, unsafe-URL rejection, and health endpoints.

**Requirement understanding.** The agent normalized the requirement into 7 functional requirements, 4 non-functional requirements and **11 acceptance criteria**. The text contained no ambiguous wording, but the agent still raised two design-level questions and answered them with safe defaults. Because nobody had confirmed those answers, the policy required a human to sign off on the assumptions.

| Question | Default (assumed) |
|---|---|
| Q1: 301 or 307 redirect? | 307, so clicks keep reaching the service and are counted |
| Q2: Datastore? | SQLite behind a repository |

**Decomposition.** 7 tasks in 5 execution waves:

```
wave 1: T1 config/errors/models, T4 rate limiter
wave 2: T2 persistence, T3 codes+validation
wave 3: T5 service
wave 4: T6 HTTP API
wave 5: T7 test suites
```

Every acceptance criterion traces to at least one task (the `traceability` gate) and at least one test (the `test_plan_covers_acs` gate).

**Orchestration.**

- **Transient failure:** the first design call fails with a simulated provider error. The node retries after a 0.2 s backoff and succeeds, which is recorded as a recovered incident (MTTR ≈ 0.2 s).
- **Parallel work:** `design ∥ test_plan` run together, then `run_tests ∥ security_scan ∥ docs`.
- **Approval checkpoints:**
  - intake: unconfirmed assumptions;
  - design: public API and schema;
  - implement: dependency manifest, API and schema;
  - release: always requires sign-off.

**Validation.**

- 67/67 tests pass with about 98–99% coverage (pytest exit code 0 is also required).
- All 11 acceptance criteria are verified by passing tests.
- The security scan reports one *medium* finding (a new dependency manifest, which calls for a supply-chain review). It is below the blocking threshold and appears in the risk register.
- The docs agent generated `docs/API.md` from the live OpenAPI contract (7 routes), a CHANGELOG entry and 4 ADRs.
- Release checklist 7/7: **GO**.

---

## 2. Brownfield: limited-use links (`max_clicks`)

**Requirement:** add an optional click limit. When the limit is reached, return 410. Show `remaining_clicks`. Existing links and clients must not change behaviour.

**Codebase reasoning.** This is real AST analysis of the workspace. It found:

- 16 modules, 7 routes, and 2 tables (`links`, `clicks`);
- the data flow `api → service → repository → db`;
- directly impacted, by TF-IDF relevance: `db`, `api`, `schemas`, `service`, `repository`;
- indirectly impacted, via reverse imports: `main` and three test modules;
- risk: **high**, because both the schema and the public API are touched.

The design also touched `models.py`, which the analysis did not predict. The architect reports it as `unpredicted_files`, so the gap is visible to reviewers.

**Requirement understanding.** The agent produced 6 acceptance criteria and 3 questions with safe defaults:

| Question | Default |
|---|---|
| Do over-limit hits count in analytics? | No |
| Can `max_clicks` change after creation? | No |
| Is overshoot acceptable under concurrency? | No; the limit is enforced atomically |

**Decomposition.**

```
T1 migration v2 → T2 model + atomic repository insert → T3 service rules → T4 API contract
T5 tests (depends only on T1, so it runs in parallel with T2)
```

**Key design decision.** The limit is enforced with a single conditional `INSERT … SELECT … WHERE count < max_clicks`. Checking the count and then inserting would race under concurrent redirects. AC5 proves the chosen approach with 20 concurrent redirects against `max_clicks=5`, which records exactly 5 clicks.

**Orchestration: rework and rollback.**

1. The first implementation contains a realistic off-by-one: it uses `<=` where it should use `<`.
2. `run_tests` fails: 75/77 pass, 2 planned tests fail, and pytest exits with code 1.
3. The workspace is rolled back to the `pre-implement` snapshot.
4. `implement` re-runs with the failing test names in its feedback. `run_tests`, `security_scan` and `docs` are invalidated and re-run.
5. Result: 77/77 pass. Metrics: rollbacks=1, reworks=1, MTTR ≈ 2.6 s.

**Change control.**

- The migration is additive (a nullable column), and the release checklist checks the diff for `DROP`, `RENAME` and `DELETE`.
- `db.py` is a protected path, so the change needs human approval.
- The rollback plan is documented: the v1 code runs unchanged against the v2 schema.

**Output.** `change.patch` (7 files: 6 source and 1 test), plus generated API docs, a changelog entry and 3 ADRs. Release checklist 7/7: **GO**.

---

## 3. Ambiguous: "make links safer and show who is clicking"

**Requirement (deliberately vague):** "Our short links need to be safer, and marketing wants to see who is clicking them. Please get this done quickly without slowing redirects down."

**Ambiguity detection.** The lexicon flags *safer*, *who*, *quickly* and *slowing*. Every flagged term must become a question; if the backend skips one, the agent adds it. The resulting 6 questions:

| Question | Safe default |
|---|---|
| Q1: What does "safer" mean? | operator-managed domain denylist |
| Q2: Does the denylist apply to existing links? | `creation_only` |
| Q3: Does "who" mean identifying individuals? | no: aggregate, pseudonymous counts |
| Q4: Is "quickly" a deadline or a latency requirement? | latency |
| Q5: What is the latency budget? | no new I/O on the redirect path |
| Q6: How long is data retained? | 90 days |

**Guardrail and dynamic re-planning.**

1. "who is clicking" matches a policy PII signal, so the spec is flagged `pii_involved`.
2. The agent asks for a `privacy_review` node. Policy allowlists that node, so the engine **inserts it between `design` and `implement` at runtime**. The graph above shows it as a hexagon.

**Revision at the design review.** In the demo, the scripted (simulated) security reviewer chooses **revise**. Run it with `--approvals interactive` or `queue` to make that call yourself: the change is sent back to `intake` with `Q2 = create_and_redirect` ("denylisted domains must also stop existing links").

1. `intake` re-runs. Q2 is now *confirmed*, and the spec moves from v1 to v2 with 5 functional requirements (was 4) and 7 acceptance criteria (was 6).
2. The engine compares hashes, sees that `requirements_spec` changed, and **invalidates** `codebase_analysis` and `test_plan`, which had already completed. `design` re-runs as well.
3. The new design adds task **T2b** (enforce the denylist on redirect), a new acceptance criterion (AC2), and a new test. It also drops the risk "existing links keep working", which the revised scope resolves.
4. The implementation includes redirect-time enforcement. `test_ambiguous_revision_changes_the_delivered_code` asserts this.

**Privacy review (5/5 checks):**

- no raw identifiers are persisted;
- identifiers are pseudonymized with a keyed hash (HMAC with a server secret);
- retention is 90 days or less;
- purpose limitation is documented;
- visitors cannot be linked across days (the day is part of the hash input).

Tests prove that the raw IP never reaches storage and that expired hashes are purged while click counts are kept.

**Lineage.** `requirements_spec@v2` has `derived_from: [clarifications@v1]`, which was produced by `human:product-owner@example.com`. Every downstream artifact chains back to it. See section 5 of the run's SUMMARY.

**Result.** 76/76 tests pass. 7 approvals were requested, including the revise round. Re-plans=3: the privacy-review insertion, the revise, and the hash invalidation. Release checklist 8/8 (it includes the privacy review): **GO**.

---

## 4. Failure drills

| Drill | Injected condition | Engine behaviour | Final state |
|---|---|---|---|
| `release-rejected` | the (simulated) reviewer rejects at the final gate | release node FAILED; workspace **restored to baseline** (the patch is empty) | `rolled_back` |
| `policy-violation` | the implementer also proposes `scripts/deploy.sh` (outside its write scope) | CRITICAL autonomy-boundary finding → nothing committed → **safe-stop**; `resume --reset-failed` completes the run, and MTTR spans the human intervention | `halted` → `succeeded` |
| `rework-exhausted` | the implementation is wrong on every attempt | rollback + rework twice (the policy's `max_rework_cycles`), then `run_tests` FAILED → safe-stop | `halted` |

Other controls are covered by the orchestrator unit tests: the kill switch (`orchestrator stop`), the attempt and wall-clock budgets, fallback-agent switching, stale-result discard, and policy-denied node insertion.

## 5. Running a scenario with your own decisions

```bash
python -m orchestrator run scenarios/ambiguous.yaml --approvals interactive
```

At each gate you see the reasons, the summary and the evidence (spec, design and files), and you choose one of:

- `a`: approve;
- `r`: reject;
- `v`: revise. You pick the target node, give feedback, and optionally supply answers such as `{"Q2": "create_and_redirect"}`.

Or review asynchronously. The run pauses at each gate until a decision is recorded:

```bash
python -m orchestrator run scenarios/ambiguous.yaml --approvals queue --run-id amb
python -m orchestrator pending amb
python -m orchestrator approve amb intake --decision approve --as <you> --comment "assumptions acceptable"
python -m orchestrator resume amb
python -m orchestrator approve amb design --decision revise --target intake --answers '{"Q2": "create_and_redirect"}' --as <you>
python -m orchestrator resume amb     # ... repeat until the release gate
```

# Sample runs

These are the outputs of `make demo`, generated offline with approvals decided by the scenario scripts. Regenerate them with `make demo`, which writes to `runs/`.

| Run | Final state | Start with |
|---|---|---|
| `demo-greenfield` | succeeded (GO) | `SUMMARY.md`, then `generated-docs/API.md` |
| `demo-brownfield` | succeeded (GO) after one rework and rollback | `SUMMARY.md` §4 (trace), `change.patch` |
| `demo-ambiguous` | succeeded (GO) after a human revision and re-planning | `SUMMARY.md` §1 (ambiguities), §4 (re-plan events), §5 (lineage) |
| `drill-release-rejected` | rolled_back (empty patch) | `SUMMARY.md` §4 |
| `drill-policy-violation` | halted, then succeeded after `resume --reset-failed` | `SUMMARY.md` §4 (`safe_stop`, `run_resumed`) |
| `drill-rework-exhausted` | halted after two bounded rework cycles | `SUMMARY.md` §4 |

Each run directory contains:

| File | Contents |
|---|---|
| `SUMMARY.md` | the engineering summary |
| `change.patch` | the diff against the baseline |
| `audit.jsonl` | the hash-chained audit log. It can be checked with `AuditLog.verify` in `orchestrator/audit.py`. |
| `state.json` | the complete run state, including every artifact version and decision |
| `metrics.json` | reliability metrics |
| `graph.mmd` | the final dependency graph with node statuses |

The `workspace/` copies and snapshots are left out to keep the folder small. `demo-console.log` is the console output of the demo.

# ADR-003: Denylist source

Status: accepted

## Decision
Operator-managed config list with parent-domain matching

## Rationale
No new network dependency on the redirect path; deterministic; feeds can populate it later.

## Alternatives
- Live threat-intel API call per redirect (latency + availability coupling)

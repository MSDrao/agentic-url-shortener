# ADR-004: Click recording

Status: accepted

## Decision
Synchronous insert on the redirect path

## Rationale
Simplest correct option; sub-millisecond at v1 scale; no loss-on-crash semantics to explain.

## Alternatives
- Async queue with batch writes (better p99 at scale, adds delivery semantics)

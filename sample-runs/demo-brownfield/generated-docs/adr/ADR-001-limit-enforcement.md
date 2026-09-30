# ADR-001: Limit enforcement

Status: accepted

## Decision
Single conditional INSERT ... SELECT ... WHERE count < max_clicks

## Rationale
Check-then-act in application code races under concurrency; one statement is atomic under SQLite's single writer and maps to a row lock / counter column on Postgres.

## Alternatives
- Read count then insert (overshoots under concurrency)
- Denormalized counter column (faster at scale; needs backfill and dual-write)

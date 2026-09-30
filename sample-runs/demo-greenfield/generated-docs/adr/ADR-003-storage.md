# ADR-003: Storage

Status: accepted

## Decision
SQLite with versioned forward-only migrations behind a repository

## Rationale
Zero-ops for v1; the repository boundary keeps a Postgres migration local to one module.

## Alternatives
- Postgres now (ops cost before product fit)
- In-memory map (no durability)

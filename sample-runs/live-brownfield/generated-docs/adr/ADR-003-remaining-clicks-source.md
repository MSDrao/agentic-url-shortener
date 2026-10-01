# ADR-003: remaining_clicks source

Status: accepted

## Decision
Derived on read as max(0, max_clicks - COUNT(clicks for link))

## Rationale
The clicks table stays the single source of truth, so there is nothing to keep consistent and no backfill.

## Alternatives
- Stored remaining counter

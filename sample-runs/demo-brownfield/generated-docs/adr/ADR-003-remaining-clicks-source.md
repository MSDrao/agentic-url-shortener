# ADR-003: remaining_clicks source

Status: accepted

## Decision
Derived on read via COUNT(*) over the (link_id, clicked_at) index

## Rationale
No second source of truth to keep consistent.

## Alternatives
- Stored counter

# ADR-001: Limit enforcement

Status: accepted

## Decision
Single conditional INSERT ... SELECT ... WHERE max_clicks IS NULL OR (SELECT COUNT(*) FROM clicks WHERE link_id = ?) < max_clicks, checking rowcount

## Rationale
A check-then-act sequence in application code races under concurrent redirects. One SQL statement is atomic under SQLite's single-writer lock (with a busy timeout for contending writers), needs no extra round trip, and does the limit check and click recording in one datastore operation as required.

## Alternatives
- Read count then insert (overshoots under concurrency)
- Denormalized click counter column with UPDATE ... WHERE counter < max (needs backfill and dual-write with the clicks table, and the extra schema is not needed at this scale)
- In-process lock (does not hold across multiple workers/processes)

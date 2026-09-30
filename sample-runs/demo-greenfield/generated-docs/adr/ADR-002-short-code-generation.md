# ADR-002: Short code generation

Status: accepted

## Decision
Cryptographically random base62, length 7, bounded retry on collision

## Rationale
Non-enumerable (codes cannot be walked), no coordination between replicas, 62^7 ~ 3.5e12 space.

## Alternatives
- Auto-increment id in base62 (enumerable, leaks volume)
- Hash of URL (same URL -> same code prevents per-campaign links)

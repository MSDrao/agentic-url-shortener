# ADR-002: Response when exhausted

Status: accepted

## Decision
410 Gone

## Rationale
Same semantics as expiry, which clients already handle.

## Alternatives
- 404 (hides that the link existed)
- Redirect to a landing page (needs product input)

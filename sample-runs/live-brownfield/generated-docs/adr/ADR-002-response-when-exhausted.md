# ADR-002: Response when exhausted

Status: accepted

## Decision
410 Gone with the standard JSON error body; unknown codes stay 404

## Rationale
Matches the spec and lets clients and marketing tooling distinguish an exhausted link from one that never existed.

## Alternatives
- 404 (hides that the link existed)
- Redirect or HTML landing page (needs product input, out of scope)

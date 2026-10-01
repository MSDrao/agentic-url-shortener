# ADR-004: Request validation location

Status: accepted

## Decision
Strict integer validation at both the schema layer (422) and the service/validation layer

## Rationale
The service tests call the service directly, and bool is a subclass of int in Python, so it must be rejected explicitly in addition to relying on schema strictness.

## Alternatives
- Schema-only validation (direct service callers could bypass it)
- Coercing numeric strings or floats (violates the 422 requirement)

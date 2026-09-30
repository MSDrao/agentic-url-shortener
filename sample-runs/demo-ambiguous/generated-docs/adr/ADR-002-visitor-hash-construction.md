# ADR-002: Visitor hash construction

Status: accepted

## Decision
HMAC-SHA256 with a server secret, day in the input

## Rationale
Unkeyed hashes of IPv4 are trivially brute-forced; the day component prevents cross-day tracking.

## Alternatives
- sha256(ip) (reversible by enumeration)
- Static salt (enables long-term tracking)

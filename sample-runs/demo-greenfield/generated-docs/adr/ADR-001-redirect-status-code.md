# ADR-001: Redirect status code

Status: accepted

## Decision
307 Temporary Redirect with Cache-Control max-age=0

## Rationale
301 is cached by browsers, so repeat clicks bypass the service and analytics undercount; 307 also preserves the method.

## Alternatives
- 301 Moved Permanently (cached, breaks analytics)
- 302 Found (legacy method-change ambiguity)

# Changelog

## [Unreleased] - 2026-09-30

### Destination denylist + privacy-preserving visitor analytics

- Operator-managed destination denylist (SHORTENER_BLOCKED_DOMAINS), including subdomains
- Denylisted destinations also stop existing links (410)
- Stats: `unique_visitors` from a keyed, daily-rotating pseudonymous hash (raw IPs are never stored)
- Retention: `LinkService.purge_visitor_data()` for the daily retention job
- DB migration v2: nullable `clicks.visitor_hash`

Assumptions (not confirmed by stakeholders):
- Q1: block harmful destinations via an operator-managed domain denylist
- Q3: no: aggregate unique-visitor counts from a pseudonymous daily hash; no personal profiles
- Q4: latency (read together with 'without slowing redirects'); no deadline was given
- Q5: no new I/O on the redirect path; one HMAC (~microseconds); p95 unchanged
- Q6: 90 days (policy maximum)

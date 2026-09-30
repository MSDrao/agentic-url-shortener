# Changelog

## [Unreleased] - 2026-09-30

### URL shortener service v1

- Create, read, deactivate short links (custom alias, expiry)
- 307 redirect with click recording; stats by day and referrer
- API-key protection for writes, per-client rate limiting, unsafe-URL rejection
- Health/readiness endpoints, request IDs, versioned migrations

Assumptions (not confirmed by stakeholders):
- Q1: 307: browsers do not cache it, so every click reaches the service and is counted
- Q2: SQLite behind a repository interface; Postgres is a later, local swap

# Changelog

## [Unreleased] - 2026-09-30

### Limited-use links (max_clicks)

- Add optional `max_clicks` to link creation; exhausted links return 410 Gone
- Link metadata includes `max_clicks` and `remaining_clicks`
- DB migration v2: nullable `links.max_clicks`

Assumptions (not confirmed by stakeholders):
- Q1: no: only successful redirects are recorded as clicks
- Q2: no: immutable in this change (out of scope)
- Q3: no: the limit is enforced atomically

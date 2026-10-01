# Changelog

## [Unreleased] - 2026-09-30

### Limited-use links (max_clicks)

- Add optional `max_clicks` (integer 1..1,000,000) to link creation; exhausted links return 410 Gone and record no further clicks
- Link metadata and stats include `max_clicks` and `remaining_clicks` (null for unlimited links)
- Limit is enforced atomically in a single conditional insert, with no overshoot under concurrent redirects
- DB migration v2: nullable `links.max_clicks` (existing links stay unlimited) and an index on clicks(link_id)

Assumptions (not confirmed by stakeholders):
- Q1: no: only successful redirects are recorded as clicks
- Q2: no: immutable in this change (out of scope)
- Q3: no: the limit is enforced atomically
- Q4: integer 1..1,000,000 inclusive; 0, negatives and non-integers return 422
- Q5: standard JSON error body with status 410; unknown codes keep returning 404
- Q6: both fields present with null value
- Q7: no: existing links remain unlimited (max_clicks null)
- Q8: each successful redirect response; HEAD or prefetch requests are treated the same as GET if the current code already records them, with no new filtering

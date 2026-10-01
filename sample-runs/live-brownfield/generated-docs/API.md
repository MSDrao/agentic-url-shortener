# URL Shortener API reference

Version 1.0.0. Generated from the OpenAPI contract - do not edit by hand.

## `GET /healthz`

Tags: ops

Responses: `200`

## `GET /readyz`

Tags: ops

Responses: `200`

## `POST /api/v1/links`

Tags: links
Parameters: `x-api-key` (header)

Request body:
- `url` (string, required) Destination http(s) URL
- `custom_alias` (string/null) Optional vanity code: 3-32 chars [A-Za-z0-9_-]
- `expires_at` (string/null) Optional expiry (ISO-8601). Naive times are UTC.
- `max_clicks` (integer/null) Optional click limit (integer 1..1,000,000). Omit or null for unlimited.

Responses: `201`, `401`, `404`, `409`, `410`, `422`, `429`

## `GET /api/v1/links/{code}`

Tags: links
Parameters: `code` (path)

Responses: `200`, `401`, `404`, `409`, `410`, `422`, `429`

## `DELETE /api/v1/links/{code}`

Tags: links
Parameters: `code` (path), `x-api-key` (header)

Responses: `204`, `401`, `404`, `409`, `410`, `422`, `429`

## `GET /api/v1/links/{code}/stats`

Tags: analytics
Parameters: `code` (path)

Responses: `200`, `401`, `404`, `409`, `410`, `422`, `429`

## `GET /{code}`

Tags: redirect
Parameters: `code` (path)

Responses: `200`, `401`, `404`, `409`, `410`, `422`, `429`


"""HTTP layer: routing, auth, rate limiting, error mapping, request tracing."""

from __future__ import annotations

import hmac
import logging
import time
import uuid

from fastapi import Depends, FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from . import __version__
from .config import Settings
from .db import Database
from .errors import DomainError
from .models import Link
from .ratelimit import TokenBucketLimiter
from .repository import LinkRepository
from .schemas import (
    CreateLinkRequest,
    DayCount,
    ErrorResponse,
    LinkResponse,
    ReferrerCount,
    StatsResponse,
)
from .service import LinkService

logger = logging.getLogger("shortener")

ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    410: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


class _HTTPError(Exception):
    def __init__(self, status_code: int, detail: str, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


def _to_response(link: Link, settings: Settings) -> LinkResponse:
    return LinkResponse(
        code=link.code,
        short_url=f"{settings.base_url}/{link.code}",
        target_url=link.target_url,
        created_at=link.created_at,
        expires_at=link.expires_at,
        is_active=link.is_active,
    )


def create_app(settings: Settings | None = None, service: LinkService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if service is None:
        db = Database(settings.database_path)
        db.migrate()
        service = LinkService(LinkRepository(db), settings)
    limiter = TokenBucketLimiter(settings.rate_limit_per_minute)

    app = FastAPI(title="URL Shortener", version=__version__)
    app.state.settings = settings
    app.state.service = service

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "request_id": getattr(request.state, "request_id", None)},
        )

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if settings.api_key is None:
            return
        if x_api_key is None or not hmac.compare_digest(x_api_key, settings.api_key):
            raise _HTTPError(401, "missing or invalid API key")

    def rate_limited(request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        allowed, retry_after = limiter.allow(client)
        if not allowed:
            raise _HTTPError(429, "rate limit exceeded", {"Retry-After": str(int(retry_after) + 1)})

    @app.exception_handler(_HTTPError)
    async def http_error_handler(request: Request, exc: _HTTPError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": getattr(request.state, "request_id", None)},
            headers=exc.headers,
        )

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    def readyz() -> JSONResponse:
        try:
            service.repo.db.ping()
        except Exception:  # pragma: no cover - exercised only on a broken DB
            logger.exception("readiness check failed")
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(content={"status": "ready"})

    @app.post(
        "/api/v1/links",
        status_code=status.HTTP_201_CREATED,
        response_model=LinkResponse,
        responses=ERROR_RESPONSES,
        tags=["links"],
        dependencies=[Depends(require_api_key), Depends(rate_limited)],
    )
    def create_link(body: CreateLinkRequest, response: Response) -> LinkResponse:
        link = service.create_link(body.url, body.custom_alias, body.expires_at)
        response.headers["Location"] = f"/api/v1/links/{link.code}"
        return _to_response(link, settings)

    @app.get(
        "/api/v1/links/{code}",
        response_model=LinkResponse,
        responses=ERROR_RESPONSES,
        tags=["links"],
    )
    def get_link(code: str) -> LinkResponse:
        return _to_response(service.get_link(code), settings)

    @app.get(
        "/api/v1/links/{code}/stats",
        response_model=StatsResponse,
        responses=ERROR_RESPONSES,
        tags=["analytics"],
    )
    def get_stats(code: str) -> StatsResponse:
        link, stats = service.get_stats(code)
        return StatsResponse(
            code=link.code,
            total_clicks=stats.total_clicks,
            clicks_by_day=[DayCount(day=d, clicks=n) for d, n in stats.clicks_by_day],
            top_referrers=[ReferrerCount(referrer=r, clicks=n) for r, n in stats.top_referrers],
        )

    @app.delete(
        "/api/v1/links/{code}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=ERROR_RESPONSES,
        tags=["links"],
        dependencies=[Depends(require_api_key)],
    )
    def delete_link(code: str) -> Response:
        service.delete_link(code)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Registered last so it never shadows the API/ops routes above.
    @app.get("/{code}", responses=ERROR_RESPONSES, tags=["redirect"])
    def redirect(code: str, request: Request) -> RedirectResponse:
        link = service.resolve(
            code,
            referrer=request.headers.get("referer"),
            user_agent=request.headers.get("user-agent"),
        )
        # 307 (not 301) so browsers do not cache the hop and every click is counted.
        return RedirectResponse(
            link.target_url,
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Cache-Control": "private, max-age=0"},
        )

    return app

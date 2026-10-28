"""HTTP filters, ported as pure-ASGI middleware (common/web/ApiKeyFilter.java plus the
415 branch Spring's dispatcher raises for a non-JSON request body).

ApiKeyMiddleware: when settings.security.api_key is non-blank, every /api/** request
must carry it in X-API-Key (constant-time compare, hmac.compare_digest ==
MessageDigest.isEqual on UTF-8 bytes). Skipped: non-/api/ paths, OPTIONS, and
/api/health (the Python port's unauthenticated liveness probe — the Java service
exposes health via /actuator/health, which is outside /api/** and therefore never
filtered). Failures answer 401 with the ApiError JSON shape, exactly like the filter.

UnsupportedMediaTypeMiddleware: HttpMediaTypeNotSupportedException parity — a POST /
PUT / PATCH under /api/ whose Content-Type is present and not application/json (or
application/*+json) is rejected with 415 before routing. Registered INSIDE the API-key
middleware, so a request that fails both checks gets 401, as in Java (ApiKeyFilter
runs before the DispatcherServlet).
"""
from __future__ import annotations

import hmac

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import Settings
from .errors import api_error_body

HEADER = "X-API-Key"
HEALTH_PATH = "/api/health"
_BODY_METHODS = {"POST", "PUT", "PATCH"}


class ApiKeyMiddleware:
    """Port of common.web.ApiKeyFilter (shouldNotFilter + doFilterInternal)."""

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._skipped(scope):
            await self.app(scope, receive, send)
            return
        supplied = None
        for name, value in scope.get("headers") or []:
            if name.lower() == b"x-api-key":
                supplied = value.decode("latin-1")
                break
        expected = self.settings.security.api_key
        if (supplied is not None
                and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=401,
            content=api_error_body(401, "UNAUTHORIZED", "Missing or invalid API key",
                                   scope.get("path", "")))
        await response(scope, receive, send)

    def _skipped(self, scope: Scope) -> bool:
        """ApiKeyFilter.shouldNotFilter, plus the /api/health exemption."""
        if not self.settings.security.api_key_enabled:
            return True
        path = scope.get("path", "")
        if not path.startswith("/api/"):
            return True
        if scope.get("method", "").upper() == "OPTIONS":
            return True
        return path == HEALTH_PATH


class UnsupportedMediaTypeMiddleware:
    """415 for JSON-body endpoints called with a non-JSON Content-Type."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http"
                and scope.get("method", "").upper() in _BODY_METHODS
                and scope.get("path", "").startswith("/api/")):
            content_type = None
            for name, value in scope.get("headers") or []:
                if name.lower() == b"content-type":
                    content_type = value.decode("latin-1")
                    break
            if content_type is not None and not _is_json(content_type):
                response = JSONResponse(
                    status_code=415,
                    content=api_error_body(
                        415, "UNSUPPORTED_MEDIA_TYPE",
                        "Content-Type '%s' is not supported" % content_type,
                        scope.get("path", "")))
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _is_json(content_type: str) -> bool:
    """application/json or any application/*+json (parameters ignored)."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    maintype, _, subtype = media_type.partition("/")
    return maintype == "application" and (subtype == "json" or subtype.endswith("+json"))

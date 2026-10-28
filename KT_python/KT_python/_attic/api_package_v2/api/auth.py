"""HTTP filters, ported as pure-ASGI middleware (common/web/CorrelationIdFilter.java
and common/web/ApiKeyFilter.java, plus the 415 branch Spring's dispatcher raises for
a non-JSON request body).

CorrelationIdMiddleware: every response carries X-Request-Id — the client's id when
it matches the safe alphabet [A-Za-z0-9._-]{1,64} (the ids end up in logs, so the
restriction guards against log injection), else a random UUID. Registered outermost,
so even the API-key filter's 401s carry it (Java: CorrelationIdFilter is
HIGHEST_PRECEDENCE, ApiKeyFilter is HIGHEST_PRECEDENCE + 10).

ApiKeyMiddleware: when settings.security.api_key is non-blank, every /api/** request
must carry it in X-API-Key (constant-time compare; hmac.compare_digest ==
MessageDigest.isEqual on UTF-8 bytes). This authenticates the client installation,
not an individual learner. Skipped: non-/api/ paths, OPTIONS (CORS preflight), and
/api/health (the unauthenticated liveness probe; the Java service's health lives at
/actuator/health, outside /api/**, and is therefore never filtered either). Failures
answer 401 in the ApiError JSON shape, exactly like the filter.

JsonContentTypeMiddleware: HttpMediaTypeNotSupportedException parity — a POST / PUT /
PATCH under /api/ with a body whose Content-Type is not application/json (or
application/*+json) is rejected with 415 before routing. Registered INSIDE the
API-key middleware, so a request failing both checks gets 401, as in Java (the filter
runs before the DispatcherServlet).
"""
from __future__ import annotations

import hmac
import re
import uuid

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import Settings
from .errors import api_error_body

API_KEY_HEADER = "X-API-Key"
REQUEST_ID_HEADER = "X-Request-Id"
HEALTH_PATH = "/api/health"
_WRITE_METHODS = ("POST", "PUT", "PATCH")
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class CorrelationIdMiddleware:
    """Port of common.web.CorrelationIdFilter (X-Request-Id on every response)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = _header(scope, b"x-request-id")
        request_id = (supplied
                      if supplied is not None and _SAFE_REQUEST_ID.fullmatch(supplied)
                      else str(uuid.uuid4()))

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_request_id)


class ApiKeyMiddleware:
    """Port of common.web.ApiKeyFilter (X-API-Key on /api/** when configured)."""

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    def _skipped(self, scope: Scope) -> bool:
        if not self.settings.security.api_key_enabled:
            return True
        path = scope.get("path", "")
        if not path.startswith("/api/"):
            return True
        if scope.get("method", "").upper() == "OPTIONS":
            return True
        return path == HEALTH_PATH

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._skipped(scope):
            await self.app(scope, receive, send)
            return
        supplied = _header(scope, b"x-api-key")
        expected = self.settings.security.api_key
        if supplied is not None and hmac.compare_digest(
                supplied.encode("utf-8"), expected.encode("utf-8")):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            api_error_body(401, "UNAUTHORIZED", "Missing or invalid API key",
                           scope.get("path", "")),
            status_code=401)
        await response(scope, receive, send)


class JsonContentTypeMiddleware:
    """The @RequestBody content-type contract: a bodied write to /api/** with a
    non-JSON Content-Type is a 415, before routing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http"
                and scope.get("method", "").upper() in _WRITE_METHODS
                and scope.get("path", "").startswith("/api/")):
            content_type = _header(scope, b"content-type")
            has_body = _header(scope, b"content-length") not in (None, "", "0")
            if content_type is None:
                if has_body:
                    await self._reject("", scope, receive, send)
                    return
            else:
                base = content_type.split(";")[0].strip().lower()
                if base != "application/json" and not base.endswith("+json"):
                    await self._reject(base, scope, receive, send)
                    return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(media_type: str, scope: Scope, receive: Receive,
                      send: Send) -> None:
        response = JSONResponse(
            api_error_body(415, "UNSUPPORTED_MEDIA_TYPE",
                           "Content type '%s' not supported" % media_type,
                           scope.get("path", "")),
            status_code=415)
        await response(scope, receive, send)

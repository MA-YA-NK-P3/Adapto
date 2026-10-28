"""ApiError body and exception handlers — port of common/error/ApiError.java and
common/error/GlobalExceptionHandler.java.

Every API error returns the same document (master plan §43):

    {"timestamp": "2026-09-30T15:04:05.123Z", "status": 400, "error": "VALIDATION_ERROR",
     "message": "Invalid request body", "path": "/api/events", "details": [...]}

Mapping (docs/api-flow.md §3):
  * domain ValidationError        -> 400 VALIDATION_ERROR (service message)
  * pydantic body violations      -> 400 VALIDATION_ERROR "Invalid request body" + details
  * broken JSON / bad enum, UUID or Instant in the body
                                  -> 400 MALFORMED_REQUEST (HttpMessageNotReadableException)
  * query/path param violations   -> 400 VALIDATION_ERROR "Invalid request parameters"
                                     (a pure type mismatch -> "Invalid value for parameter 'x'")
  * NotFoundError                 -> 404 NOT_FOUND; unknown route -> 404 "No endpoint at this path"
  * wrong method on a known path  -> 405 METHOD_NOT_ALLOWED
  * ConflictError / IntegrityError-> 409 CONFLICT
  * BusinessRuleError             -> 422 BUSINESS_RULE_VIOLATION
  * anything else                 -> 500 INTERNAL_ERROR "Unexpected internal error"
(401 UNAUTHORIZED and 415 UNSUPPORTED_MEDIA_TYPE are produced earlier, in auth.py.)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import KnowledgeTracerError
from .schemas import iso_instant

logger = logging.getLogger(__name__)

ERROR_NAMES = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHORIZED",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "BUSINESS_RULE_VIOLATION",
    500: "INTERNAL_ERROR",
}

MALFORMED_BODY = "Request body is malformed or contains invalid values"

# Pydantic error types that mean the body could not be parsed into the DTO at all
# (Jackson would have thrown HttpMessageNotReadableException); everything else under
# "body" is a constraint violation (MethodArgumentNotValidException).
_PARSE_TYPES = frozenset({
    "json_invalid", "enum", "uuid_parsing", "uuid_type", "uuid_version",
    "datetime_parsing", "datetime_type", "date_parsing", "time_parsing",
    "int_parsing", "float_parsing", "bool_parsing", "decimal_parsing",
    "int_type", "float_type", "bool_type", "list_type", "dict_type",
    "model_attributes_type", "bytes_type", "tuple_type", "set_type",
})


def api_error_body(status: int, error: str, message: str, path: str,
                   details: Optional[List[str]] = None) -> dict:
    """The ApiError record as a JSON-able dict (details is always an array)."""
    return {
        "timestamp": iso_instant(datetime.now(timezone.utc)),
        "status": status,
        "error": error,
        "message": message,
        "path": path,
        "details": list(details or []),
    }


def api_error_response(status: int, error: str, message: str, request: Request,
                       details: Optional[List[str]] = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content=api_error_body(status, error, message,
                                               request.url.path, details))


def _field_name(loc) -> str:
    """'body.answers.0.question' -> 'answers[0].question' (Java's field path style)."""
    parts = list(loc or ())
    if parts and parts[0] in ("body", "query", "path", "header", "cookie"):
        parts = parts[1:]
    if not parts:
        return "body"
    out = str(parts[0])
    for part in parts[1:]:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


async def knowledge_tracer_error(request: Request, exc: KnowledgeTracerError) -> JSONResponse:
    """Domain errors carry their HTTP status (errors.py); the error name follows it."""
    status = getattr(exc, "http_status", 500) or 500
    return api_error_response(status, ERROR_NAMES.get(status, "INTERNAL_ERROR"),
                              str(exc), request)


async def request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    body_errors = [e for e in errors if (e.get("loc") or ())[0:1] == ("body",)]
    param_errors = [e for e in errors if (e.get("loc") or ())[0:1] in
                    (("query",), ("path",), ("header",))]

    if param_errors and not body_errors:
        # MethodArgumentTypeMismatchException: a value that cannot become the declared type.
        if all(e.get("type") in _PARSE_TYPES for e in param_errors):
            return api_error_response(
                400, "VALIDATION_ERROR",
                "Invalid value for parameter '%s'" % _field_name(param_errors[0].get("loc")),
                request)
        # ConstraintViolationException / HandlerMethodValidationException (@Min on a param).
        details = ["%s: %s" % (_field_name(e.get("loc")), e.get("msg")) for e in param_errors]
        return api_error_response(400, "VALIDATION_ERROR", "Invalid request parameters",
                                  request, details)

    # A missing body or unparseable JSON/enum/UUID/Instant is MALFORMED_REQUEST.
    if any(e.get("type") == "json_invalid"
           or (e.get("type") == "missing" and tuple(e.get("loc") or ()) == ("body",))
           or e.get("type") in _PARSE_TYPES for e in body_errors):
        return api_error_response(400, "MALFORMED_REQUEST", MALFORMED_BODY, request)
    details = ["%s: %s" % (_field_name(e.get("loc")), e.get("msg"))
               for e in (body_errors or errors)]
    return api_error_response(400, "VALIDATION_ERROR", "Invalid request body",
                              request, details)


async def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
    """DataIntegrityViolationException: a concurrent write won a unique-constraint race."""
    cause = getattr(exc, "orig", None)
    logger.warning("Integrity violation on %s %s: %s", request.method, request.url.path,
                   type(cause).__name__ if cause is not None else type(exc).__name__)
    return api_error_response(409, "CONFLICT",
                              "Request conflicts with concurrently written data; retry",
                              request)


async def http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Routing-level errors: NoResourceFoundException / HttpRequestMethodNotSupportedException."""
    if exc.status_code == 404:
        return api_error_response(404, "NOT_FOUND", "No endpoint at this path", request)
    if exc.status_code == 405:
        return api_error_response(405, "METHOD_NOT_ALLOWED",
                                  "Request method '%s' is not supported" % request.method,
                                  request)
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return api_error_response(exc.status_code,
                              ERROR_NAMES.get(exc.status_code, "INTERNAL_ERROR"),
                              message, request)


async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error on %s %s", request.method, request.url.path,
                 exc_info=exc)
    return api_error_response(500, "INTERNAL_ERROR", "Unexpected internal error", request)


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(KnowledgeTracerError, knowledge_tracer_error)
    app.add_exception_handler(RequestValidationError, request_validation_error)
    app.add_exception_handler(IntegrityError, integrity_error)
    app.add_exception_handler(StarletteHTTPException, http_exception)
    app.add_exception_handler(Exception, unexpected_error)

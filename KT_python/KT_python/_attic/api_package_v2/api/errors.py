"""ApiError body + exception handlers — port of common/error/ApiError.java and
common/error/GlobalExceptionHandler.java (@RestControllerAdvice).

Every error response is the ApiError record:

    {timestamp, status, error, message, path, details[]}

timestamp renders like Instant.now() (ISO-8601 UTC, 'Z' suffix); details is a list of
"field: message" strings (empty when there are no field errors).

The Java handler's 400 split is preserved:
  * JSON that cannot be parsed/bound (broken syntax, bad enum/UUID/Instant, wrong JSON
    type, missing body)        -> 400 MALFORMED_REQUEST  (HttpMessageNotReadableException)
  * constraint violations on a well-formed body -> 400 VALIDATION_ERROR "Invalid request body"
    (MethodArgumentNotValidException, with per-field details)
  * bad query/path parameters  -> 400 VALIDATION_ERROR "Invalid request parameters"
    (ConstraintViolationException) or "Invalid value for parameter 'x'"
    (MethodArgumentTypeMismatchException)
Domain errors carry their status from knowledge_tracer.errors (400/404/409/422).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import KnowledgeTracerError
from ..services import iso_instant

logger = logging.getLogger(__name__)

#: ApiError.error names per status (the Java ErrorCode enum values).
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


def api_error_body(status: int, error: str, message: str, path: str,
                   details: Optional[List[str]] = None) -> Dict[str, Any]:
    """ApiError.of(...): every error response has exactly this shape."""
    from datetime import datetime, timezone
    return {
        "timestamp": iso_instant(datetime.now(timezone.utc)),
        "status": status,
        "error": error,
        "message": message,
        "path": path,
        "details": list(details) if details else [],
    }


def api_error_response(status: int, error: str, message: str, path: str,
                       details: Optional[List[str]] = None,
                       headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    return JSONResponse(api_error_body(status, error, message, path, details),
                        status_code=status, headers=headers)


# --------------------------------------------------------------------------
# Validation-error classification (the GlobalExceptionHandler 400 split)
# --------------------------------------------------------------------------

#: pydantic error types whose Java counterpart is HttpMessageNotReadableException
#: (the JSON could not be bound to the DTO at all) -> MALFORMED_REQUEST.
MALFORMED_TYPES = frozenset({
    "json_invalid", "model_type", "list_type", "dict_type",
    "enum", "uuid_parsing", "int_parsing", "float_parsing", "bool_parsing",
    "int_from_float", "datetime_parsing", "datetime_from_date_parsing",
    "date_parsing", "time_parsing",
})


def field_path(loc) -> str:
    """('answers', 0, 'question') -> 'answers[0].question' (Spring's field path)."""
    parts: List[str] = []
    for part in loc:
        if isinstance(part, int):
            if parts:
                parts[-1] += "[%d]" % part
        else:
            parts.append(str(part))
    return ".".join(parts)


def validation_error_response(request: Request,
                              exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    path = request.url.path
    locations = [e.get("loc", ()) for e in errors]
    in_body = not locations or not locations[0] or locations[0][0] == "body"
    if in_body:
        # A missing body entirely is Java's "Required request body is missing"
        # (HttpMessageNotReadableException), as is any binding failure.
        malformed = any(
            e.get("type") in MALFORMED_TYPES
            or (e.get("type") == "missing" and tuple(e.get("loc", ())) == ("body",))
            for e in errors)
        if malformed:
            return api_error_response(
                400, "MALFORMED_REQUEST",
                "Request body is malformed or contains invalid values", path)
        details = [field_path(tuple(e.get("loc", ()))[1:]) + ": " + e.get("msg", "")
                   for e in errors]
        return api_error_response(400, "VALIDATION_ERROR", "Invalid request body",
                                  path, details)
    # Query/path parameters (GlobalExceptionHandler.typeMismatch / constraintViolation).
    if errors and all(e.get("type") in MALFORMED_TYPES for e in errors):
        name = str(locations[0][-1])
        return api_error_response(400, "VALIDATION_ERROR",
                                  "Invalid value for parameter '%s'" % name, path)
    details = [field_path(loc[1:]) + ": " + e.get("msg", "")
               for e, loc in zip(errors, locations)]
    return api_error_response(400, "VALIDATION_ERROR", "Invalid request parameters",
                              path, details)


def register_exception_handlers(app: FastAPI) -> None:
    """The @RestControllerAdvice methods, in the Java handler's precedence."""

    @app.exception_handler(KnowledgeTracerError)
    async def knowledge_tracer_error(request: Request, exc: KnowledgeTracerError):
        status = exc.http_status
        return api_error_response(status, ERROR_NAMES.get(status, "INTERNAL_ERROR"),
                                  str(exc), request.url.path)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        return validation_error_response(request, exc)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError):
        # DataIntegrityViolationException — a unique constraint lost a race.
        logger.warning("Integrity violation on %s %s: %s",
                       request.method, request.url.path, exc)
        return api_error_response(
            409, "CONFLICT",
            "Request conflicts with concurrently written data; retry",
            request.url.path)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            # NoResourceFoundException — no route matched.
            body = api_error_body(404, "NOT_FOUND", "No endpoint at this path",
                                  request.url.path)
        elif exc.status_code == 405:
            # HttpRequestMethodNotSupportedException's message.
            body = api_error_body(405, "METHOD_NOT_ALLOWED",
                                  "Request method '%s' is not supported"
                                  % request.method, request.url.path)
        else:
            body = api_error_body(exc.status_code,
                                  ERROR_NAMES.get(exc.status_code, "ERROR"),
                                  str(exc.detail), request.url.path)
        return JSONResponse(body, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("Unhandled error on %s %s", request.method, request.url.path,
                     exc_info=exc)
        return api_error_response(500, "INTERNAL_ERROR", "Unexpected internal error",
                                  request.url.path)

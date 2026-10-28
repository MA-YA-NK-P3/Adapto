"""Application exceptions, port of common.exception.* (see GlobalExceptionHandler.java).

The API layer maps these to HTTP status codes:
400 ValidationError, 404 NotFoundError, 409 ConflictError, 422 BusinessRuleError.
"""
from __future__ import annotations


class KnowledgeTracerError(Exception):
    """Base class for all application errors."""
    http_status = 500


class ValidationError(KnowledgeTracerError):
    """Malformed request (missing field, bad enum, out-of-range value)."""
    http_status = 400


class NotFoundError(KnowledgeTracerError):
    """Referenced learner / problem / concept / recommendation does not exist."""
    http_status = 404


class ConflictError(KnowledgeTracerError):
    """Idempotency or uniqueness conflict, e.g. an eventId reused for another learner."""
    http_status = 409


class BusinessRuleError(KnowledgeTracerError):
    """A domain rule rejected the request (unknown problemId, prerequisite cycle, ...)."""
    http_status = 422

"""FastAPI HTTP layer for the Knowledge Tracer Python port.

Ports the Java api/ tree (the seven Spring controllers, docs/api-flow.md §5) plus the
cross-cutting web concerns (common/web/CorrelationIdFilter, common/web/ApiKeyFilter,
config/WebConfig, common/error/GlobalExceptionHandler). `create_app` builds the whole
app; see app.py for the factory contract.
"""
from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]

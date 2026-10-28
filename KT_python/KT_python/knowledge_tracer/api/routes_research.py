"""Research export endpoints — port of research/ResearchExportController.java
(docs/api-flow.md §5.17-5.19; docs/research.md, docs/privacy.md).

  GET /api/research/events     all events as kt-events-v1 JSON lines (events.jsonl)
  GET /api/research/decisions  rewarded Jev decisions as kt-decisions-v1 (decisions.jsonl)
  GET /api/research/catalog    the catalog in the data/seed/catalog_1000.json schema

Every export is gated by require_enabled() BEFORE it runs, exactly like the Java
controller: export disabled -> 404, blank/default pseudonym salt -> 422. The @Min(1)
limit validation runs before the guards (FastAPI validates query parameters first,
like Spring's @Validated).

The Java side streams via StreamingResponseBody; the replica materializes through a
bounded buffer — the wire result (status, headers, body) is identical.
"""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, Query, Response

from .app import get_services

router = APIRouter()


def _ndjson(payload: bytes, filename: str) -> Response:
    return Response(
        content=payload, status_code=200, media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename})


@router.get("/api/research/events")
def research_events(services=Depends(get_services)):
    services.research.require_enabled()
    out = io.BytesIO()
    services.research.write_events(out)
    return _ndjson(out.getvalue(), "events.jsonl")


@router.get("/api/research/decisions")
def research_decisions(limit: int = Query(10000, ge=1),
                       services=Depends(get_services)):
    services.research.require_enabled()
    out = io.BytesIO()
    services.research.write_decisions(out, limit)
    return _ndjson(out.getvalue(), "decisions.jsonl")


@router.get("/api/research/catalog")
def research_catalog(services=Depends(get_services)):
    services.research.require_enabled()
    return services.research.catalog()

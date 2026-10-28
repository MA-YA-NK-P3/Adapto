"""/api/research — port of research.ResearchExportController.

Every export is gated by ResearchExportService.require_enabled() BEFORE anything is
read: export disabled -> 404, blank/default pseudonym salt -> 422. The NDJSON writers
take a binary stream; the responses buffer through io.BytesIO (explicitly supported
by research.py) and answer with Content-Type: application/x-ndjson and the same
Content-Disposition attachment filenames as Java.
"""
from __future__ import annotations

import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from .app import Services, get_services

router = APIRouter()

NDJSON = "application/x-ndjson"


def _ndjson_response(buf: io.BytesIO, filename: str) -> Response:
    return Response(
        content=buf.getvalue(),
        media_type=NDJSON,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/research/events")
def events(services: Services = Depends(get_services)) -> Response:
    services.research.require_enabled()
    buf = io.BytesIO()
    services.research.write_events(buf)
    return _ndjson_response(buf, "events.jsonl")


@router.get("/api/research/catalog")
def catalog(services: Services = Depends(get_services)) -> dict:
    services.research.require_enabled()
    return services.research.catalog()


@router.get("/api/research/decisions")
def decisions(limit: int = Query(10000, ge=1),
              services: Services = Depends(get_services)) -> Response:
    # @Min(1) on the query parameter is enforced before the export guards, as in Java.
    services.research.require_enabled()
    buf = io.BytesIO()
    services.research.write_decisions(buf, limit)
    return _ndjson_response(buf, "decisions.jsonl")

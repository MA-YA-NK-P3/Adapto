"""POST /api/events — port of event.api.EventController.

201 on first delivery, 200 on idempotent replay (same eventId); duplicates never
re-update learner state. The event, the state transition and the reward commit as one
transaction (db.transaction is the @Transactional equivalent).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..db import transaction
from ..ingestion import InteractionEventRequest, parse_timestamp
from .app import Services, get_services
from .schemas import InteractionEventIn, iso_instant

router = APIRouter()


@router.post("/api/events")
def ingest(body: InteractionEventIn,
           services: Services = Depends(get_services)) -> JSONResponse:
    request = InteractionEventRequest(
        user_id=body.user_id,
        problem_id=body.problem_id,
        timestamp=parse_timestamp(body.timestamp),
        attempt_number=body.attempt_number,
        result=body.result,
        event_id=body.event_id,
        time_spent_seconds=body.time_spent_seconds,
        hint_used=body.hint_used,
        solution_viewed=body.solution_viewed,
        confidence=body.confidence,
        perceived_difficulty=body.perceived_difficulty,
        blocker=body.blocker,
        session_id=body.session_id)
    with transaction(services.session):
        outcome = services.ingestion.ingest(request)
    e = outcome.event
    return JSONResponse(
        status_code=200 if outcome.duplicate else 201,
        content={
            "id": str(e.id),
            "eventId": str(e.event_id),
            "userId": e.learner.external_ref,
            "problemId": e.problem.problem_key,
            "timestamp": iso_instant(e.occurred_at),
            "result": e.result.name,
            "duplicate": outcome.duplicate,
        })

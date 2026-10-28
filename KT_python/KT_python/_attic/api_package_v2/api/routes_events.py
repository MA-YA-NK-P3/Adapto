"""POST /api/events — port of event/api/EventController.java.

Ingest one raw learning event. 201 on first delivery, 200 on idempotent replay (same
eventId) — duplicates never re-update learner state. Reusing an eventId for another
learner or problem is a 409, checked BEFORE any learner is created. The response is
the full InteractionEventResponse record (the stored event plus the duplicate flag).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..domain import InteractionEvent
from ..ingestion import InteractionEventRequest
from ..services import iso_instant
from .app import get_services
from .schemas import InteractionEventRequestIn

router = APIRouter()


def event_response(event: InteractionEvent, duplicate: bool) -> dict:
    """Port of event.api.dto.InteractionEventResponse (event schema v1 + duplicate)."""
    return {
        "id": str(event.id),
        "eventId": str(event.event_id),
        "userId": event.learner.external_ref,
        "problemId": event.problem.problem_key,
        "timestamp": iso_instant(event.occurred_at),
        "attemptNumber": event.attempt_number,
        "result": event.result.name,
        "timeSpentSeconds": event.time_spent_seconds,
        "hintUsed": event.hint_used,
        "solutionViewed": event.solution_viewed,
        "confidence": event.confidence,
        "perceivedDifficulty": event.perceived_difficulty,
        "blocker": event.blocker,
        "sessionId": event.session_id,
        "duplicate": duplicate,
    }


@router.post("/api/events")
def post_event(body: InteractionEventRequestIn, services=Depends(get_services)):
    outcome = services.ingestion.ingest(InteractionEventRequest(
        user_id=body.user_id, problem_id=body.problem_id,
        timestamp=body.timestamp, attempt_number=body.attempt_number,
        result=body.result, event_id=body.event_id,
        time_spent_seconds=body.time_spent_seconds, hint_used=body.hint_used,
        solution_viewed=body.solution_viewed, confidence=body.confidence,
        perceived_difficulty=body.perceived_difficulty, blocker=body.blocker,
        session_id=body.session_id))
    return JSONResponse(event_response(outcome.event, outcome.duplicate),
                        status_code=200 if outcome.duplicate else 201)

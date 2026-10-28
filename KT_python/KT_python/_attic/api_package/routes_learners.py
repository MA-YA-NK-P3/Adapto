"""/api/learners — port of learner.api.LearnerController (and the thin
learner.LearnerService, which the replica inlines here: it is a repository wrapper).

POST /api/learners                     idempotent create (201 new / 200 existing)
GET  /api/learners/{userId}/state      estimated state (read-write: inserts the
                                       cold-start learner_states row on first read)
GET  /api/learners/{userId}/next-action  the recommendation pipeline (writes one
                                       recommendations audit row per call)
GET  /api/learners/{userId}/recommendations  audit trail, newest first
GET  /api/learners/{userId}/history    raw events, newest first
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import transaction
from ..domain import InteractionEvent, Learner
from ..errors import NotFoundError
from ..recommendation import round2
from .app import Services, get_services
from .schemas import CreateLearnerIn, iso_instant

router = APIRouter()


def _get_learner(session: Session, user_id: str) -> Learner:
    """LearnerService.getByExternalRef: 404, never auto-provisioned here."""
    learner = session.scalar(select(Learner).where(Learner.external_ref == user_id))
    if learner is None:
        raise NotFoundError("Learner not found: " + user_id)
    return learner


@router.post("/api/learners")
def create(body: CreateLearnerIn,
           services: Services = Depends(get_services)) -> JSONResponse:
    session = services.session
    with transaction(session):
        learner = session.scalar(
            select(Learner).where(Learner.external_ref == body.external_ref))
        created = learner is None
        if created:
            learner = Learner(external_ref=body.external_ref)
            session.add(learner)
            session.flush()
    return JSONResponse(
        status_code=201 if created else 200,
        content={"externalRef": learner.external_ref,
                 "createdAt": iso_instant(learner.created_at),
                 "created": created})


@router.get("/api/learners/{user_id}/state")
def state(user_id: str, services: Services = Depends(get_services)) -> dict:
    session = services.session
    with transaction(session):
        learner = _get_learner(session, user_id)
        current = services.state.get_or_create_state(learner)
        concepts = {}
        for m in sorted(services.state.masteries_of(learner),
                        key=lambda m: m.concept.code):
            concepts[m.concept.code] = {
                "mastery": round2(m.mastery),
                "uncertainty": round2(m.uncertainty()),
                "evidenceCount": m.evidence_count,
            }
        return {
            "userId": user_id,
            "ability": round2(current.ability),
            "uncertainty": round2(current.uncertainty),
            "calibrated": current.calibrated,
            "totalEvents": current.total_events,
            "concepts": concepts,
        }


@router.get("/api/learners/{user_id}/next-action")
def next_action(user_id: str, services: Services = Depends(get_services)) -> dict:
    with transaction(services.session):
        response = services.recommendation.next_action(user_id)
    return response.to_dict()


@router.get("/api/learners/{user_id}/recommendations")
def recommendations(user_id: str, limit: int = Query(20, ge=1),
                    services: Services = Depends(get_services)) -> list:
    entries = services.recommendation.history(user_id, limit)
    return [entry.to_dict() for entry in entries]


@router.get("/api/learners/{user_id}/history")
def history(user_id: str, limit: int = Query(50, ge=1),
            services: Services = Depends(get_services)) -> list:
    session = services.session
    learner = _get_learner(session, user_id)
    events = session.scalars(
        select(InteractionEvent)
        .where(InteractionEvent.learner_id == learner.id)
        .order_by(InteractionEvent.occurred_at.desc())
        .limit(min(limit, 200)))
    return [{
        "eventId": str(e.event_id),
        "problemId": e.problem.problem_key,
        "timestamp": iso_instant(e.occurred_at),
        "attemptNumber": e.attempt_number,
        "result": e.result.name,
        "timeSpentSeconds": e.time_spent_seconds,
        "hintUsed": e.hint_used,
        "solutionViewed": e.solution_viewed,
        "confidence": e.confidence,
        "perceivedDifficulty": e.perceived_difficulty,
        "blocker": e.blocker,
    } for e in events]

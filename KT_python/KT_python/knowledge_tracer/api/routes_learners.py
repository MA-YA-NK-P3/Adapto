"""Learner endpoints — port of learner/api/LearnerController.java
(docs/api-flow.md §5.2-5.6).

  POST /api/learners                          register (idempotent: 201 created / 200 existing)
  GET  /api/learners/{userId}/state           current ESTIMATED state (never a permanent label)
  GET  /api/learners/{userId}/next-action     run the full pipeline; one audit row per call
  GET  /api/learners/{userId}/recommendations past recommendations, newest first
  GET  /api/learners/{userId}/history         stored events, newest first (observations)

Unknown learners are 404 on every read endpoint — reads never auto-provision.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import iso_instant
from .app import get_services
from .schemas import CreateLearnerRequestIn

router = APIRouter()


@router.post("/api/learners")
def create_learner(body: CreateLearnerRequestIn, services=Depends(get_services)):
    result = services.learners.create_if_absent(body.external_ref)
    payload = {"externalRef": result.learner.external_ref,
               "createdAt": iso_instant(result.learner.created_at),
               "created": result.created}
    return JSONResponse(payload, status_code=201 if result.created else 200)


@router.get("/api/learners/{user_id}/state")
def learner_state(user_id: str, services=Depends(get_services)):
    return services.learners.state_view(user_id).to_dict()


@router.get("/api/learners/{user_id}/next-action")
def next_action(user_id: str, services=Depends(get_services)):
    return services.recommendation.next_action(user_id).to_dict()


@router.get("/api/learners/{user_id}/recommendations")
def learner_recommendations(user_id: str, limit: int = Query(20, ge=1),
                            services=Depends(get_services)):
    return [h.to_dict() for h in services.recommendation.history(user_id, limit)]


@router.get("/api/learners/{user_id}/history")
def learner_history(user_id: str, limit: int = Query(50, ge=1),
                    services=Depends(get_services)):
    return [h.to_dict() for h in services.learners.history(user_id, limit)]

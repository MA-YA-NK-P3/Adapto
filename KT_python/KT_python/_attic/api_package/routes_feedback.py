"""POST /api/feedback — port of feedback.api.FeedbackController.

201 with {"feedbackId": <uuid>, "status": "RECORDED"}. Unlike events, feedback does
NOT auto-provision learners: an unknown userId is 404 (the service raises a
NotFoundError subclass, mapped by the global handler).

knowledge_tracer.feedback was built in parallel with this module, so the service is
resolved through the get_feedback_service dependency (tests override it with a fake)
and the submission dataclasses are imported lazily at request time; if the module is
ever absent, a duck-typed stand-in with the same fields is used instead.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import transaction
from .app import Services, get_services, get_session, get_settings
from .schemas import FeedbackIn

router = APIRouter()


def get_feedback_service(session: Session = Depends(get_session),
                         settings: Settings = Depends(get_settings)):
    """The real FeedbackService, imported lazily (the module landed in parallel).

    Tests override this dependency with a fake (app.dependency_overrides).
    """
    from ..feedback import FeedbackService  # noqa: PLC0415 — lazy on purpose
    return FeedbackService(session, settings)


def _build_submission(body: FeedbackIn):
    """feedback.FeedbackSubmission(user_id, problem_id, recommendation_id, answers,
    free_text) — the documented contract. Falls back to a duck-typed stand-in should
    knowledge_tracer.feedback be absent."""
    try:
        from ..feedback import Answer, FeedbackSubmission  # noqa: PLC0415
        return FeedbackSubmission(
            user_id=body.user_id,
            problem_id=body.problem_id,
            recommendation_id=body.recommendation_id,
            answers=[Answer(question=a.question, answer=a.answer) for a in body.answers],
            free_text=body.free_text)
    except ImportError:
        return SimpleNamespace(
            user_id=body.user_id,
            problem_id=body.problem_id,
            recommendation_id=body.recommendation_id,
            answers=[SimpleNamespace(question=a.question, answer=a.answer)
                     for a in body.answers],
            free_text=body.free_text)


@router.post("/api/feedback")
def submit(body: FeedbackIn,
           services: Services = Depends(get_services),
           feedback_service=Depends(get_feedback_service)) -> JSONResponse:
    # services.session is the same request-scoped session the service was built with.
    with transaction(services.session):
        feedback_id: uuid.UUID = feedback_service.submit(_build_submission(body))
    return JSONResponse(status_code=201,
                        content={"feedbackId": str(feedback_id), "status": "RECORDED"})

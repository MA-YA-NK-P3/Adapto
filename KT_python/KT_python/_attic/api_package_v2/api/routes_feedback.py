"""POST /api/feedback — port of feedback/api/FeedbackController.java
(docs/api-flow.md §5.14).

Stores one learner self-report; 201 with {"feedbackId", "status": "RECORDED"}.
Unlike events, feedback does NOT auto-provision learners: an unknown userId is a 404
(knowledge_tracer.errors.NotFoundError from the service).

The service is injected through the get_feedback_service dependency so tests can
override it (app.dependency_overrides) with a fake while the real module is built in
parallel; the default wires knowledge_tracer.feedback.FeedbackService, whose contract
is FeedbackService(session, settings).submit(FeedbackSubmission) -> uuid.UUID.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..feedback import Answer, FeedbackService, FeedbackSubmission
from .app import get_session
from .schemas import FeedbackRequestIn

router = APIRouter()


def get_feedback_service(request: Request,
                         session: Session = Depends(get_session)) -> FeedbackService:
    """The real FeedbackService, wired per request; tests override this dependency."""
    return FeedbackService(session, request.app.state.settings)


@router.post("/api/feedback", status_code=201)
def submit_feedback(body: FeedbackRequestIn,
                    service: FeedbackService = Depends(get_feedback_service)):
    feedback_id = service.submit(FeedbackSubmission(
        user_id=body.user_id, problem_id=body.problem_id,
        recommendation_id=body.recommendation_id,
        answers=[Answer(question=a.question, answer=a.answer) for a in body.answers],
        free_text=body.free_text))
    return {"feedbackId": str(feedback_id), "status": "RECORDED"}

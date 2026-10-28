"""Feedback subsystem — port of feedback.FeedbackService, feedback.FeedbackRepository
and the feedback.api.dto.FeedbackRequest / FeedbackResponse records (the Feedback
entity itself lives in domain.py, like every JPA entity).

Learner-provided feedback is a self-report, write-only for now: no model or export
reads it. Answers are stored as a JSON array of {"question", "answer"} to keep the
question set flexible without schema churn (the next-action response decides which
questions to ask — see recommendation.feedback_questions).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .domain import Feedback, Learner, Problem, Recommendation
from .errors import NotFoundError, ValidationError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Request DTO (feedback.api.dto.FeedbackRequest and its nested Answer record)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    """Port of FeedbackRequest.Answer."""
    question: str
    answer: str


@dataclass(frozen=True)
class FeedbackSubmission:
    """Port of FeedbackRequest (userId -> user_id, freeText -> free_text).

    problem_id is the public problem key (e.g. "LC21"), recommendation_id the audit
    row this feedback refers to; both are optional.
    """
    user_id: str
    problem_id: Optional[str]
    recommendation_id: Optional[uuid.UUID]
    answers: List[Answer]
    free_text: Optional[str]


def validate_submission(request: FeedbackSubmission) -> None:
    """The Bean Validation rules of the Java DTO, enforced at the service boundary
    (same pattern as ingestion.validate_request)."""
    if not request.user_id or not request.user_id.strip() or len(request.user_id) > 64:
        raise ValidationError("userId is required and must be at most 64 characters")
    if request.problem_id is not None and len(request.problem_id) > 32:
        raise ValidationError("problemId must be at most 32 characters")
    if not request.answers:
        raise ValidationError("answers must not be empty")
    for answer in request.answers:
        if not answer.question or not answer.question.strip() or len(answer.question) > 512:
            raise ValidationError(
                "question is required and must be at most 512 characters")
        if not answer.answer or not answer.answer.strip() or len(answer.answer) > 1024:
            raise ValidationError(
                "answer is required and must be at most 1024 characters")
    if request.free_text is not None and len(request.free_text) > 2048:
        raise ValidationError("freeText must be at most 2048 characters")


# --------------------------------------------------------------------------
# FeedbackService
# --------------------------------------------------------------------------

class FeedbackService:
    """Port of feedback.FeedbackService.

    The Java constructor injects the feedback/learner/problem/recommendation
    repositories and an ObjectMapper; the replica collapses the repositories into the
    SQLAlchemy session and uses the json module, as the other foundation services do.
    """

    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def submit(self, request: FeedbackSubmission) -> uuid.UUID:
        """Persist one feedback row and return its id (@Transactional in Java: the
        caller's transaction commits). Unknown references are 404, exactly like the
        Java service — feedback never auto-provisions."""
        validate_submission(request)

        learner = self.session.scalar(
            select(Learner).where(Learner.external_ref == request.user_id))
        if learner is None:
            raise NotFoundError("Learner not found: " + request.user_id)

        feedback = Feedback(learner_id=learner.id)
        if request.problem_id is not None:
            problem = self.session.scalar(
                select(Problem).where(
                    (Problem.problem_key == request.problem_id) | (Problem.slug == request.problem_id)
                )
            )
            if problem is None:
                raise NotFoundError("Problem not found: " + request.problem_id)
            feedback.problem_id = problem.id
        if request.recommendation_id is not None:
            recommendation = self.session.get(Recommendation, request.recommendation_id)
            if recommendation is None:
                raise NotFoundError(
                    "Recommendation not found: " + str(request.recommendation_id))
            feedback.recommendation_id = recommendation.id
        feedback.answers_json = self._serialize_answers(request)
        feedback.free_text = request.free_text

        self.session.add(feedback)
        self.session.flush()
        logger.info("Feedback received learner=%s id=%s", learner.external_ref, feedback.id)
        return feedback.id

    @staticmethod
    def _serialize_answers(request: FeedbackSubmission) -> str:
        """Jackson's writeValueAsString(answers): a JSON array of {question, answer}.
        Cannot fail for validated string fields, so the Java BusinessRuleException
        guard has no Python counterpart."""
        return json.dumps([{"question": a.question, "answer": a.answer}
                           for a in request.answers])

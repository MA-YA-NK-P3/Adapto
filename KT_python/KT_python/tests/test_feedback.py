"""Feedback service tests — port of feedback.FeedbackServiceTest (submit paths),
on the mini catalog. The Java controller tests map to the API agent's layer; here
the service boundary is tested directly, like the other foundation services.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from knowledge_tracer.domain import (ActionType, Feedback, Learner, Problem,
                                     Recommendation)
from knowledge_tracer.errors import NotFoundError, ValidationError
from knowledge_tracer.feedback import (Answer, FeedbackService,
                                       FeedbackSubmission)


@pytest.fixture()
def feedback_service(session, settings):
    return FeedbackService(session, settings)


def _learner(session, user="U1"):
    learner = Learner(external_ref=user)
    session.add(learner)
    session.flush()
    return learner


def _submission(user="U1", **kwargs):
    defaults = dict(
        problem_id=None,
        recommendation_id=None,
        answers=[Answer("What prevented you from solving the problem?",
                        "Could not handle duplicates")],
        free_text=None,
    )
    defaults.update(kwargs)
    return FeedbackSubmission(user_id=user, **defaults)


@pytest.mark.usefixtures("mini_catalog")
class TestFeedbackSubmit:
    """Port of the FeedbackService.submit scenarios."""

    def test_submit_for_known_learner_persists_row_and_returns_its_id(
            self, session, feedback_service):
        learner = _learner(session)

        feedback_id = feedback_service.submit(_submission())

        assert isinstance(feedback_id, uuid.UUID)
        row = session.get(Feedback, feedback_id)
        assert row is not None
        assert row.learner_id == learner.id
        assert row.created_at is not None

    def test_unknown_learner_is_rejected(self, feedback_service):
        """Java: LearnerRepository.findByExternalRef -> ResourceNotFoundException."""
        with pytest.raises(NotFoundError, match="Learner not found: GHOST"):
            feedback_service.submit(_submission(user="GHOST"))

    def test_answers_round_trip_as_json(self, session, feedback_service):
        _learner(session)
        answers = [Answer("Did you understand the prerequisite?", "Mostly"),
                   Answer("What prevented you from solving the problem?", "Time")]

        feedback_id = feedback_service.submit(_submission(answers=answers))

        row = session.get(Feedback, feedback_id)
        assert json.loads(row.answers_json) == [
            {"question": "Did you understand the prerequisite?", "answer": "Mostly"},
            {"question": "What prevented you from solving the problem?",
             "answer": "Time"},
        ]

    def test_optional_links_and_free_text_are_nullable(self, session, feedback_service):
        _learner(session)

        feedback_id = feedback_service.submit(_submission())

        row = session.get(Feedback, feedback_id)
        assert row.problem_id is None
        assert row.recommendation_id is None
        assert row.free_text is None
        assert row.problem is None
        assert row.recommendation is None

    def test_problem_and_recommendation_links_resolve(self, session, feedback_service):
        learner = _learner(session)
        problem = session.scalar(select(Problem).where(Problem.problem_key == "LC_E1"))
        rec = Recommendation(learner_id=learner.id,
                             action=ActionType.CALIBRATION_PROBLEM,
                             problem_id=problem.id, difficulty=5.0, reason="manual",
                             confidence=0.8, decided_by="JEV", candidates_json="[]")
        session.add(rec)
        session.flush()

        feedback_id = feedback_service.submit(_submission(
            problem_id="LC_E1", recommendation_id=rec.id, free_text="too hard"))

        row = session.get(Feedback, feedback_id)
        assert row.problem_id == problem.id
        assert row.recommendation_id == rec.id
        assert row.free_text == "too hard"
        # relationships load like the other entities' (lazy="joined")
        assert row.learner.external_ref == "U1"
        assert row.problem.problem_key == "LC_E1"
        assert row.recommendation.id == rec.id

    def test_unknown_problem_is_rejected(self, session, feedback_service):
        _learner(session)
        with pytest.raises(NotFoundError, match="Problem not found: LC_GHOST"):
            feedback_service.submit(_submission(problem_id="LC_GHOST"))

    def test_unknown_recommendation_is_rejected(self, session, feedback_service):
        _learner(session)
        ghost = uuid.uuid4()
        with pytest.raises(NotFoundError,
                           match="Recommendation not found: " + str(ghost)):
            feedback_service.submit(_submission(recommendation_id=ghost))

    def test_multiple_submissions_for_one_learner_all_persist(
            self, session, feedback_service):
        learner = _learner(session)

        ids = [feedback_service.submit(_submission(free_text="note %d" % i))
               for i in range(3)]

        assert len(set(ids)) == 3
        rows = list(session.scalars(
            select(Feedback).where(Feedback.learner_id == learner.id)))
        assert len(rows) == 3
        assert {r.free_text for r in rows} == {"note 0", "note 1", "note 2"}


class TestFeedbackValidation:
    """The Bean Validation rules of FeedbackRequest, enforced at the service boundary."""

    def test_answers_must_not_be_empty(self, feedback_service):
        with pytest.raises(ValidationError, match="answers must not be empty"):
            feedback_service.submit(_submission(answers=[]))

    def test_user_id_is_required(self, feedback_service):
        with pytest.raises(ValidationError, match="userId is required"):
            feedback_service.submit(_submission(user="  "))

    def test_answer_fields_are_required(self, feedback_service):
        with pytest.raises(ValidationError, match="question is required"):
            feedback_service.submit(_submission(answers=[Answer("", "A")]))
        with pytest.raises(ValidationError, match="answer is required"):
            feedback_service.submit(_submission(answers=[Answer("Q", "")]))

    def test_free_text_size_limit(self, feedback_service):
        with pytest.raises(ValidationError, match="freeText"):
            feedback_service.submit(_submission(free_text="x" * 2049))

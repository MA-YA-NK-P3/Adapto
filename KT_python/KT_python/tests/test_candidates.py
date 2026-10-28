"""Candidate generation and success estimation tests (docs/recommendation.md — the
candidate rule table; master plan §17).

Port of the candidate-related scenarios of LearningLoopIT (calibration items for cold
start, gap-directed remediation) plus focused unit tests of the seven candidate rules,
and of SuccessEstimator (IRT / IRT+DKT blending). The Java suite has no dedicated
CandidateGenerationServiceTest; these tests pin the Java behaviour rule by rule on the
mini catalog, driving events through EventIngestionService exactly like the Java
integration tests drive them through the HTTP API.

The DKT predictor is a duck-typed fake: success.py never imports knowledge_tracer.dkt.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from knowledge_tracer.candidates import (STRONG_TAG_WEIGHT, CandidateAction,
                                         CandidateGenerationService)
from knowledge_tracer.domain import (ActionType, Learner, LearnerState, Problem)
from knowledge_tracer.irt import information, sigmoid
from knowledge_tracer.knowledge_gap import GapReport, GapType, KnowledgeGapService
from knowledge_tracer.knowledge_graph import KnowledgeGraphService
from knowledge_tracer.success import SuccessEstimator
from tests.conftest import event_request


class FakeDktSnapshot:
    """Duck-typed dkt.DktSnapshot stand-in."""
    def __init__(self, predictions=None, mastery_map=None):
        self.predictions = predictions or {}
        self.mastery_map = mastery_map or {}

    def predict_success(self, problem):
        return self.predictions.get(problem.problem_key)

    def mastery(self):
        return dict(self.mastery_map)


class FakeDktPredictor:
    """Duck-typed dkt.DktPredictor stand-in (records snapshot() calls)."""
    def __init__(self, snapshots=None, status="ENABLED (fake DKT)"):
        self.snapshots = snapshots or {}
        self._status = status
        self.requested = []

    def snapshot(self, learner_id):
        self.requested.append(learner_id)
        return self.snapshots.get(learner_id)

    def status(self):
        return self._status


class FakeEstimate:
    """Hand-controlled Estimate stand-in for candidate-scoring tests."""
    def __init__(self, predictions):
        self.predictions = predictions

    def irt(self, problem):
        return self.predictions.get(problem.problem_key, 0.0)

    def predict(self, problem):
        return self.predictions.get(problem.problem_key, 0.0)

    def dkt(self):
        return None

    def source(self):
        return "IRT"


@pytest.fixture()
def candidate_service(session, settings, services):
    return CandidateGenerationService(session, settings, services.localization)


def _learner(session, user="U1"):
    return session.scalar(select(Learner).where(Learner.external_ref == user))


def _generate(session, settings, services, candidate_service, user="U1",
              predictor=None):
    """The Java RecommendationService pipeline up to candidate generation."""
    learner = _learner(session, user)
    state = services.state.get_or_create_state(learner)
    gap = KnowledgeGapService(session, settings, KnowledgeGraphService(session)).detect(state)
    estimate = SuccessEstimator(session, settings,
                                predictor or FakeDktPredictor()).for_learner(learner, state)
    return gap, estimate, candidate_service.generate(learner, state, gap, estimate)


def _by_action(candidates, action):
    return [c for c in candidates if c.action is action]


class TestCandidateAction:
    """Port of the CandidateAction record contract."""

    def test_predicted_success_defaults_to_none(self):
        c = CandidateAction(ActionType.FEEDBACK_REQUEST, None, None, None, "ask", 0.1)
        assert c.predicted_success is None

    def test_record_equality(self):
        a = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_E1", None, 3.0, "r", 0.9, 0.7)
        b = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_E1", None, 3.0, "r", 0.9, 0.7)
        assert a == b


@pytest.mark.usefixtures("mini_catalog")
class TestSuccessEstimator:
    """Port of SuccessEstimator's blending rules (docs/recommendation.md)."""

    def _problem(self, session, key):
        return session.scalar(select(Problem).where(Problem.problem_key == key))

    def _state(self, session, services, events=0):
        learner = Learner(external_ref="U1")
        session.add(learner)
        session.flush()
        for i in range(events):
            services.ingestion.ingest(
                event_request(problem="LC_E1", result="ACCEPTED", attempt=i + 1,
                              minutes_ago=10 - i))
        return learner, services.state.get_or_create_state(learner)

    def test_pure_irt_when_dkt_disabled(self, session, settings, services):
        learner, state = self._state(session, services)
        predictor = FakeDktPredictor()
        estimate = SuccessEstimator(session, settings, predictor).for_learner(learner, state)
        problem = self._problem(session, "LC_M1")
        assert estimate.dkt() is None
        assert estimate.source() == "IRT"
        assert estimate.irt(problem) == pytest.approx(sigmoid(state.ability - 5.0))
        assert estimate.predict(problem) == pytest.approx(estimate.irt(problem))
        assert predictor.requested == []  # snapshot never requested while disabled

    def test_snapshot_not_requested_below_min_history(self, session, settings, services):
        settings.dkt.enabled = True
        learner, state = self._state(session, services, events=2)  # min_history = 3
        predictor = FakeDktPredictor()
        estimate = SuccessEstimator(session, settings, predictor).for_learner(learner, state)
        assert predictor.requested == []
        assert estimate.dkt() is None
        assert estimate.source() == "IRT"

    def test_missing_snapshot_stays_pure_irt(self, session, settings, services):
        settings.dkt.enabled = True
        learner, state = self._state(session, services, events=3)
        predictor = FakeDktPredictor()  # no snapshot for this learner
        estimate = SuccessEstimator(session, settings, predictor).for_learner(learner, state)
        assert predictor.requested == [learner.id]
        assert estimate.dkt() is None
        assert estimate.source() == "IRT"

    def test_snapshot_without_problem_prediction_falls_back_to_irt_but_reports_blend(
            self, session, settings, services):
        """Java parity: source() is "IRT+DKT" whenever a snapshot exists, even when a
        given problem has no DKT prediction and predict() falls back to pure IRT."""
        settings.dkt.enabled = True
        learner, state = self._state(session, services, events=3)
        snapshot = FakeDktSnapshot(predictions={})  # nothing for LC_M1
        predictor = FakeDktPredictor({learner.id: snapshot})
        estimate = SuccessEstimator(session, settings, predictor).for_learner(learner, state)
        problem = self._problem(session, "LC_M1")
        assert estimate.dkt() is snapshot
        assert estimate.source() == "IRT+DKT"
        assert estimate.predict(problem) == pytest.approx(estimate.irt(problem))

    def test_blend_when_snapshot_predicts(self, session, settings, services):
        settings.dkt.enabled = True
        settings.dkt.blend_weight = 0.5
        learner, state = self._state(session, services, events=3)
        snapshot = FakeDktSnapshot(predictions={"LC_M1": 0.9})
        predictor = FakeDktPredictor({learner.id: snapshot})
        estimate = SuccessEstimator(session, settings, predictor).for_learner(learner, state)
        problem = self._problem(session, "LC_M1")
        assert estimate.predict(problem) == pytest.approx(
            0.5 * estimate.irt(problem) + 0.5 * 0.9)


@pytest.mark.usefixtures("mini_catalog")
class TestCandidateGeneration:
    """The seven candidate rules, pinned one by one (docs/recommendation.md table)."""

    def test_uncalibrated_learner_gets_two_calibration_candidates(
            self, session, settings, services, candidate_service):
        """Port of LearningLoopIT.newLearnerWithZeroHistoryGetsCalibrationItem."""
        session.add(Learner(external_ref="U1"))
        session.flush()
        gap, estimate, candidates = _generate(
            session, settings, services, candidate_service)
        assert gap.type is GapType.UNKNOWN

        calibration = _by_action(candidates, ActionType.CALIBRATION_PROBLEM)
        assert len(calibration) == 2
        # ability 5.0 -> nearest difficulties are LC_M1 (5.0) and LC_M4 (5.1)
        assert [c.problem_key for c in calibration] == ["LC_M1", "LC_M4"]
        first = calibration[0]
        assert first.concept_code is None
        assert first.difficulty == 5.0
        assert first.reason == ("Cold-start calibration: item near current estimate "
                                "5.00 maximizes information")
        assert first.expected_value == pytest.approx(0.5 + information(5.0, 5.0))
        # calibration stays pure IRT
        assert first.predicted_success == pytest.approx(sigmoid(5.0 - 5.0))

    def test_calibration_candidates_skip_attempted_problems(
            self, session, settings, services, candidate_service):
        services.ingestion.ingest(event_request(problem="LC_M1", result="ACCEPTED",
                                                minutes_ago=5))
        _, _, candidates = _generate(session, settings, services, candidate_service)
        calibration = _by_action(candidates, ActionType.CALIBRATION_PROBLEM)
        assert len(calibration) == 2
        assert all(c.problem_key != "LC_M1" for c in calibration)
        # the two unattempted items nearest the (updated) calibration target
        ability = services.state.get_or_create_state(_learner(session)).ability
        expected = sorted(
            (p for p in session.scalars(select(Problem))
             if not p.paid_only and p.problem_key != "LC_M1"),
            key=lambda p: abs(p.estimated_difficulty - ability))[:2]
        assert [c.problem_key for c in calibration] == [p.problem_key for p in expected]

    def test_same_level_picks_predicted_success_closest_to_target(
            self, session, settings, services, candidate_service):
        session.add(Learner(external_ref="U1"))
        session.flush()
        _, _, candidates = _generate(session, settings, services, candidate_service)
        same_level = _by_action(candidates, ActionType.SAME_LEVEL_PROBLEM)
        assert len(same_level) == 1
        # ability 5.0: LC_M1 has P = sigmoid(0) = 0.50, closest to the 0.65 target
        assert same_level[0].problem_key == "LC_M1"
        assert same_level[0].reason == (
            "Predicted success 0.50 (IRT) is closest to the target zone 0.65")
        assert same_level[0].expected_value == pytest.approx(1.0 - abs(0.5 - 0.65))
        assert same_level[0].predicted_success == pytest.approx(0.5)

    def test_concept_gap_yields_easier_problem_on_weak_concept(
            self, session, settings, services, candidate_service):
        """Port of LearningLoopIT.repeatedFailureOnLinkedListIsDetectedAsConceptGap...

        One success, then repeated failures on HASHING problems: EASIER_PROBLEM on
        HASHING, preferring the unattempted strong tag (LC_E2) over the attempted
        failed one (LC_M3).
        """
        services.ingestion.ingest(event_request(problem="LC_E1", result="ACCEPTED",
                                                minutes_ago=10))
        services.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                                minutes_ago=5))
        services.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                                attempt=2, minutes_ago=4))
        gap, estimate, candidates = _generate(session, settings, services,
                                              candidate_service)
        assert gap.type is GapType.CONCEPT_GAP

        easier = _by_action(candidates, ActionType.EASIER_PROBLEM)
        assert len(easier) == 1
        chosen = easier[0]
        assert chosen.problem_key == "LC_E2"
        assert chosen.concept_code == "HASHING"
        assert chosen.reason == "Easier HASHING problem to rebuild the weak concept"
        assert chosen.expected_value == 0.75
        problem = session.scalar(select(Problem).where(Problem.problem_key == "LC_E2"))
        assert chosen.predicted_success == pytest.approx(estimate.predict(problem))

    def test_prerequisite_gap_yields_prerequisite_review(
            self, session, settings, services, candidate_service):
        """Port of LearningLoopIT.weakPrerequisiteIsDetectedAndReviewedFirst."""
        services.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                                minutes_ago=10))
        services.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                                attempt=2, minutes_ago=9))
        services.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                                minutes_ago=8))
        gap, _, candidates = _generate(session, settings, services, candidate_service)
        assert gap.type is GapType.PREREQUISITE_GAP
        assert gap.concept_code == "DYNAMIC_PROGRAMMING"
        assert gap.prerequisite_code == "HASHING"

        review = _by_action(candidates, ActionType.PREREQUISITE_REVIEW)
        assert len(review) == 1
        assert review[0].concept_code == "HASHING"
        assert review[0].problem_key == "LC_E2"  # strong tag, unattempted, unsolved
        assert review[0].reason == (
            "Evidence suggests prerequisite HASHING is weak; strengthen it before "
            "continuing with DYNAMIC_PROGRAMMING")
        assert review[0].expected_value == 0.8

        # involves_concept() also fires the EASIER_PROBLEM rule for the concept itself
        easier = _by_action(candidates, ActionType.EASIER_PROBLEM)
        assert len(easier) == 1
        assert easier[0].concept_code == "DYNAMIC_PROGRAMMING"
        # strong tags, unattempted first, then closest below the target: LC_M2 (5.2)
        # beats LC_H2 (7.2); LC_H1 was already attempted
        assert easier[0].problem_key == "LC_M2"

    def test_harder_problem_requires_a_success_streak_of_three(
            self, session, settings, services, candidate_service):
        for i, key in enumerate(["LC_E1", "LC_E2"]):
            services.ingestion.ingest(event_request(problem=key, result="ACCEPTED",
                                                    minutes_ago=10 - i))
        _, _, candidates = _generate(session, settings, services, candidate_service)
        assert _by_action(candidates, ActionType.HARDER_PROBLEM) == []

        services.ingestion.ingest(event_request(problem="LC_E3", result="ACCEPTED",
                                                minutes_ago=1))
        _, _, candidates = _generate(session, settings, services, candidate_service)
        harder = _by_action(candidates, ActionType.HARDER_PROBLEM)
        assert len(harder) == 1
        learner = _learner(session)
        ability = services.state.get_or_create_state(learner).ability
        assert ability < harder[0].difficulty <= ability + 2.0
        attempted = {"LC_E1", "LC_E2", "LC_E3"}
        assert harder[0].problem_key not in attempted
        assert harder[0].reason == ("Recent success streak suggests readiness for a "
                                    "stretch problem")
        assert harder[0].expected_value == 0.55

    def test_retry_problem_is_the_most_recent_failure(
            self, session, settings, services, candidate_service):
        services.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                                minutes_ago=10))
        services.ingestion.ingest(event_request(problem="LC_E3", result="WRONG_ANSWER",
                                                minutes_ago=5))
        _, _, candidates = _generate(session, settings, services, candidate_service)
        retry = _by_action(candidates, ActionType.RETRY_PROBLEM)
        assert len(retry) == 1
        assert retry[0].problem_key == "LC_E3"
        assert retry[0].reason == ("Re-attempt the most recently failed problem after "
                                   "state update")
        assert retry[0].expected_value == 0.5

    def test_no_retry_without_failures(self, session, settings, services,
                                       candidate_service):
        services.ingestion.ingest(event_request(problem="LC_E1", result="ACCEPTED",
                                                minutes_ago=5))
        _, _, candidates = _generate(session, settings, services, candidate_service)
        assert _by_action(candidates, ActionType.RETRY_PROBLEM) == []

    def test_spaced_review_picks_a_solved_problem_outside_the_recent_window(
            self, session, settings, services, candidate_service):
        settings.recommendation.recent_window = 2
        for i, key in enumerate(["LC_E1", "LC_E2", "LC_E3"]):
            services.ingestion.ingest(event_request(problem=key, result="ACCEPTED",
                                                    minutes_ago=10 - i))
        _, _, candidates = _generate(session, settings, services, candidate_service)
        spaced = _by_action(candidates, ActionType.SPACED_REVIEW)
        assert len(spaced) == 1
        # recent window covers LC_E3/LC_E2; LC_E1 is the first solved problem outside it
        assert spaced[0].problem_key == "LC_E1"
        assert spaced[0].reason == "Spaced review of a previously solved problem"
        assert spaced[0].expected_value == 0.4

    def test_no_spaced_review_when_nothing_solved(self, session, settings, services,
                                                  candidate_service):
        services.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                                minutes_ago=5))
        _, _, candidates = _generate(session, settings, services, candidate_service)
        assert _by_action(candidates, ActionType.SPACED_REVIEW) == []

    def test_paid_problems_are_never_candidates(self, session, settings, services,
                                                candidate_service):
        """LC_PAID has the unique closest-to-target prediction, yet the SAME_LEVEL pick
        must be a free problem (CandidateGenerationService filters paid_only)."""
        learner = Learner(external_ref="U1")
        session.add(learner)
        session.flush()
        state = services.state.get_or_create_state(learner)
        state.calibrated = True  # skip the calibration rule to isolate the pool filter
        session.flush()
        estimate = FakeEstimate({"LC_PAID": 0.65})  # everything else scores 0.0
        candidates = candidate_service.generate(learner, state, GapReport.none(),
                                                estimate)
        assert candidates  # the SAME_LEVEL rule still fires
        assert all(c.problem_key != "LC_PAID" for c in candidates)

    def test_paid_solved_problem_is_not_a_spaced_review(self, session, settings,
                                                        services, candidate_service):
        settings.recommendation.recent_window = 1
        services.ingestion.ingest(event_request(problem="LC_PAID", result="ACCEPTED",
                                                minutes_ago=10))
        services.ingestion.ingest(event_request(problem="LC_E1", result="ACCEPTED",
                                                minutes_ago=5))
        _, _, candidates = _generate(session, settings, services, candidate_service)
        assert _by_action(candidates, ActionType.SPACED_REVIEW) == []

    def test_remediation_prefers_direct_exercise_then_unattempted(
            self, session, settings, services, candidate_service):
        """Remediation ordering (docs/recommendation.md): tag weight >= 0.7 first, then
        unattempted, then closest below the target difficulty."""
        # CONCEPT_GAP on ARRAYS via repeated failures on LC_E1 (ARRAYS 1.0).
        services.ingestion.ingest(event_request(problem="LC_E3", result="ACCEPTED",
                                                minutes_ago=20))
        services.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                                minutes_ago=10))
        services.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                                attempt=2, minutes_ago=9))
        gap, _, candidates = _generate(session, settings, services, candidate_service)
        assert gap.type is GapType.CONCEPT_GAP
        assert gap.concept_code == "ARRAYS"
        easier = _by_action(candidates, ActionType.EASIER_PROBLEM)
        assert len(easier) == 1
        # ARRAYS tags: LC_E1 (1.0, attempted), LC_E4 (0.7, unattempted),
        # LC_E2 (0.6, weak tag), LC_PAID (1.0, paid). Strong + unattempted -> LC_E4.
        assert easier[0].problem_key == "LC_E4"
        assert STRONG_TAG_WEIGHT == 0.7

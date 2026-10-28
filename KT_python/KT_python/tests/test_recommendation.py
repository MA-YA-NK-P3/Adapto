"""Recommendation pipeline and outcome-reward tests (docs/recommendation.md,
docs/jev.md, docs/api-flow.md §5.4/§5.5).

Ports of recommendation.RecommendationServiceTest (helpers),
recommendation.OutcomeRewardServiceTest (reward formula), decision.DecisionFallbackIT
(provider failure / invalid / low-confidence -> fallback), decision.BanditLoopIT
(decide -> outcome -> reward -> arm update) and the next-action parts of
pipeline.LearningLoopIT — all on the mini catalog, driving events through
EventIngestionService like the Java integration tests drive them through the HTTP API.

The DKT predictor is a duck-typed fake: recommendation.py never imports
knowledge_tracer.dkt.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from knowledge_tracer.candidates import CandidateAction, CandidateGenerationService
from knowledge_tracer.config import Settings
from knowledge_tracer.decision import (BanditDecisionLayer, BanditStore,
                                       DecisionLayerRouter, DecisionOutcome,
                                       DeterministicDecisionLayer, FallbackPolicy,
                                       HttpDecisionLayer, PolicyValidator)
from knowledge_tracer.domain import (ActionType, BanditArm, InteractionEvent,
                                     InteractionResult, Learner, Problem,
                                     Recommendation)
from knowledge_tracer.errors import NotFoundError
from knowledge_tracer.ingestion import EventIngestionService
from knowledge_tracer.knowledge_gap import KnowledgeGapService
from knowledge_tracer.knowledge_graph import KnowledgeGraphService
from knowledge_tracer.recommendation import (OutcomeRewardService,
                                             RecommendationService, round2,
                                             to_camel_case)
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
    """Duck-typed dkt.DktPredictor stand-in."""
    def __init__(self, snapshots=None, status="ENABLED (fake DKT)"):
        self.snapshots = snapshots or {}
        self._status = status

    def snapshot(self, learner_id):
        return self.snapshots.get(learner_id)

    def status(self):
        return self._status


class StubDecisionLayer:
    """The @MockitoBean DecisionLayer of DecisionFallbackIT."""
    def __init__(self, fn):
        self.fn = fn

    def name(self):
        return "stub"

    def decide(self, context):
        return self.fn(context)


def build_stack(session, settings, services, decision_layer=None,
                dkt_predictor=None):
    """Wire the full recommendation stack like the (parallel) container will."""
    predictor = dkt_predictor or FakeDktPredictor()
    graph = KnowledgeGraphService(session)
    gap_service = KnowledgeGapService(session, settings, graph)
    estimator = SuccessEstimator(session, settings, predictor)
    candidate_service = CandidateGenerationService(session, settings,
                                                   services.localization)
    deterministic = DeterministicDecisionLayer()
    store = BanditStore(session, settings)
    bandit = BanditDecisionLayer(settings, store, deterministic)
    http = HttpDecisionLayer(settings)
    router = DecisionLayerRouter(settings, deterministic, bandit, http)
    validator = PolicyValidator(session)
    fallback = FallbackPolicy(session, validator)
    reward_service = OutcomeRewardService(session, settings, store)
    # Ingestion wired with the REAL reward sink (conftest's services use a recorder).
    ingestion = EventIngestionService(session, settings, services.state, reward_service)
    service = RecommendationService(
        session, settings, services.state, gap_service, candidate_service,
        decision_layer if decision_layer is not None else router,
        validator, fallback, estimator, predictor)
    return SimpleNamespace(service=service, ingestion=ingestion, store=store,
                           router=router, validator=validator, fallback=fallback,
                           reward_service=reward_service)


@pytest.fixture()
def stack(session, settings, services):
    return build_stack(session, settings, services)


def _learner(session, user="U1"):
    return session.scalar(select(Learner).where(Learner.external_ref == user))


def _audit_rows(session, user="U1"):
    learner = _learner(session, user)
    return list(session.scalars(
        select(Recommendation)
        .where(Recommendation.learner_id == learner.id)
        .order_by(Recommendation.created_at.desc())))


class TestHelpers:
    """Port of RecommendationServiceTest."""

    def test_concept_codes_map_to_camel_case_knowledge_keys(self):
        assert to_camel_case("ARRAYS") == "arrays"
        assert to_camel_case("LINKED_LIST") == "linkedList"
        assert to_camel_case("DYNAMIC_PROGRAMMING") == "dynamicProgramming"

    def test_rounds_to_two_decimals(self):
        assert round2(4.3456) == 4.35
        assert round2(0.0) == 0.0


class TestRewardFormula:
    """Port of OutcomeRewardServiceTest (reward = w*quality + (1-w)*growth)."""

    def _event(self, result, hint=False, solution=False):
        return InteractionEvent(
            event_id=uuid.uuid4(), learner_id=uuid.uuid4(), problem_id=uuid.uuid4(),
            occurred_at=datetime.now(timezone.utc), attempt_number=1,
            result=result, hint_used=hint, solution_viewed=solution)

    def test_default_reward_is_ability_growth_only(self, settings):
        cfg = settings.jev.reward
        assert cfg.quality_weight == 0.0
        accepted = self._event(InteractionResult.ACCEPTED)
        assert OutcomeRewardService.reward(accepted, 5.0, 5.25, cfg) == pytest.approx(0.75)
        assert OutcomeRewardService.reward(accepted, 5.0, 5.0, cfg) == pytest.approx(0.5)
        wrong = self._event(InteractionResult.WRONG_ANSWER)
        assert OutcomeRewardService.reward(wrong, 5.0, 4.0, cfg) == 0.0

    def test_reward_combines_outcome_quality_and_ability_growth(self, settings):
        cfg = settings.jev.reward
        cfg.quality_weight = 0.7
        # unassisted success, ability +0.25 -> growth 0.75
        assert OutcomeRewardService.reward(
            self._event(InteractionResult.ACCEPTED), 5.0, 5.25, cfg
        ) == pytest.approx(0.7 * 1.0 + 0.3 * 0.75)
        assert OutcomeRewardService.reward(
            self._event(InteractionResult.ACCEPTED, hint=True), 5.0, 5.0, cfg
        ) == pytest.approx(0.7 * 0.6 + 0.3 * 0.5)
        assert OutcomeRewardService.reward(
            self._event(InteractionResult.ACCEPTED, hint=True, solution=True),
            5.0, 5.0, cfg) == pytest.approx(0.7 * 0.3 + 0.3 * 0.5)
        # failure with a large ability drop is clamped to growth 0 -> reward 0
        assert OutcomeRewardService.reward(
            self._event(InteractionResult.WRONG_ANSWER), 5.0, 3.0, cfg) == 0.0

    def test_reward_stays_in_unit_interval(self, settings):
        cfg = settings.jev.reward
        cfg.quality_weight = 0.7
        for result in InteractionResult:
            for delta in (-3, -0.2, 0, 0.2, 3):
                reward = OutcomeRewardService.reward(
                    self._event(result), 5.0, 5.0 + delta, cfg)
                assert 0.0 <= reward <= 1.0


@pytest.mark.usefixtures("mini_catalog")
class TestNextAction:
    """End-to-end next_action on the mini catalog (port of the LearningLoopIT and
    DecisionFallbackIT next-action scenarios)."""

    def test_unknown_learner_is_404(self, stack):
        """Java: LearnerService.getByExternalRef throws ResourceNotFoundException —
        next-action never auto-provisions."""
        with pytest.raises(NotFoundError, match="Learner not found: GHOST"):
            stack.service.next_action("GHOST")

    def test_new_learner_gets_calibration_problem(self, session, stack):
        """Port of LearningLoopIT.newLearnerWithZeroHistoryGetsCalibrationItem."""
        session.add(Learner(external_ref="U1"))
        session.flush()
        resp = stack.service.next_action("U1")

        assert resp.user_id == "U1"
        assert resp.state.ability == 5.0
        assert resp.state.uncertainty == 2.0
        assert resp.state.calibrated is False
        assert resp.state.total_events == 0
        assert resp.gap.type == "UNKNOWN"
        assert resp.recommendation.type == "CALIBRATION_PROBLEM"
        assert resp.recommendation.problem_id is not None
        assert resp.recommendation.difficulty == pytest.approx(5.0)  # LC_M1
        assert resp.recommendation.decided_by == "JEV"
        assert resp.recommendation.provider == "deterministic"
        assert resp.recommendation.fallback_used is False
        assert resp.recommendation.confidence == 0.8
        assert resp.recommendation.predicted_success == pytest.approx(0.5)
        # no gap concept -> evidence is just the candidate's reason
        assert resp.recommendation.evidence == [resp.recommendation.reason]
        assert resp.feedback.required is False
        assert resp.feedback.questions == []
        assert resp.knowledge == {}
        assert resp.dkt_knowledge == {}
        assert resp.models == {
            "irt": "ENABLED (Rasch 1PL, MAP)",
            "conceptMastery": "ENABLED (Beta-Bernoulli)",
            "prerequisiteGraph": "ENABLED",
            "adaptiveLocalization": "ENABLED",
            "dkt": "DISABLED",
            "successModel": "IRT",
            "jev": "ENABLED (deterministic)",
        }

    def test_audit_row_contents(self, session, stack):
        """One recommendations row per call, with the full decision audit."""
        session.add(Learner(external_ref="U1"))
        session.flush()
        resp = stack.service.next_action("U1")

        rows = _audit_rows(session)
        assert len(rows) == 1
        rec = rows[0]
        assert rec.id == resp.recommendation.recommendation_id
        assert rec.action is ActionType.CALIBRATION_PROBLEM
        assert rec.decided_by == "JEV"
        assert rec.fallback_used is False
        assert rec.provider == "deterministic"
        assert rec.ability_at_decision == 5.0
        assert rec.predicted_success == pytest.approx(0.5)
        assert rec.reward is None
        assert rec.rewarded_at is None
        assert rec.reward_event_id is None

        candidates = json.loads(rec.candidates_json)
        assert len(candidates) == 3  # 2 x CALIBRATION_PROBLEM + SAME_LEVEL_PROBLEM
        assert set(candidates[0].keys()) == {
            "action", "problemKey", "conceptCode", "difficulty", "reason",
            "expectedValue", "predictedSuccess"}

        context = json.loads(rec.context_json)  # stored: the chosen action has a problem
        assert len(context) == 8  # BanditFeatures.DIM
        assert context[0] == 1.0  # bias

        # the response DTO serializes to the documented camelCase shape
        body = resp.to_dict()
        assert body["recommendation"]["decidedBy"] == "JEV"
        assert body["recommendation"]["provider"] == "deterministic"
        assert body["state"]["totalEvents"] == 0

    def test_concept_gap_is_remediated(self, session, stack):
        """Port of LearningLoopIT.repeatedFailureOnLinkedListIsDetectedAsConceptGap..."""
        stack.ingestion.ingest(event_request(problem="LC_E1", result="ACCEPTED",
                                             minutes_ago=10))
        stack.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                             minutes_ago=5))
        stack.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                             attempt=2, minutes_ago=4))
        resp = stack.service.next_action("U1")

        assert resp.gap.type == "CONCEPT_GAP"
        assert resp.gap.concept == "HASHING"
        assert resp.recommendation.type == "EASIER_PROBLEM"
        assert resp.recommendation.topic == "HASHING"
        assert resp.recommendation.problem_id == "LC_E2"
        assert resp.recommendation.decided_by == "JEV"
        assert resp.recommendation.provider == "deterministic"
        assert resp.recommendation.fallback_used is False
        # the chosen action addresses the gap -> the gap evidence is cited
        assert resp.recommendation.evidence == resp.gap.evidence
        assert resp.recommendation.evidence[0].startswith(
            "HASHING mastery estimate 0.25 is below threshold 0.45")
        assert resp.feedback.required is True
        assert resp.feedback.questions == ["What prevented you from solving the "
                                           "problem?",
                                           "Which concept felt unfamiliar?"]
        assert resp.knowledge == {"arrays": 0.67, "hashing": 0.25}

    def test_prerequisite_gap_is_remediated_first(self, session, stack):
        """Port of LearningLoopIT.weakPrerequisiteIsDetectedAndReviewedFirst."""
        stack.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                             minutes_ago=10))
        stack.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                             attempt=2, minutes_ago=9))
        stack.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                             minutes_ago=8))
        resp = stack.service.next_action("U1")

        assert resp.gap.type == "PREREQUISITE_GAP"
        assert resp.gap.concept == "DYNAMIC_PROGRAMMING"
        assert resp.gap.prerequisite == "HASHING"
        assert resp.recommendation.type == "PREREQUISITE_REVIEW"
        assert resp.recommendation.topic == "HASHING"
        assert resp.recommendation.problem_id == "LC_E2"
        assert resp.feedback.required is True
        assert resp.feedback.questions == ["Did you understand the prerequisite?",
                                           "What prevented you from solving the "
                                           "problem?"]

    def test_dkt_blend_is_reported(self, session, settings, services):
        """dktKnowledge and the model report reflect the (fake) DKT snapshot."""
        settings.dkt.enabled = True
        predictor = FakeDktPredictor(status="ENABLED (LSTM kt-dkt-v1, fake)")
        stack = build_stack(session, settings, services, dkt_predictor=predictor)
        for i in range(3):
            stack.ingestion.ingest(event_request(problem="LC_E1", result="ACCEPTED",
                                                 attempt=i + 1, minutes_ago=10 - i))
        learner = _learner(session)
        predictor.snapshots[learner.id] = FakeDktSnapshot(
            mastery_map={"ARRAYS": 0.7, "LINKED_LIST": 0.3})
        resp = stack.service.next_action("U1")
        assert resp.models["dkt"] == "ENABLED (LSTM kt-dkt-v1, fake)"
        assert resp.models["successModel"] == "IRT+DKT"
        assert resp.dkt_knowledge == {"arrays": 0.7, "linkedList": 0.3}


@pytest.mark.usefixtures("mini_catalog")
class TestDecisionFallback:
    """Port of DecisionFallbackIT: Jev must never be a single point of failure (§46)."""

    def _stack_with_stub(self, session, settings, services, fn):
        return build_stack(session, settings, services,
                           decision_layer=StubDecisionLayer(fn))

    def _prepare_learner(self, stack):
        stack.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                             minutes_ago=5))

    def test_unavailable_jev_triggers_fallback(self, session, settings, services):
        def boom(context):
            raise RuntimeError("Jev timeout")

        stack = self._stack_with_stub(session, settings, services, boom)
        self._prepare_learner(stack)
        resp = stack.service.next_action("U1")
        assert resp.recommendation.fallback_used is True
        assert resp.recommendation.decided_by == "FALLBACK"
        assert resp.recommendation.provider is None
        assert resp.recommendation.problem_id is not None

    def test_action_outside_candidate_set_triggers_fallback(
            self, session, settings, services):
        def invent(context):
            return DecisionOutcome(
                CandidateAction(ActionType.HARDER_PROBLEM, "LC_GHOST", None, 7.0,
                                "invented", 1.0),
                0.99, "x")

        stack = self._stack_with_stub(session, settings, services, invent)
        self._prepare_learner(stack)
        resp = stack.service.next_action("U1")
        assert resp.recommendation.fallback_used is True
        assert resp.recommendation.decided_by == "FALLBACK"

    def test_low_confidence_triggers_fallback(self, session, settings, services):
        def unsure(context):
            return DecisionOutcome(context.candidates[0], 0.05, "unsure")

        stack = self._stack_with_stub(session, settings, services, unsure)
        self._prepare_learner(stack)
        resp = stack.service.next_action("U1")
        assert resp.recommendation.fallback_used is True

    def test_valid_high_confidence_decision_is_used(self, session, settings, services):
        def confident(context):
            return DecisionOutcome(context.candidates[0], 0.9, "ok")

        stack = self._stack_with_stub(session, settings, services, confident)
        self._prepare_learner(stack)
        resp = stack.service.next_action("U1")
        assert resp.recommendation.fallback_used is False
        assert resp.recommendation.decided_by == "JEV"
        assert resp.recommendation.provider == "stub"

    def test_jev_disabled_uses_the_fallback_policy(self, session, settings, services):
        settings.jev.enabled = False
        stack = build_stack(session, settings, services)
        self._prepare_learner(stack)
        resp = stack.service.next_action("U1")
        assert resp.recommendation.decided_by == "FALLBACK"
        assert resp.recommendation.provider is None
        assert resp.recommendation.fallback_used is True
        assert resp.models["jev"] == "DISABLED (fallback policy only)"

    def test_unknown_provider_falls_back(self, session, settings, services):
        settings.jev.provider = "gpt-magic"
        stack = build_stack(session, settings, services)
        self._prepare_learner(stack)
        resp = stack.service.next_action("U1")
        assert resp.recommendation.decided_by == "FALLBACK"
        assert resp.recommendation.provider is None

    def test_fallback_audit_row_has_null_provider(self, session, settings, services):
        settings.jev.enabled = False
        stack = build_stack(session, settings, services)
        self._prepare_learner(stack)
        stack.service.next_action("U1")
        rec = _audit_rows(session)[0]
        assert rec.decided_by == "FALLBACK"
        assert rec.provider is None
        assert rec.fallback_used is True
        assert json.loads(rec.candidates_json)  # non-empty even on fallback


@pytest.mark.usefixtures("mini_catalog")
class TestOutcomeReward:
    """Port of BanditLoopIT + the attribution rules of OutcomeRewardService."""

    def _recommend(self, stack, user="U1"):
        return stack.service.next_action(user)

    def test_outcome_on_recommended_problem_rewards_decision_and_updates_arm(
            self, session, settings, stack):
        """Port of BanditLoopIT.outcomeOnRecommendedProblemRewardsTheDecision..."""
        settings.jev.provider = "bandit"
        for key in ("LC_E1", "LC_E2", "LC_E3"):
            stack.ingestion.ingest(event_request(problem=key, result="ACCEPTED",
                                                 minutes_ago=30))
        resp = self._recommend(stack)
        assert resp.models["jev"] == "ENABLED (bandit)"
        assert resp.recommendation.decided_by == "JEV"
        assert resp.recommendation.provider == "bandit"
        assert 0.0 <= resp.recommendation.predicted_success <= 1.0
        action = resp.recommendation.type
        problem = resp.recommendation.problem_id

        arm = session.get(BanditArm, action)
        pulls_before = arm.pulls if arm is not None else 0

        stack.ingestion.ingest(event_request(problem=problem, result="ACCEPTED",
                                             minutes_ago=1))
        history = stack.service.history("U1", 20)
        assert history[0].provider == "bandit"
        reward = history[0].reward
        # first-attempt success raises ability -> growth reward > 0.5
        assert 0.5 < reward <= 1.0
        arm = session.get(BanditArm, action)
        assert arm.pulls == pulls_before + 1
        assert arm.reward_sum == pytest.approx(reward)

        # a second outcome on the same problem is not attributed twice
        stack.ingestion.ingest(event_request(problem=problem, result="ACCEPTED",
                                             attempt=2, minutes_ago=0))
        assert session.get(BanditArm, action).pulls == pulls_before + 1
        assert stack.service.history("U1", 20)[0].reward == pytest.approx(reward)

    def test_unrelated_events_do_not_produce_rewards(self, session, stack):
        """Port of BanditLoopIT.unrelatedEventsDoNotProduceRewards."""
        stack.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                             minutes_ago=5))
        resp = self._recommend(stack)
        other = "LC_M1" if resp.recommendation.problem_id != "LC_M1" else "LC_M2"
        stack.ingestion.ingest(event_request(problem=other, result="ACCEPTED",
                                             minutes_ago=1))
        assert stack.service.history("U1", 20)[0].reward is None

    def test_outcome_beyond_the_attribution_window_is_not_attributed(
            self, session, settings, stack):
        assert settings.jev.reward.attribution_window_hours == 336
        stack.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                             minutes_ago=5))
        resp = self._recommend(stack)
        rec = _audit_rows(session)[0]
        # back-date the recommendation beyond the 336 h window
        rec.created_at = datetime.now(timezone.utc) - timedelta(hours=400)
        session.flush()

        returned = stack.ingestion.ingest(
            event_request(problem=resp.recommendation.problem_id, result="ACCEPTED",
                          minutes_ago=0))
        assert returned.duplicate is False
        fresh = _audit_rows(session)[0]
        assert fresh.reward is None
        assert fresh.rewarded_at is None
        assert fresh.reward_event_id is None

    def test_reward_is_written_once(self, session, stack):
        stack.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                             minutes_ago=5))
        resp = self._recommend(stack)
        problem = resp.recommendation.problem_id
        stack.ingestion.ingest(event_request(problem=problem, result="ACCEPTED",
                                             minutes_ago=1))
        first = _audit_rows(session)[0]
        assert first.reward is not None
        rewarded_at, reward_event_id = first.rewarded_at, first.reward_event_id

        stack.ingestion.ingest(event_request(problem=problem, result="WRONG_ANSWER",
                                             attempt=2, minutes_ago=0))
        second = _audit_rows(session)[0]
        assert second.reward == pytest.approx(first.reward)
        assert second.rewarded_at == rewarded_at
        assert second.reward_event_id == reward_event_id

    def test_reward_without_stored_context_still_writes_but_skips_the_bandit(
            self, session, stack):
        """Java parity: context_json is null (e.g. FEEDBACK_REQUEST) -> the reward is
        written but no arm is updated."""
        stack.ingestion.ingest(event_request(problem="LC_E1", result="ACCEPTED",
                                             minutes_ago=5))
        learner = _learner(session)
        lc_m1 = session.scalar(select(Problem).where(Problem.problem_key == "LC_M1"))
        rec = Recommendation(
            learner_id=learner.id, action=ActionType.SAME_LEVEL_PROBLEM,
            problem_id=lc_m1.id, difficulty=5.0, reason="manual", confidence=0.9,
            expected_value=0.9, predicted_success=0.5, decided_by="JEV",
            fallback_used=False, provider="deterministic",
            candidates_json="[]", context_json=None, ability_at_decision=5.0)
        session.add(rec)
        session.flush()

        stack.ingestion.ingest(event_request(problem="LC_M1", result="ACCEPTED",
                                             minutes_ago=0))
        updated = _audit_rows(session)[0]
        assert updated.reward is not None
        assert updated.reward_event_id is not None
        assert session.get(BanditArm, "SAME_LEVEL_PROBLEM") is None


@pytest.mark.usefixtures("mini_catalog")
class TestHistory:
    """Port of the recommendations-history endpoint logic (docs/api-flow.md §5.5)."""

    def test_unknown_learner_is_404(self, stack):
        with pytest.raises(NotFoundError):
            stack.service.history("GHOST", 20)

    def test_newest_first_with_limit_clamped(self, session, stack):
        session.add(Learner(external_ref="U1"))
        session.flush()
        stack.service.next_action("U1")
        second = stack.service.next_action("U1")

        history = stack.service.history("U1", 20)
        assert len(history) == 2
        assert history[0].recommendation_id == second.recommendation.recommendation_id
        assert history[0].created_at >= history[1].created_at
        entry = history[0]
        assert entry.type == "CALIBRATION_PROBLEM"
        assert entry.problem_id is not None
        assert entry.decided_by == "JEV"
        assert entry.provider == "deterministic"
        assert entry.fallback_used is False
        assert entry.reward is None  # until an outcome on that problem is attributed
        assert entry.to_dict()["type"] == "CALIBRATION_PROBLEM"

        # limit is clamped to [1, 200] as in the Java service
        assert len(stack.service.history("U1", 0)) == 1
        assert len(stack.service.history("U1", 500)) == 2

"""Outcome -> reward loop proof (docs/jev.md §reward, docs/api-flow.md §5.4).

Not a re-implementation: this drives the EXISTING recommendation stack and asserts
the loop is closed end to end on the mini catalog:

  1. a next-action recommendation persists one audit row with NO reward;
  2. when the outcome event for the recommended problem is ingested, the
     OutcomeRewardService port computes the reward (quality/growth formula) and
     records it on exactly that audit row (reward, rewarded_at, reward_event_id);
  3. with the bandit decision layer selected, the LinUCB arm's sufficient
     statistics (A, b, pulls, reward_sum) change by exactly the rewarded rank-one
     update, and the arm's policy parameters theta = A^-1 b move.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from knowledge_tracer.candidates import CandidateGenerationService
from knowledge_tracer.decision import (BanditDecisionLayer, BanditFeatures,
                                       BanditStore, DecisionLayerRouter,
                                       DeterministicDecisionLayer, FallbackPolicy,
                                       HttpDecisionLayer, LinUCB, PolicyValidator)
from knowledge_tracer.domain import BanditArm, Learner, Recommendation
from knowledge_tracer.ingestion import EventIngestionService
from knowledge_tracer.knowledge_gap import KnowledgeGapService
from knowledge_tracer.knowledge_graph import KnowledgeGraphService
from knowledge_tracer.recommendation import (OutcomeRewardService,
                                             RecommendationService)
from knowledge_tracer.success import SuccessEstimator
from tests.conftest import event_request


@pytest.fixture()
def loop_stack(session, settings, services):
    """The full recommendation stack with the bandit provider selected and the REAL
    OutcomeRewardService as the ingestion reward sink (conftest's services fixture
    uses a RecordingRewardSink instead). DKT stays disabled, so no predictor fake
    is needed (SuccessEstimator/RecommendationService never touch it)."""
    settings.jev.provider = "bandit"
    graph = KnowledgeGraphService(session)
    gap_service = KnowledgeGapService(session, settings, graph)
    estimator = SuccessEstimator(session, settings, None)
    candidate_service = CandidateGenerationService(session, settings,
                                                   services.localization)
    deterministic = DeterministicDecisionLayer()
    store = BanditStore(session, settings)
    bandit = BanditDecisionLayer(settings, store, deterministic)
    router = DecisionLayerRouter(settings, deterministic, bandit,
                                 HttpDecisionLayer(settings))
    validator = PolicyValidator(session)
    fallback = FallbackPolicy(session, validator)
    reward_service = OutcomeRewardService(session, settings, store)
    ingestion = EventIngestionService(session, settings, services.state,
                                      reward_service)
    service = RecommendationService(session, settings, services.state, gap_service,
                                    candidate_service, router, validator, fallback,
                                    estimator, None)
    return SimpleNamespace(service=service, ingestion=ingestion, store=store,
                           reward_service=reward_service)


def _learner(session, user="U1"):
    return session.scalar(select(Learner).where(Learner.external_ref == user))


def _audit_rows(session, user="U1"):
    learner = _learner(session, user)
    return list(session.scalars(
        select(Recommendation)
        .where(Recommendation.learner_id == learner.id)
        .order_by(Recommendation.created_at.desc())))


def _seed_history(loop_stack):
    """Three accepted first attempts on easy problems: no gap, so the (guarded)
    bandit — not a remediation rule — makes the decision."""
    for key in ("LC_E1", "LC_E2", "LC_E3"):
        loop_stack.ingestion.ingest(event_request(problem=key, result="ACCEPTED",
                                                  minutes_ago=30))


@pytest.mark.usefixtures("mini_catalog")
class TestRewardLoop:
    def test_recommendation_persists_audit_row_without_reward(
            self, session, loop_stack):
        """Loop step 1: decide -> persist audit row; reward columns stay NULL until
        an outcome is attributed."""
        _seed_history(loop_stack)

        resp = loop_stack.service.next_action("U1")

        assert resp.recommendation.decided_by == "JEV"
        assert resp.recommendation.provider == "bandit"
        rows = _audit_rows(session)
        assert len(rows) == 1
        rec = rows[0]
        assert rec.id == resp.recommendation.recommendation_id
        assert rec.problem_id is not None
        assert rec.ability_at_decision is not None
        # no reward yet...
        assert rec.reward is None
        assert rec.rewarded_at is None
        assert rec.reward_event_id is None
        # ...but the bandit feature vector is already stored for the update
        x = json.loads(rec.context_json)
        assert len(x) == BanditFeatures.DIM

    def test_outcome_records_computed_reward_on_the_audit_row(
            self, session, settings, services, loop_stack):
        """Loop step 2: ingest the outcome event for the recommended problem ->
        OutcomeRewardService.on_outcome attributes it and writes the formula's
        reward onto the pending audit row."""
        _seed_history(loop_stack)
        resp = loop_stack.service.next_action("U1")
        rec = _audit_rows(session)[0]
        ability_at_decision = rec.ability_at_decision

        outcome = loop_stack.ingestion.ingest(
            event_request(problem=resp.recommendation.problem_id, result="ACCEPTED",
                          minutes_ago=1))
        assert outcome.duplicate is False

        rewarded = _audit_rows(session)[0]
        assert rewarded.id == rec.id
        assert rewarded.reward is not None
        assert rewarded.rewarded_at is not None
        assert rewarded.reward_event_id == outcome.event.event_id
        # the recorded value is exactly the ported reward formula applied to the
        # persisted event and the ability transition the ingestion pipeline observed
        ability_after = services.state.get_or_create_state(_learner(session)).ability
        expected = OutcomeRewardService.reward(outcome.event, ability_at_decision,
                                               ability_after, settings.jev.reward)
        assert rewarded.reward == pytest.approx(expected)
        assert 0.0 <= rewarded.reward <= 1.0

    def test_rewarded_outcome_updates_the_bandit_arm(self, session, settings,
                                                     loop_stack):
        """Loop step 3: the bandit's observable state (the jev_bandit_arms row of the
        chosen action) changes by exactly A += x x', b += r x with the stored context
        x and the recorded reward r — the loop is closed."""
        _seed_history(loop_stack)
        resp = loop_stack.service.next_action("U1")
        assert resp.recommendation.provider == "bandit"
        rec = _audit_rows(session)[0]
        action = rec.action.value
        x = np.asarray(json.loads(rec.context_json), dtype=float)
        assert x.shape == (BanditFeatures.DIM,)

        arm = session.get(BanditArm, action)
        if arm is None:  # no update yet: the arm is at the warm-start prior
            a_before = LinUCB.prior_a(BanditFeatures.DIM, settings.jev.bandit.lambda_)
            b_before = LinUCB.prior_b(BanditFeatures.THETA0,
                                      settings.jev.bandit.lambda_)
            pulls_before, reward_sum_before = 0, 0.0
        else:
            a_before = np.asarray(json.loads(arm.a_matrix), dtype=float)
            b_before = np.asarray(json.loads(arm.b_vector), dtype=float)
            pulls_before, reward_sum_before = arm.pulls, arm.reward_sum

        loop_stack.ingestion.ingest(
            event_request(problem=resp.recommendation.problem_id, result="ACCEPTED",
                          minutes_ago=1))
        reward = _audit_rows(session)[0].reward
        assert reward is not None

        arm = session.get(BanditArm, action)
        assert arm is not None
        a_after = np.asarray(json.loads(arm.a_matrix), dtype=float)
        b_after = np.asarray(json.loads(arm.b_vector), dtype=float)
        # the exact LinUCB rank-one update with the stored context and reward
        assert a_after == pytest.approx(a_before + np.outer(x, x))
        assert b_after == pytest.approx(b_before + reward * x)
        assert arm.pulls == pulls_before + 1
        assert arm.reward_sum == pytest.approx(reward_sum_before + reward)
        # the arm's policy parameters actually moved
        theta_before = LinUCB.theta(a_before, b_before)
        theta_after = LinUCB.theta(a_after, b_after)
        assert not np.allclose(theta_before, theta_after)
        # and the store serves the updated statistics
        assert loop_stack.store.snapshot()[action].pulls == pulls_before + 1

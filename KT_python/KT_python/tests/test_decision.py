"""Decision-layer tests (docs/jev.md; master plan §6, §46).

Ports of decision.DeterministicDecisionLayerTest, decision.PolicyValidatorTest,
decision.FallbackPolicyTest, decision.DecisionLayerRouterTest,
decision.bandit.BanditDecisionLayerTest and decision.bandit.LinUcbTest. Where the Java
tests mock ProblemRepository, the replica tests run against the real mini catalog —
the session IS the repository here.

No real network calls: urllib.request.urlopen is monkeypatched throughout.
"""
from __future__ import annotations

import json
import math
import urllib.error
import uuid

import numpy as np
import pytest
from sqlalchemy import select

from knowledge_tracer.candidates import CandidateAction
from knowledge_tracer.decision import (BanditDecisionLayer, BanditFeatures,
                                       BanditStore, DecisionContext, DecisionLayerError,
                                       DecisionLayerRouter, DecisionOutcome,
                                       DeterministicDecisionLayer, FallbackPolicy,
                                       HttpDecisionLayer, LinUCB, PolicyValidator)
from knowledge_tracer.domain import ActionType, BanditArm, Problem
from knowledge_tracer.knowledge_gap import GapReport, GapType


def make_context(calibrated=True, gap=None, candidates=(), solved=(), ability=4.0,
                 uncertainty=0.8, learner_ref="U1"):
    return DecisionContext(learner_ref, ability, uncertainty, calibrated,
                           gap if gap is not None else GapReport.none(),
                           list(candidates), set(solved))


def _concept_gap():
    return GapReport(GapType.CONCEPT_GAP, concept_code="HASHING",
                     evidence=["weak"], confidence=0.8)


# Candidate fixtures mirroring the Java DeterministicDecisionLayerTest constants.
CALIBRATION = CandidateAction(ActionType.CALIBRATION_PROBLEM, "LC_E1", None, 3.0,
                              "calibrate", 0.75)
SAME_LEVEL = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_E1", None, 3.0,
                             "same", 0.95)
EASIER = CandidateAction(ActionType.EASIER_PROBLEM, "LC_E2", "HASHING", 2.8,
                         "easier", 0.75)
PREREQ = CandidateAction(ActionType.PREREQUISITE_REVIEW, "LC_E1", "ARRAYS", 2.5,
                         "prereq", 0.8)


class TestDeterministicDecisionLayer:
    """Port of DeterministicDecisionLayerTest (rule ordering)."""

    layer = DeterministicDecisionLayer()

    def test_empty_candidate_set_yields_no_choice(self):
        out = self.layer.decide(make_context())
        assert out.chosen is None
        assert out.confidence == 0.0
        assert out.rationale == "No candidates available"

    def test_uncalibrated_learner_without_gap_gets_calibration_item(self):
        out = self.layer.decide(make_context(
            calibrated=False, gap=GapReport.unknown("new"),
            candidates=[SAME_LEVEL, CALIBRATION]))
        assert out.chosen.action is ActionType.CALIBRATION_PROBLEM
        assert out.confidence == 0.8

    def test_concept_gap_outranks_calibration(self):
        out = self.layer.decide(make_context(
            calibrated=False, gap=_concept_gap(),
            candidates=[CALIBRATION, EASIER, SAME_LEVEL]))
        assert out.chosen == EASIER
        assert out.confidence == 0.8  # the gap confidence, not 0.8 calibration confidence

    def test_prerequisite_gap_chooses_prerequisite_review(self):
        gap = GapReport(GapType.PREREQUISITE_GAP, concept_code="DYNAMIC_PROGRAMMING",
                        prerequisite_code="HASHING", evidence=["weak"], confidence=0.7)
        out = self.layer.decide(make_context(
            gap=gap, candidates=[SAME_LEVEL, EASIER, PREREQ]))
        assert out.chosen == PREREQ
        assert out.confidence == 0.7
        assert out.rationale == ("Evidence suggests prerequisite HASHING is weak for "
                                 "DYNAMIC_PROGRAMMING")

    def test_prerequisite_gap_falls_back_to_easier_problem_when_no_review_candidate(self):
        gap = GapReport(GapType.PREREQUISITE_GAP, concept_code="DYNAMIC_PROGRAMMING",
                        prerequisite_code="HASHING", evidence=["weak"], confidence=0.7)
        out = self.layer.decide(make_context(gap=gap, candidates=[SAME_LEVEL, EASIER]))
        assert out.chosen == EASIER

    def test_calibrated_learner_without_gap_gets_highest_expected_value(self):
        out = self.layer.decide(make_context(candidates=[CALIBRATION, SAME_LEVEL]))
        assert out.chosen == SAME_LEVEL
        assert out.confidence <= 0.9
        assert out.rationale == ("No actionable gap: selecting highest expected-value "
                                 "candidate")

    def test_always_chooses_from_candidate_set(self):
        all_candidates = [CALIBRATION, SAME_LEVEL, EASIER, PREREQ]
        for gap in (GapReport.none(), _concept_gap(), GapReport.unknown("x")):
            for calibrated in (True, False):
                out = self.layer.decide(make_context(
                    calibrated=calibrated, gap=gap, candidates=all_candidates))
                assert out.chosen in all_candidates


@pytest.mark.usefixtures("mini_catalog")
class TestPolicyValidator:
    """Port of PolicyValidatorTest — the five checks, against the real catalog."""

    @pytest.fixture()
    def validator(self, session):
        return PolicyValidator(session)

    def _problem_id(self, session, key):
        return session.scalar(select(Problem).where(Problem.problem_key == key)).id

    def test_accepts_valid_choice(self, session, validator):
        result = validator.validate(DecisionOutcome(SAME_LEVEL, 0.9, "ok"),
                                    make_context(candidates=[SAME_LEVEL]), 0.35)
        assert result.valid
        assert result.violations == []

    def test_rejects_null_decision(self, validator):
        assert not validator.validate(DecisionOutcome(None, 0.9, "x"),
                                      make_context(candidates=[SAME_LEVEL]), 0.35).valid
        assert not validator.validate(None, make_context(candidates=[SAME_LEVEL]),
                                      0.35).valid

    def test_rejects_action_outside_candidate_set(self, validator):
        invented = CandidateAction(ActionType.HARDER_PROBLEM, "LC_E1", None, 3.0,
                                   "?", 1.0)
        result = validator.validate(DecisionOutcome(invented, 0.9, "x"),
                                    make_context(candidates=[SAME_LEVEL]), 0.35)
        assert not result.valid
        assert any("candidate set" in v for v in result.violations)

    def test_rejects_nonexistent_problem(self, validator):
        ghost = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_GHOST", None, 3.0,
                                "?", 1.0)
        result = validator.validate(DecisionOutcome(ghost, 0.9, "x"),
                                    make_context(candidates=[ghost]), 0.35)
        assert not result.valid
        assert any("does not exist" in v for v in result.violations)

    def test_rejects_out_of_range_difficulty(self, validator):
        for bad_difficulty in (11.0, -0.5, float("nan")):
            bad = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_E1", None,
                                  bad_difficulty, "?", 1.0)
            result = validator.validate(DecisionOutcome(bad, 0.9, "x"),
                                        make_context(candidates=[bad]), 0.35)
            assert not result.valid
            assert any("[0,10]" in v for v in result.violations)

    def test_solved_problem_only_allowed_for_retry_or_spaced_review(
            self, session, validator):
        solved_id = self._problem_id(session, "LC_E1")
        harder = CandidateAction(ActionType.HARDER_PROBLEM, "LC_E1", None, 3.0, "?", 1.0)
        review = CandidateAction(ActionType.SPACED_REVIEW, "LC_E1", None, 3.0, "?", 1.0)
        retry = CandidateAction(ActionType.RETRY_PROBLEM, "LC_E1", None, 3.0, "?", 1.0)
        assert not validator.validate(
            DecisionOutcome(harder, 0.9, "x"),
            make_context(candidates=[harder], solved=[solved_id]), 0.35).valid
        assert validator.validate(
            DecisionOutcome(review, 0.9, "x"),
            make_context(candidates=[review], solved=[solved_id]), 0.35).valid
        assert validator.validate(
            DecisionOutcome(retry, 0.9, "x"),
            make_context(candidates=[retry], solved=[solved_id]), 0.35).valid

    def test_rejects_low_confidence(self, validator):
        result = validator.validate(DecisionOutcome(SAME_LEVEL, 0.2, "x"),
                                    make_context(candidates=[SAME_LEVEL]), 0.35)
        assert not result.valid
        assert any("confidence" in v for v in result.violations)

    def test_feedback_request_without_problem_is_allowed(self, validator):
        feedback = CandidateAction(ActionType.FEEDBACK_REQUEST, None, None, None,
                                   "ask", 0.5)
        assert validator.validate(DecisionOutcome(feedback, 0.9, "x"),
                                  make_context(candidates=[feedback]), 0.35).valid


@pytest.mark.usefixtures("mini_catalog")
class TestFallbackPolicy:
    """Port of FallbackPolicyTest (the FIXED descending-expected-value behaviour)."""

    @pytest.fixture()
    def fallback(self, session):
        return FallbackPolicy(session, PolicyValidator(session))

    def _problem_id(self, session, key):
        return session.scalar(select(Problem).where(Problem.problem_key == key)).id

    def test_nearest_unsolved_skips_paid_and_solved_problems(self, session, fallback):
        # ability 3.1: LC_PAID (3.1, paid) would be nearest; LC_E4 (3.1) is solved.
        solved = {self._problem_id(session, "LC_E4")}
        out = fallback.select(make_context(ability=3.1, solved=solved))
        assert out.chosen.action is ActionType.SAME_LEVEL_PROBLEM
        assert out.chosen.problem_key != "LC_PAID"
        assert out.chosen.problem_key != "LC_E4"
        assert out.confidence == 0.3
        assert out.rationale == ("Deterministic fallback: nearest-difficulty unsolved "
                                 "problem")

    def test_invalid_best_candidate_falls_through_to_next_valid_candidate(
            self, session, fallback):
        solved_id = self._problem_id(session, "LC_E1")
        best = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_E1", None, 3.0,
                               "r", 0.9)   # invalid: already solved, not retry-like
        second = CandidateAction(ActionType.HARDER_PROBLEM, "LC_M1", None, 5.0,
                                 "r", 0.6)
        ctx = make_context(ability=5.0, candidates=[best, second], solved=[solved_id])
        out = fallback.select(ctx)
        assert out.chosen == second
        assert out.confidence == 0.5
        assert out.rationale == ("Deterministic fallback: highest expected-value valid "
                                 "candidate")

    def test_descending_expected_value_order(self, session, fallback):
        lower = CandidateAction(ActionType.RETRY_PROBLEM, "LC_E1", None, 3.0, "r", 0.5)
        higher = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_M1", None, 5.0,
                                 "r", 0.9)
        out = fallback.select(make_context(candidates=[lower, higher]))
        assert out.chosen == higher

    def test_only_paid_problems_left_falls_back_to_feedback_request(
            self, session, fallback):
        solved = {p.id for p in session.scalars(select(Problem)) if not p.paid_only}
        out = fallback.select(make_context(ability=5.0, solved=solved))
        assert out.chosen.action is ActionType.FEEDBACK_REQUEST
        assert out.chosen.problem_key is None
        assert out.chosen.difficulty is None
        assert out.chosen.expected_value == 0.1
        assert out.confidence == 0.1
        assert out.rationale == "Deterministic fallback: exhausted problem pool"


@pytest.mark.usefixtures("mini_catalog")
class TestDecisionLayerRouter:
    """Port of DecisionLayerRouterTest."""

    @pytest.fixture()
    def parts(self, session, settings):
        deterministic = DeterministicDecisionLayer()
        store = BanditStore(session, settings)
        bandit = BanditDecisionLayer(settings, store, deterministic)
        http = HttpDecisionLayer(settings)
        router = DecisionLayerRouter(settings, deterministic, bandit, http)
        return router, bandit, http, settings

    def test_dispatches_by_configured_provider(self, parts):
        router, bandit, http, settings = parts
        assert router.name() == "deterministic"
        settings.jev.provider = " Bandit "
        assert router.provider() is bandit
        settings.jev.provider = "http"
        assert router.provider() is http

    def test_unknown_provider_fails_loudly_so_the_fallback_is_used(self, parts):
        router, _, _, settings = parts
        settings.jev.provider = "gpt-magic"
        with pytest.raises(DecisionLayerError, match="gpt-magic"):
            router.decide(make_context())

    def test_http_provider_refuses_insecure_salt_before_any_request(
            self, parts, monkeypatch):
        """Port of httpProviderRefusesBlankOrDefaultSaltBeforeAnyRequest: the guard
        fires before any network I/O (monkeypatched urlopen must never be called)."""
        _, _, http, settings = parts
        calls = []

        def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("no network call allowed with an insecure salt")

        monkeypatch.setattr("urllib.request.urlopen", forbidden)
        settings.jev.http.url = "http://127.0.0.1:1/unreachable"
        ctx = make_context(learner_ref="real-user-id")
        for salt in ("change-me", " ", None):
            settings.research.pseudonym_salt = salt
            with pytest.raises(DecisionLayerError, match="KT_PSEUDONYM_SALT"):
                http.decide(ctx)
        assert calls == []

    def test_http_protocol_sends_only_the_bounded_context(self, parts):
        """Port of httpProtocolSendsOnlyTheBoundedContext."""
        c = CandidateAction(ActionType.EASIER_PROBLEM, "LC_E2", "HASHING", 2.8,
                            "easier", 0.75, 0.8)
        ctx = make_context(learner_ref="real-user-id", gap=_concept_gap(),
                           candidates=[c])
        body = HttpDecisionLayer.request_body(ctx, "L-abc")
        assert body["protocol"] == "kt-jev-v1"
        assert body["learner"]["id"] == "L-abc"
        assert body["learner"]["ability"] == 4.0
        assert body["gap"]["type"] == "CONCEPT_GAP"
        assert body["gap"]["concept"] == "HASHING"
        assert "real-user-id" not in json.dumps(body)
        assert len(body["candidates"]) == 1
        assert body["candidates"][0]["index"] == 0
        assert body["candidates"][0]["problemKey"] == "LC_E2"
        assert body["candidates"][0]["predictedSuccess"] == 0.8
        assert body["rules"] == [
            "choose exactly one candidate by index",
            "prefer remediating a well-evidenced prerequisite or concept gap",
            "target predicted success near 0.65 (productive struggle)",
            "never invent problems or actions",
        ]


class FakeHttpResponse:
    """Minimal stand-in for the object urllib.request.urlopen returns."""
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.usefixtures("mini_catalog")
class TestHttpDecisionLayer:
    """kt-jev-v1 over monkeypatched urllib (no real network)."""

    SECURE_SALT = "test-secret-salt"

    @pytest.fixture()
    def http(self, settings):
        settings.research.pseudonym_salt = self.SECURE_SALT
        return HttpDecisionLayer(settings), settings

    def _context(self):
        easier = CandidateAction(ActionType.EASIER_PROBLEM, "LC_E2", "HASHING", 2.8,
                                 "easier", 0.75, 0.8)
        same = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_M1", None, 5.0,
                               "same", 0.95, 0.66)
        return make_context(learner_ref="real-user-id", gap=_concept_gap(),
                            candidates=[easier, same])

    def test_successful_decide(self, http, monkeypatch):
        layer, settings = http
        settings.jev.http.api_key = "secret-key"
        settings.jev.http.timeout_ms = 1500
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHttpResponse(json.dumps(
                {"chosenIndex": 1, "confidence": 0.7, "rationale": "pick two"}))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        out = layer.decide(self._context())
        assert out.chosen.problem_key == "LC_M1"
        assert out.confidence == 0.7
        assert out.rationale == "External Jev: pick two"

        request = captured["request"]
        assert captured["timeout"] == 1.5
        assert request.get_method() == "POST"
        # urllib.request.Request stores headers capitalized ("X-API-Key" -> "X-api-key")
        assert request.headers.get("X-api-key") == "secret-key"
        assert request.headers.get("Content-type") == "application/json"
        body = json.loads(request.data.decode("utf-8"))
        assert body["protocol"] == "kt-jev-v1"
        assert body["learner"]["id"].startswith("L-")
        assert "real-user-id" not in json.dumps(body)

    def test_no_api_key_header_when_blank(self, http, monkeypatch):
        layer, settings = http
        settings.jev.http.api_key = " "
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return FakeHttpResponse(json.dumps({"chosenIndex": 0}))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        out = layer.decide(self._context())
        assert "X-api-key" not in captured["request"].headers
        assert out.confidence == 0.5  # default when the answer omits confidence
        assert out.rationale == "External Jev: external Jev decision"

    def test_confidence_is_clamped_to_unit_interval(self, http, monkeypatch):
        layer, _ = http
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda request, timeout=None: FakeHttpResponse(
                                json.dumps({"chosenIndex": 0, "confidence": 1.7})))
        assert layer.decide(self._context()).confidence == 1.0

    @pytest.mark.parametrize("answer", [
        {"chosenIndex": 5},                       # out of range
        {"chosenIndex": -1},                      # negative
        {"confidence": 0.9},                      # missing index
        {"chosenIndex": None},                    # null index
    ])
    def test_bad_chosen_index_raises(self, http, monkeypatch, answer):
        layer, _ = http
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda request, timeout=None: FakeHttpResponse(
                                json.dumps(answer)))
        with pytest.raises(DecisionLayerError, match="no valid candidate index"):
            layer.decide(self._context())

    def test_transport_error_raises_so_the_caller_falls_back(self, http, monkeypatch):
        layer, _ = http

        def broken(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", broken)
        with pytest.raises(DecisionLayerError):
            layer.decide(self._context())

    def test_malformed_json_raises(self, http, monkeypatch):
        layer, _ = http
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda request, timeout=None: FakeHttpResponse("not json"))
        with pytest.raises(DecisionLayerError, match="malformed JSON"):
            layer.decide(self._context())


class TestLinUCB:
    """Port of LinUcbTest."""

    def test_fresh_arm_predicts_the_warm_start_expected_value(self):
        a = LinUCB.prior_a(BanditFeatures.DIM, 1.0)
        b = LinUCB.prior_b(BanditFeatures.THETA0, 1.0)
        x = [1, 0.6, 0.96, 0.1, 0.4, 1, 0, 0.83]
        s = LinUCB.score(a, b, x, 0.5)
        assert s.mean == pytest.approx(0.83)
        assert s.width == pytest.approx(math.sqrt(float(np.dot(x, x))))
        assert s.ucb == pytest.approx(s.mean + 0.5 * s.width)

    def test_updates_converge_to_the_true_linear_reward_and_shrink_exploration(self):
        rng = np.random.RandomState(1)
        truth = np.array([0.2, 0.5, -0.1, 0.0, 0.0, 0.1, 0.3, 0.2])
        a = LinUCB.prior_a(8, 1.0)
        b = LinUCB.prior_b(BanditFeatures.THETA0, 1.0)
        probe = np.array([1, 0.5, 1, 0, 0.5, 1, 0.5, 0.7])
        width_before = LinUCB.score(a, b, probe, 1.0).width
        for _ in range(4000):
            x = np.array([1, rng.rand(), rng.rand(), rng.rand() * 2 - 1,
                          rng.rand(), rng.randint(2), rng.rand(), rng.rand() * 1.5])
            LinUCB.update(a, b, x, float(np.dot(truth, x)) + rng.normal() * 0.05)
        theta = LinUCB.theta(a, b)
        for i in range(len(truth)):
            assert theta[i] == pytest.approx(truth[i], abs=0.03)
        assert LinUCB.score(a, b, probe, 1.0).width < width_before / 20

    def test_cholesky_solve_inverts_a_symmetric_positive_definite_matrix(self):
        a = np.array([[4, 2, 0.4], [2, 3, 0.5], [0.4, 0.5, 2]])
        y = np.array([1.0, 2.0, 3.0])
        z = LinUCB._solve(np.linalg.cholesky(a), y)
        for i in range(3):
            assert float(np.dot(a[i], z)) == pytest.approx(y[i])


class TestBanditFeatures:
    """Port of the BanditFeatures parts of BanditDecisionLayerTest."""

    def test_layout_constants(self):
        assert BanditFeatures.VERSION == 1
        assert BanditFeatures.NAMES == [
            "bias", "predictedSuccess", "information", "relativeDifficulty",
            "abilityUncertainty", "calibrated", "gapMatch", "expectedValue"]
        assert BanditFeatures.DIM == 8
        assert BanditFeatures.THETA0 == [0, 0, 0, 0, 0, 0, 0, 1]

    def test_features_are_bounded_and_mark_gap_matches(self):
        gap = GapReport(GapType.CONCEPT_GAP, concept_code="HASHING",
                        evidence=["weak"], confidence=0.8)
        ctx = make_context(ability=4.5, uncertainty=0.6, gap=gap, candidates=[EASIER])
        x = BanditFeatures.of(EASIER, ctx)
        assert len(x) == BanditFeatures.DIM
        assert x[6] == 0.8  # gapMatch = gap confidence for the gap concept
        assert BanditFeatures.of(SAME_LEVEL, ctx)[6] == 0.0
        for v in x:
            assert -1.0 <= v <= 1.5

    def test_missing_difficulty_and_prediction_use_neutral_values(self):
        feedback = CandidateAction(ActionType.FEEDBACK_REQUEST, None, None, None,
                                   "ask", 0.1)
        x = BanditFeatures.of(feedback, make_context(candidates=[feedback]))
        assert x[1] == 0.5   # predictedSuccess default
        assert x[2] == 1.0   # information at p = 0.5
        assert x[3] == 0.0   # relativeDifficulty default


@pytest.mark.usefixtures("mini_catalog")
class TestBanditStore:
    """BanditStore persistence: warm start, update, feature-layout reset."""

    def test_prior_is_the_warm_start(self, session, settings):
        store = BanditStore(session, settings)
        prior = store.prior()
        assert prior.pulls == 0
        assert prior.reward_sum == 0.0
        assert np.allclose(prior.a, np.eye(BanditFeatures.DIM))
        assert np.allclose(prior.b, BanditFeatures.THETA0)

    def test_update_persists_and_snapshot_reads_back(self, session, settings):
        store = BanditStore(session, settings)
        x = BanditFeatures.of(SAME_LEVEL, make_context(candidates=[SAME_LEVEL]))
        store.update("SAME_LEVEL_PROBLEM", x, 0.7)

        row = session.get(BanditArm, "SAME_LEVEL_PROBLEM")
        assert row is not None
        assert row.dim == BanditFeatures.DIM
        assert row.feature_version == BanditFeatures.VERSION
        assert row.pulls == 1
        assert row.reward_sum == pytest.approx(0.7)

        stats = BanditStore(session, settings).snapshot()["SAME_LEVEL_PROBLEM"]
        assert np.allclose(stats.a, np.eye(BanditFeatures.DIM) + np.outer(x, x))
        assert np.allclose(stats.b, np.array(BanditFeatures.THETA0) + 0.7 * np.array(x))
        assert stats.pulls == 1

    def test_incompatible_arm_is_reset_to_the_prior(self, session, settings):
        session.add(BanditArm(arm="RETRY_PROBLEM", dim=3, feature_version=0,
                              a_matrix=json.dumps(np.eye(3).tolist()),
                              b_vector=json.dumps([0.0, 0.0, 0.0]),
                              pulls=17, reward_sum=9.0))
        session.flush()
        store = BanditStore(session, settings)
        assert store.snapshot() == {}  # incompatible arms are hidden

        x = BanditFeatures.of(SAME_LEVEL, make_context(candidates=[SAME_LEVEL]))
        store.update("RETRY_PROBLEM", x, 0.5)
        row = session.get(BanditArm, "RETRY_PROBLEM")
        assert row.dim == BanditFeatures.DIM
        assert row.feature_version == BanditFeatures.VERSION
        assert row.pulls == 1          # reset, then one update
        assert row.reward_sum == pytest.approx(0.5)

    def test_update_with_wrong_feature_dim_is_ignored(self, session, settings):
        BanditStore(session, settings).update("SAME_LEVEL_PROBLEM", [1.0, 2.0], 0.9)
        assert session.get(BanditArm, "SAME_LEVEL_PROBLEM") is None

    def test_corrupt_arm_json_falls_back_to_the_prior(self, session, settings):
        session.add(BanditArm(arm="EASIER_PROBLEM", dim=BanditFeatures.DIM,
                              feature_version=BanditFeatures.VERSION,
                              a_matrix="{not json", b_vector="[]",
                              pulls=4, reward_sum=2.0))
        session.flush()
        stats = BanditStore(session, settings).snapshot()["EASIER_PROBLEM"]
        assert np.allclose(stats.a, np.eye(BanditFeatures.DIM))
        # Java parity: toStats returns prior() wholesale on corrupt JSON (pulls 0)
        assert stats.pulls == 0
        assert stats.reward_sum == 0.0


@pytest.mark.usefixtures("mini_catalog")
class TestBanditDecisionLayer:
    """Port of BanditDecisionLayerTest."""

    BANDIT_SAME = CandidateAction(ActionType.SAME_LEVEL_PROBLEM, "LC_E1", None, 4.0,
                                  "same", 0.95, 0.66)
    BANDIT_RETRY = CandidateAction(ActionType.RETRY_PROBLEM, "LC_E2", None, 5.0,
                                   "retry", 0.5, 0.45)
    BANDIT_EASIER = CandidateAction(ActionType.EASIER_PROBLEM, "LC_E3", "HASHING", 2.8,
                                    "easier", 0.75, 0.8)

    @pytest.fixture()
    def layer(self, session, settings):
        store = BanditStore(session, settings)
        return (BanditDecisionLayer(settings, store, DeterministicDecisionLayer()),
                store, settings)

    def _context(self, gap, *candidates):
        return make_context(ability=4.5, uncertainty=0.6, gap=gap,
                            candidates=list(candidates))

    def test_empty_candidate_set_yields_no_choice(self, layer):
        out = layer[0].decide(self._context(GapReport.none()))
        assert out.chosen is None
        assert out.confidence == 0.0

    def test_without_data_and_exploration_it_ranks_like_the_heuristic(self, layer):
        bandit, _, settings = layer
        settings.jev.bandit.alpha = 0.0
        out = bandit.decide(self._context(GapReport.none(),
                                          self.BANDIT_RETRY, self.BANDIT_SAME))
        assert out.chosen == self.BANDIT_SAME
        assert out.rationale.startswith("LinUCB")
        assert 0.35 <= out.confidence <= 0.95

    def test_learned_rewards_override_the_warm_start(self, layer):
        bandit, store, settings = layer
        settings.jev.bandit.alpha = 0.0
        ctx = self._context(GapReport.none(), self.BANDIT_RETRY, self.BANDIT_SAME)
        x = BanditFeatures.of(self.BANDIT_SAME, ctx)
        for _ in range(200):
            store.update("SAME_LEVEL_PROBLEM", x, 0.05)
        assert bandit.decide(ctx).chosen == self.BANDIT_RETRY

    def test_guarded_mode_keeps_gap_remediation_deterministic(self, layer):
        bandit, _, settings = layer
        gap = GapReport(GapType.CONCEPT_GAP, concept_code="HASHING",
                        evidence=["weak"], confidence=0.8)
        ctx = self._context(gap, self.BANDIT_SAME, self.BANDIT_EASIER,
                            self.BANDIT_RETRY)
        out = bandit.decide(ctx)
        assert out.chosen == self.BANDIT_EASIER
        assert out.rationale.startswith("Guarded bandit")

        settings.jev.bandit.guarded = False
        assert bandit.decide(ctx).rationale.startswith("LinUCB")

    def test_guarded_mode_does_not_apply_without_remediation_candidate(self, layer):
        bandit, _, settings = layer
        settings.jev.bandit.alpha = 0.0
        gap = GapReport(GapType.CONCEPT_GAP, concept_code="HASHING",
                        evidence=["weak"], confidence=0.8)
        out = bandit.decide(self._context(gap, self.BANDIT_SAME, self.BANDIT_RETRY))
        assert out.rationale.startswith("LinUCB")

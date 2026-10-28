"""Foundation tests: IRT invariants, mastery, localization, ingestion, seeding.

These mirror IrtModelTest, ConceptMasteryServiceTest, AdaptiveLocalizationServiceTest
and the ingestion parts of LearningLoopIT on the Java side.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from knowledge_tracer import irt
from knowledge_tracer.config import Settings
from knowledge_tracer.domain import (Concept, InteractionEvent, InteractionResult,
                                     Learner, Problem, StaticDifficulty)
from knowledge_tracer.errors import BusinessRuleError, ConflictError, ValidationError
from knowledge_tracer.irt import (ItemObservation, ResponseObservation,
                                  estimate_ability, estimate_difficulty)
from knowledge_tracer.mastery import credit_weight
from knowledge_tracer.seed import load_catalog
from tests.conftest import event_request

REAL_CATALOG = Path(__file__).resolve().parents[2] / "data" / "seed" / "catalog_1000.json"


# --- IrtModel invariants --------------------------------------------------------

class TestIrtModel:
    def test_probability_bounds_and_monotonicity(self):
        for ability in (0.0, 2.5, 5.0, 7.5, 10.0):
            for difficulty in (0.0, 5.0, 10.0):
                p = irt.probability_of_success(ability, difficulty)
                assert 0.0 < p < 1.0
        assert irt.probability_of_success(6, 5) > irt.probability_of_success(4, 5)
        assert irt.probability_of_success(5, 4) > irt.probability_of_success(5, 6)
        assert irt.probability_of_success(5, 5) == pytest.approx(0.5)

    def test_success_never_decreases_ability(self):
        responses = [ResponseObservation(5.0, True), ResponseObservation(5.0, False)]
        with_success = estimate_ability(responses + [ResponseObservation(5.0, True)], 5.0, 2.0)
        with_failure = estimate_ability(responses + [ResponseObservation(5.0, False)], 5.0, 2.0)
        assert with_success.ability > with_failure.ability

    def test_uncertainty_shrinks_with_evidence(self):
        one = estimate_ability([ResponseObservation(5.0, True)], 5.0, 2.0)
        many = estimate_ability([ResponseObservation(5.0, True)] * 20, 5.0, 2.0)
        assert many.uncertainty < one.uncertainty

    def test_all_correct_stays_finite(self):
        est = estimate_ability([ResponseObservation(0.0, True)] * 50, 5.0, 2.0)
        assert 0.0 <= est.ability <= 10.0

    def test_clamp_handles_nan_and_inf(self):
        # Non-finite input maps to the scale midpoint, as in the Java IrtModel.
        assert irt.clamp(float("nan")) == 5.0
        assert irt.clamp(float("inf")) == 5.0
        assert irt.clamp(-3.0) == 0.0
        assert irt.clamp(12.0) == 10.0

    def test_difficulty_recovers_from_population(self):
        # 100 learners at ability 5 answering a true-difficulty-7 item (25% pass).
        obs = ([ItemObservation(5.0, False)] * 75) + ([ItemObservation(5.0, True)] * 25)
        est = estimate_difficulty(obs, prior_mean=5.0, prior_sd=1.5)
        assert est.difficulty > 5.5  # pulled well above the prior
        assert est.sample_size == 100


class TestPrior:
    def test_acceptance_adjustment_direction(self, settings):
        service = irt.IrtService(None, settings)
        easy_common = service.prior(StaticDifficulty.EASY, 0.60)
        easy_tricky = service.prior(StaticDifficulty.EASY, 0.30)
        assert easy_tricky > easy_common
        assert 0.5 <= easy_tricky <= 9.5

    def test_no_acceptance_falls_back_to_label(self, settings):
        service = irt.IrtService(None, settings)
        assert service.prior(StaticDifficulty.HARD, None) == 7.0


# --- Concept mastery --------------------------------------------------------------

def _event(result="ACCEPTED", **kw):
    e = InteractionEvent(result=InteractionResult[result], attempt_number=1,
                         hint_used=kw.get("hint_used", False),
                         solution_viewed=kw.get("solution_viewed", False),
                         confidence=kw.get("confidence"),
                         time_spent_seconds=kw.get("time_spent_seconds"))
    return e


class TestMastery:
    def test_single_failure_does_not_destroy_mastery(self, session, settings, mini_catalog):
        from knowledge_tracer.mastery import ConceptMasteryService
        service = ConceptMasteryService(session, settings)
        learner = Learner(external_ref="U1")
        session.add(learner)
        session.flush()
        concept = session.scalar(select(Concept).where(Concept.code == "ARRAYS"))
        m = service.apply_event(learner, concept, _event("WRONG_ANSWER"), 1.0)
        assert m.mastery == pytest.approx(1.0 / 3.0)

    def test_credit_weights(self):
        assert credit_weight(_event("ACCEPTED")) == 1.0
        assert credit_weight(_event("ACCEPTED", hint_used=True)) == pytest.approx(0.7)
        assert credit_weight(_event("ACCEPTED", solution_viewed=True)) == pytest.approx(0.4)
        assert credit_weight(_event("ACCEPTED", confidence=1)) == pytest.approx(0.8)
        assert credit_weight(_event("ACCEPTED", time_spent_seconds=3000)) == pytest.approx(0.8)
        assert credit_weight(_event("WRONG_ANSWER", solution_viewed=True)) == pytest.approx(0.5)
        assert credit_weight(_event("WRONG_ANSWER")) == 1.0


# --- Ingestion + state ------------------------------------------------------------

@pytest.mark.usefixtures("mini_catalog")
class TestIngestion:
    def test_ingest_creates_learner_event_and_state(self, services, session):
        outcome = services.ingestion.ingest(event_request())
        session.commit()
        assert not outcome.duplicate
        state = services.state.get_or_create_state(outcome.event.learner)
        assert state.total_events == 1
        assert state.ability != 5.0  # moved from the prior
        assert len(services.reward_sink.calls) == 1

    def test_duplicate_event_id_is_idempotent(self, services, session):
        event_id = uuid.uuid4()
        first = services.ingestion.ingest(event_request(event_id=event_id))
        again = services.ingestion.ingest(event_request(event_id=event_id))
        session.commit()
        assert again.duplicate
        assert again.event.id == first.event.id
        state = services.state.get_or_create_state(first.event.learner)
        assert state.total_events == 1  # not applied twice
        assert len(services.reward_sink.calls) == 1

    def test_event_id_reused_for_another_learner_is_conflict(self, services, session):
        event_id = uuid.uuid4()
        services.ingestion.ingest(event_request(event_id=event_id, user="U1"))
        with pytest.raises(ConflictError):
            services.ingestion.ingest(event_request(event_id=event_id, user="STRANGER"))
        # ... and the stranger was never provisioned
        assert session.scalar(select(func.count()).select_from(Learner)) == 1

    def test_unknown_problem_is_422(self, services):
        with pytest.raises(BusinessRuleError):
            services.ingestion.ingest(event_request(problem="NOPE"))

    def test_validation_errors(self, services):
        with pytest.raises(ValidationError):
            services.ingestion.ingest(event_request(attempt=0))
        with pytest.raises(ValidationError):
            services.ingestion.ingest(event_request(confidence=7))


@pytest.mark.usefixtures("mini_catalog")
class TestCalibration:
    def test_calibrated_after_min_first_attempts(self, services, session, settings):
        problems = ["LC_E1", "LC_E2", "LC_E3", "LC_E4", "LC_M1", "LC_M2", "LC_M3", "LC_M4"]
        for i in range(settings.localization.calibration_min_events):
            services.ingestion.ingest(
                event_request(problem=problems[i], minutes_ago=100 - i))
        session.commit()
        learner = session.scalar(select(Learner).where(Learner.external_ref == "U1"))
        assert services.state.get_or_create_state(learner).calibrated

    def test_retries_of_one_problem_do_not_end_calibration(self, services, session, settings):
        for attempt in range(1, settings.localization.calibration_min_events + 4):
            services.ingestion.ingest(event_request(attempt=attempt))
        session.commit()
        learner = session.scalar(select(Learner).where(Learner.external_ref == "U1"))
        state = services.state.get_or_create_state(learner)
        assert state.total_events >= settings.localization.calibration_min_events
        assert not state.calibrated  # only ONE first-attempt response informed the estimate


# --- Seeding the real catalog -------------------------------------------------------

@pytest.mark.skipif(not REAL_CATALOG.exists(), reason="catalog_1000.json not present")
class TestSeed:
    def test_loads_real_catalog(self, session, settings):
        report = load_catalog(session, settings, str(REAL_CATALOG))
        session.commit()
        assert report.concepts == 25
        assert report.problems == 1000
        assert report.prerequisites == 21
        assert report.problem_concepts > 1000
        # second load is a no-op
        assert load_catalog(session, settings, str(REAL_CATALOG)).skipped

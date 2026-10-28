"""Knowledge-gap detection tests (docs/recommendation.md — the gap rule table).

Port of the gap-detection parts of LearningLoopIT (UNKNOWN, INSUFFICIENT_PRACTICE,
CONCEPT_GAP, PREREQUISITE_GAP, TIME_PRESSURE) and GraphDisabledIT (the
prerequisite-graph toggle), plus unit coverage of GapReport/GapType. All detection
tests drive events through EventIngestionService on the mini catalog, exactly like
the Java integration tests drive them through the HTTP API.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from knowledge_tracer.domain import Learner
from knowledge_tracer.knowledge_gap import GapReport, GapType, KnowledgeGapService
from knowledge_tracer.knowledge_graph import KnowledgeGraphService
from tests.conftest import event_request


@pytest.fixture()
def gap_service(session, settings) -> KnowledgeGapService:
    return KnowledgeGapService(session, settings, KnowledgeGraphService(session))


def _detect(services, session, gap_service, user: str = "U1") -> GapReport:
    learner = session.scalar(select(Learner).where(Learner.external_ref == user))
    state = services.state.get_or_create_state(learner)
    return gap_service.detect(state)


class TestGapReport:
    def test_none(self):
        report = GapReport.none()
        assert report.type is GapType.NONE
        assert report.concept_code is None
        assert report.prerequisite_code is None
        assert report.evidence == ["No weak concept or struggle pattern detected"]
        assert report.confidence == 0.6
        assert not report.involves_concept()

    def test_unknown(self):
        report = GapReport.unknown("some reason")
        assert report.type is GapType.UNKNOWN
        assert report.concept_code is None
        assert report.prerequisite_code is None
        assert report.evidence == ["some reason"]
        assert report.confidence == 0.0
        assert not report.involves_concept()

    def test_involves_concept(self):
        for gap_type in (GapType.CONCEPT_GAP, GapType.PREREQUISITE_GAP):
            assert GapReport(gap_type).involves_concept()
        for gap_type in (GapType.LOW_GLOBAL_ABILITY, GapType.INSUFFICIENT_PRACTICE,
                         GapType.HIGH_ITEM_DIFFICULTY, GapType.TIME_PRESSURE,
                         GapType.MULTIPLE_ATTEMPTS, GapType.LOW_CONFIDENCE,
                         GapType.UNKNOWN, GapType.NONE):
            assert not GapReport(gap_type).involves_concept()

    def test_gap_type_has_exactly_the_java_values(self):
        assert {t.name for t in GapType} == {
            "LOW_GLOBAL_ABILITY", "CONCEPT_GAP", "PREREQUISITE_GAP",
            "INSUFFICIENT_PRACTICE", "HIGH_ITEM_DIFFICULTY", "TIME_PRESSURE",
            "MULTIPLE_ATTEMPTS", "LOW_CONFIDENCE", "UNKNOWN", "NONE"}


@pytest.mark.usefixtures("mini_catalog")
class TestDetect:
    def test_unknown_for_learner_without_history(self, services, session, gap_service):
        learner = Learner(external_ref="GHOST")
        session.add(learner)
        session.flush()
        state = services.state.get_or_create_state(learner)
        report = gap_service.detect(state)
        assert report.type is GapType.UNKNOWN
        assert report.evidence == ["No interaction history yet"]
        assert report.confidence == 0.0

    def test_insufficient_practice(self, services, session, gap_service):
        """Port of LearningLoopIT.sparseLearnerIsFlaggedAsInsufficientPractice."""
        services.ingestion.ingest(event_request(problem="LC_E1"))
        report = _detect(services, session, gap_service)
        assert report.type is GapType.INSUFFICIENT_PRACTICE
        assert report.evidence == ["Only 1 observations so far; estimates are uncertain"]
        assert report.confidence == 0.4

    def test_concept_gap(self, services, session, gap_service):
        """Port of LearningLoopIT.repeatedFailureOnLinkedListIsDetectedAsConceptGap...

        One success, then repeated failures on HASHING problems; the only
        prerequisite (ARRAYS) is fine, so this is a plain concept gap.
        """
        services.ingestion.ingest(event_request(problem="LC_E1", result="ACCEPTED",
                                                minutes_ago=10))
        services.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                                minutes_ago=5))
        services.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                                attempt=2, minutes_ago=4))
        report = _detect(services, session, gap_service)
        assert report.type is GapType.CONCEPT_GAP
        assert report.concept_code == "HASHING"
        assert report.prerequisite_code is None
        assert report.involves_concept()
        assert report.evidence[0] == ("HASHING mastery estimate 0.25 is below "
                                      "threshold 0.45 (n=2 observations)")
        assert "2 recent unsuccessful attempt(s) on HASHING problems" in report.evidence
        assert report.confidence == pytest.approx(0.5 + 0.08 * 2)

    def test_prerequisite_gap(self, services, session, gap_service):
        """Port of LearningLoopIT.weakPrerequisiteIsDetectedAndReviewedFirst.

        DYNAMIC_PROGRAMMING is the weakest concept with enough evidence and its
        direct prerequisite HASHING is observed and weak too.
        """
        services.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                                minutes_ago=10))
        services.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                                attempt=2, minutes_ago=9))
        services.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                                minutes_ago=8))
        report = _detect(services, session, gap_service)
        assert report.type is GapType.PREREQUISITE_GAP
        assert report.concept_code == "DYNAMIC_PROGRAMMING"
        assert report.prerequisite_code == "HASHING"
        assert report.involves_concept()
        assert report.evidence[0] == ("DYNAMIC_PROGRAMMING mastery estimate 0.25 is "
                                      "below threshold 0.45 (n=2 observations)")
        assert ("2 recent unsuccessful attempt(s) on DYNAMIC_PROGRAMMING problems"
                in report.evidence)
        assert report.evidence[-1] == "prerequisite HASHING also weak (mastery 0.33)"
        assert report.confidence == pytest.approx(0.5 + 0.08 * 3)

    def test_unobserved_prerequisite_cannot_produce_prerequisite_gap(
            self, services, session, gap_service):
        # Only DYNAMIC_PROGRAMMING has evidence; both its prerequisites are
        # unobserved (evidence 0), which the Java filter excludes.
        services.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                                minutes_ago=5))
        services.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                                attempt=2, minutes_ago=4))
        report = _detect(services, session, gap_service)
        assert report.type is GapType.CONCEPT_GAP
        assert report.concept_code == "DYNAMIC_PROGRAMMING"
        assert report.prerequisite_code is None

    def test_graph_disabled_turns_prerequisite_gap_into_concept_gap(
            self, services, session, settings, gap_service):
        """Port of GraphDisabledIT: the same evidence that yields PREREQUISITE_GAP
        with the graph is only a CONCEPT_GAP without it."""
        services.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                                minutes_ago=10))
        services.ingestion.ingest(event_request(problem="LC_H1", result="WRONG_ANSWER",
                                                attempt=2, minutes_ago=9))
        services.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                                minutes_ago=8))
        settings.graph.enabled = False  # read at detect() time, as in the Java service
        report = _detect(services, session, gap_service)
        assert report.type is GapType.CONCEPT_GAP
        assert report.concept_code == "DYNAMIC_PROGRAMMING"
        assert report.prerequisite_code is None
        assert all("prerequisite" not in e for e in report.evidence)

    def test_low_global_ability(self, services, session, gap_service):
        # Five first-attempt failures on five disjoint concepts: no concept reaches
        # min_evidence, so the global rule fires instead of a concept gap.
        problems = ["LC_E1", "LC_E3", "LC_M1", "LC_M3", "LC_H1"]
        for i, problem in enumerate(problems):
            services.ingestion.ingest(event_request(problem=problem, result="WRONG_ANSWER",
                                                    minutes_ago=10 - i))
        learner = session.scalar(select(Learner).where(Learner.external_ref == "U1"))
        state = services.state.get_or_create_state(learner)
        assert state.ability < 3.0  # precondition of the rule
        report = gap_service.detect(state)
        assert report.type is GapType.LOW_GLOBAL_ABILITY
        assert not report.involves_concept()
        assert report.evidence[0].startswith("ability estimate")
        assert report.evidence[0].endswith("is low on the 0-10 scale")
        assert report.evidence[1] == "5 of last 5 attempts unsuccessful"
        assert report.confidence == pytest.approx(0.9)  # capped

    def test_multiple_attempts(self, services, session, gap_service):
        # First-attempt successes build ability and concept evidence...
        for i, problem in enumerate(["LC_E1", "LC_E3", "LC_M1", "LC_M3"]):
            services.ingestion.ingest(event_request(problem=problem, result="ACCEPTED",
                                                    minutes_ago=100 - i))
        # ... then failures on retries (not first attempts, so ability stays high).
        # Every concept sits at mastery 0.5 with evidence 2 — none is weak.
        services.ingestion.ingest(event_request(problem="LC_E1", result="WRONG_ANSWER",
                                                attempt=3, minutes_ago=50))
        services.ingestion.ingest(event_request(problem="LC_E3", result="WRONG_ANSWER",
                                                attempt=3, minutes_ago=49))
        services.ingestion.ingest(event_request(problem="LC_M1", result="WRONG_ANSWER",
                                                attempt=4, minutes_ago=48))
        services.ingestion.ingest(event_request(problem="LC_M3", result="WRONG_ANSWER",
                                                attempt=4, minutes_ago=47))
        report = _detect(services, session, gap_service)
        assert report.type is GapType.MULTIPLE_ATTEMPTS
        assert report.evidence == ["average 3.5 attempts on unsuccessful interactions"]
        assert report.confidence == pytest.approx(0.9)  # min(0.9, 0.5 + 0.08*8)

    def test_time_pressure(self, services, session, gap_service):
        """Port of LearningLoopIT.timeoutsAreReportedAsTimePressure: timeouts on
        problems whose concepts have no other weak evidence, so no concept gap
        pre-empts."""
        for i, problem in enumerate(["LC_E1", "LC_E3", "LC_M1", "LC_M3"]):
            services.ingestion.ingest(event_request(problem=problem, result="ACCEPTED",
                                                    minutes_ago=100 - i))
        services.ingestion.ingest(event_request(problem="LC_H1",
                                                result="TIME_LIMIT_EXCEEDED",
                                                minutes_ago=50))
        services.ingestion.ingest(event_request(problem="LC_M3",
                                                result="TIME_LIMIT_EXCEEDED",
                                                attempt=2, minutes_ago=49))
        report = _detect(services, session, gap_service)
        assert report.type is GapType.TIME_PRESSURE
        assert report.evidence == [
            "2 of 2 recent unsuccessful attempts were TIME_LIMIT_EXCEEDED",
            "evidence suggests solution efficiency, not correctness, is the obstacle"]
        assert report.confidence == pytest.approx(0.5 + 0.08 * 2)

    def test_low_confidence(self, services, session, gap_service):
        for i, problem in enumerate(["LC_E1", "LC_E3", "LC_M1", "LC_M3", "LC_H1"]):
            services.ingestion.ingest(event_request(problem=problem, result="ACCEPTED",
                                                    minutes_ago=10 - i, confidence=1))
        report = _detect(services, session, gap_service)
        assert report.type is GapType.LOW_CONFIDENCE
        assert report.evidence == ["average self-reported confidence 1.0/5 over 5 reports"]
        assert report.confidence == 0.5

    def test_none_for_healthy_learner(self, services, session, gap_service):
        problems = ["LC_E1", "LC_E2", "LC_E3", "LC_M1", "LC_M3", "LC_H1"]
        for i, problem in enumerate(problems):
            services.ingestion.ingest(event_request(problem=problem, result="ACCEPTED",
                                                    minutes_ago=100 - i))
        report = _detect(services, session, gap_service)
        assert report.type is GapType.NONE
        assert report.evidence == ["No weak concept or struggle pattern detected"]
        assert report.confidence == 0.6

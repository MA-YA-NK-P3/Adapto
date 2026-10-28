"""Knowledge-gap detection (master plan §16, docs/recommendation.md).

Port of gap.KnowledgeGapService, gap.GapReport and gap.GapType.

Rule-based evidence analysis over the learner's estimated state. Produces a
GapReport with explicit evidence and heuristic confidence — the system says
"evidence suggests", never "we know".

Rule order matters (the first match wins): prerequisite gaps are more actionable
than raw concept gaps, which are more specific than low global ability. The rules,
their ordering, thresholds, evidence strings and confidence values are faithful
ports of the Java service:

  1. PREREQUISITE_GAP — weakest concept with >= min_evidence observations is below
     weak_threshold AND its weakest direct prerequisite is observed (evidence >= 1)
     and also below weak_threshold (only when settings.graph.enabled)
  2. CONCEPT_GAP — same weak concept, prerequisites fine (or graph disabled)
  3. LOW_GLOBAL_ABILITY — ability < 3.0 and more than half of the recent window failed
  4. MULTIPLE_ATTEMPTS — unsuccessful recent events average >= 3 attempts
  5. TIME_PRESSURE — >= 2 TIME_LIMIT_EXCEEDED failures and >= 30% of recent failures
  6. LOW_CONFIDENCE — >= 3 self-reports averaging <= 2/5
  7. INSUFFICIENT_PRACTICE — fewer than 5 events in total
  8. NONE — nothing above; UNKNOWN — no interaction history at all

HIGH_ITEM_DIFFICULTY exists in GapType but is NEVER emitted by the Java service
(the rule is not implemented there); the replica keeps that behaviour on purpose.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .domain import (ConceptMastery, InteractionEvent, InteractionResult,
                     LearnerState, ProblemConcept)
from .knowledge_graph import KnowledgeGraphService


class GapType(enum.Enum):
    """Candidate explanations for learner struggle (master plan §16). Port of gap.GapType."""
    LOW_GLOBAL_ABILITY = "LOW_GLOBAL_ABILITY"
    CONCEPT_GAP = "CONCEPT_GAP"
    PREREQUISITE_GAP = "PREREQUISITE_GAP"
    INSUFFICIENT_PRACTICE = "INSUFFICIENT_PRACTICE"
    HIGH_ITEM_DIFFICULTY = "HIGH_ITEM_DIFFICULTY"  # defined but never emitted (Java parity)
    TIME_PRESSURE = "TIME_PRESSURE"
    MULTIPLE_ATTEMPTS = "MULTIPLE_ATTEMPTS"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNKNOWN = "UNKNOWN"
    NONE = "NONE"  # no gap detected — learner is progressing normally


@dataclass(frozen=True)
class GapReport:
    """Evidence-based (never causal-certain) answer to "why is the learner struggling?".

    Port of the gap.GapReport record.

    type:              most likely gap category
    concept_code:      concept the learner is struggling with, when applicable
    prerequisite_code: weak prerequisite of concept_code (PREREQUISITE_GAP only)
    evidence:          human-readable observations supporting the classification
    confidence:        heuristic confidence in [0,1] — an estimate, not a fact
    """
    type: GapType
    concept_code: Optional[str] = None
    prerequisite_code: Optional[str] = None
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.0

    @classmethod
    def none(cls) -> "GapReport":
        return cls(GapType.NONE,
                   evidence=["No weak concept or struggle pattern detected"],
                   confidence=0.6)

    @classmethod
    def unknown(cls, reason: str) -> "GapReport":
        return cls(GapType.UNKNOWN, evidence=[reason], confidence=0.0)

    def involves_concept(self) -> bool:
        return self.type in (GapType.CONCEPT_GAP, GapType.PREREQUISITE_GAP)


class KnowledgeGapService:
    """Port of gap.KnowledgeGapService (docs/recommendation.md, docs/knowledge-graph.md).

    The Java constructor injects KnowledgeTracerProperties, ConceptMasteryRepository,
    InteractionEventRepository, ProblemConceptRepository and KnowledgeGraphService;
    the replica collapses the three repositories into the SQLAlchemy session, as the
    other foundation services do.
    """

    def __init__(self, session: Session, settings: Settings,
                 graph_service: KnowledgeGraphService):
        self.session = session
        self.settings = settings
        self.graph = graph_service

    def detect(self, state: LearnerState) -> GapReport:
        """Classify the learner's most likely struggle from their estimated state."""
        if state.total_events == 0:
            return GapReport.unknown("No interaction history yet")

        mastery_cfg = self.settings.mastery
        masteries = list(self.session.scalars(
            select(ConceptMastery).where(ConceptMastery.learner_id == state.learner_id)))
        recent = list(self.session.scalars(
            select(InteractionEvent)
            .where(InteractionEvent.learner_id == state.learner_id)
            .order_by(InteractionEvent.occurred_at.desc())
            .limit(self.settings.recommendation.recent_window)))

        # Weakest concept with enough evidence to trust the estimate.
        eligible = [m for m in masteries if m.evidence_count >= mastery_cfg.min_evidence]
        weakest = min(eligible, key=lambda m: m.mastery) if eligible else None

        if weakest is not None and weakest.mastery < mastery_cfg.weak_threshold:
            evidence = [
                "%s mastery estimate %.2f is below threshold %.2f (n=%d observations)" % (
                    weakest.concept.code, weakest.mastery, mastery_cfg.weak_threshold,
                    weakest.evidence_count)]
            failures_on_concept = self._count_recent_failures_on_concept(
                recent, weakest.concept_id)
            if failures_on_concept > 0:
                evidence.append("%d recent unsuccessful attempt(s) on %s problems" % (
                    failures_on_concept, weakest.concept.code))

            if self.settings.graph.enabled:
                weak_prereq = self.graph.weakest_prerequisite(weakest.concept_id, masteries)
                if (weak_prereq is not None and weak_prereq.evidence_count >= 1
                        and weak_prereq.mastery < mastery_cfg.weak_threshold):
                    evidence.append("prerequisite %s also weak (mastery %.2f)" % (
                        weak_prereq.concept.code, weak_prereq.mastery))
                    return GapReport(
                        GapType.PREREQUISITE_GAP,
                        concept_code=weakest.concept.code,
                        prerequisite_code=weak_prereq.concept.code,
                        evidence=evidence,
                        confidence=self._confidence(
                            weakest.evidence_count + weak_prereq.evidence_count))
            return GapReport(GapType.CONCEPT_GAP,
                             concept_code=weakest.concept.code,
                             evidence=evidence,
                             confidence=self._confidence(weakest.evidence_count))

        recent_failures = sum(1 for e in recent if not e.result.is_correct)
        if recent and state.ability < 3.0 and recent_failures > len(recent) / 2.0:
            return GapReport(GapType.LOW_GLOBAL_ABILITY, evidence=[
                "ability estimate %.2f is low on the 0-10 scale" % state.ability,
                "%d of last %d attempts unsuccessful" % (recent_failures, len(recent))],
                confidence=self._confidence(state.total_events))

        failed_attempt_numbers = [e.attempt_number for e in recent if not e.result.is_correct]
        avg_attempts_on_failures = (sum(failed_attempt_numbers) / len(failed_attempt_numbers)
                                    if failed_attempt_numbers else 0.0)
        if avg_attempts_on_failures >= 3.0:
            return GapReport(GapType.MULTIPLE_ATTEMPTS, evidence=[
                "average %.1f attempts on unsuccessful interactions" % avg_attempts_on_failures],
                confidence=self._confidence(len(recent)))

        recent_failure_events = [e for e in recent if not e.result.is_correct]
        timeouts = sum(1 for e in recent_failure_events
                       if e.result is InteractionResult.TIME_LIMIT_EXCEEDED)
        if timeouts >= 2 and timeouts >= 0.3 * len(recent_failure_events):
            return GapReport(GapType.TIME_PRESSURE, evidence=[
                "%d of %d recent unsuccessful attempts were TIME_LIMIT_EXCEEDED" % (
                    timeouts, len(recent_failure_events)),
                "evidence suggests solution efficiency, not correctness, is the obstacle"],
                confidence=self._confidence(len(recent_failure_events)))

        confidences = [e.confidence for e in recent if e.confidence is not None]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 5.0
        if len(confidences) >= 3 and avg_confidence <= 2.0:
            return GapReport(GapType.LOW_CONFIDENCE, evidence=[
                "average self-reported confidence %.1f/5 over %d reports" % (
                    avg_confidence, len(confidences))],
                confidence=0.5)

        if state.total_events < 5:
            return GapReport(GapType.INSUFFICIENT_PRACTICE, evidence=[
                "Only %d observations so far; estimates are uncertain" % state.total_events],
                confidence=0.4)

        return GapReport.none()

    def _count_recent_failures_on_concept(self, recent: List[InteractionEvent],
                                          concept_id: uuid.UUID) -> int:
        """Recent unsuccessful events on problems tagged with the given concept."""
        count = 0
        for event in recent:
            if event.result.is_correct:
                continue
            tags = self.session.scalars(
                select(ProblemConcept).where(ProblemConcept.problem_id == event.problem_id))
            if any(tag.concept_id == concept_id for tag in tags):
                count += 1
        return count

    def _confidence(self, evidence_count: int) -> float:
        """Heuristic confidence: more evidence → more confidence, capped at 0.9."""
        return min(0.9, 0.5 + 0.08 * evidence_count)

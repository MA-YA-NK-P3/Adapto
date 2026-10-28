"""Candidate-action generation (master plan §17, docs/recommendation.md).

Port of recommendation.CandidateGenerationService and recommendation.CandidateAction.

The decision layer (decision.py) chooses among the candidates built here; it can never
invent an action outside this set. Scoring heuristic (docs/recommendation.md): candidates
are valued by how close their predicted success probability (success.SuccessEstimator:
IRT, blended with DKT when available) is to the configured target ("productive struggle"
zone), plus rule bonuses for addressing a detected gap.

Premium (paid-only) problems are never candidates. Problems are filtered in memory: at
1000 problems a full scan per request is a few milliseconds (docs/limitations.md). The
pool scan order is the repository's (insertion order in practice), exactly as Java's
ProblemRepository.findAll() — tie-breaks that keep "the first" candidate therefore
behave the same.

The seven rules, their triggers, expected values and reason strings are faithful ports:

  1. CALIBRATION_PROBLEM — learner not yet calibrated: the 2 unattempted items nearest
     the localization target; expected value 0.5 + Fisher information (pure IRT)
  2. PREREQUISITE_REVIEW — PREREQUISITE_GAP: remediation item on the prerequisite,
     target difficulty ability - 1.0; expected value 0.8
  3. EASIER_PROBLEM — gap.involves_concept() (CONCEPT_GAP or PREREQUISITE_GAP):
     remediation item on the concept, target ability - 1.5; expected value 0.75
  4. SAME_LEVEL_PROBLEM — always: unattempted item with P closest to the target;
     expected value 1 - |P - target|
  5. HARDER_PROBLEM — recent success streak >= 3: unattempted item in (ability,
     ability + 2]; expected value 0.55
  6. RETRY_PROBLEM — a recent failure exists: the most recently failed problem;
     expected value 0.5
  7. SPACED_REVIEW — a solved problem not seen in the recent window; expected value 0.4
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .domain import (ActionType, InteractionEvent, InteractionResult, Learner,
                     LearnerState, Problem, ProblemConcept)
from .irt import information
from .knowledge_gap import GapReport, GapType
from .localization import AdaptiveLocalizationService

if TYPE_CHECKING:  # success.py does not import this module; the hint is cycle-free.
    from .success import Estimate

#: Tag weight from which a problem counts as exercising a concept directly (catalog tier B).
STRONG_TAG_WEIGHT = 0.7


@dataclass(frozen=True)
class CandidateAction:
    """One structured candidate action (master plan §17).

    Port of the recommendation.CandidateAction record. The decision layer chooses among
    these; it can never invent an action outside the candidate set.

    expected_value:    heuristic value score in [0,1] (documented in docs/recommendation.md)
    predicted_success: model-predicted P(correct) on the referenced problem (IRT, or IRT
                       blended with DKT); None for actions without a problem
    """
    action: ActionType
    problem_key: Optional[str]
    concept_code: Optional[str]
    difficulty: Optional[float]
    reason: str
    expected_value: float
    predicted_success: Optional[float] = None


class CandidateGenerationService:
    """Port of recommendation.CandidateGenerationService (docs/recommendation.md).

    The Java constructor injects KnowledgeTracerProperties, ProblemRepository,
    ProblemConceptRepository, InteractionEventRepository and
    AdaptiveLocalizationService; the replica collapses the three repositories into the
    SQLAlchemy session, as the other foundation services do.
    """

    def __init__(self, session: Session, settings: Settings,
                 localization_service: AdaptiveLocalizationService):
        self.session = session
        self.settings = settings
        self.localization = localization_service

    def generate(self, learner: Learner, state: LearnerState, gap: GapReport,
                 estimate: "Estimate") -> List[CandidateAction]:
        """The bounded candidate set for one learner, in Java rule order."""
        ability = state.ability
        target = self.settings.recommendation.target_success_probability

        pool = [p for p in self.session.scalars(select(Problem)) if not p.paid_only]
        attempted = self._attempted_problem_ids(learner.id)
        solved = self._solved_problem_ids(learner.id)
        recent = list(self.session.scalars(
            select(InteractionEvent)
            .where(InteractionEvent.learner_id == learner.id)
            .order_by(InteractionEvent.occurred_at.desc())
            .limit(self.settings.recommendation.recent_window)))

        candidates: List[CandidateAction] = []

        # 1. Cold start: maximally informative items near the current estimate (§11, §37).
        #    Information is an IRT quantity, so calibration stays pure IRT.
        if self.localization.needs_calibration(state):
            target_difficulty = self.localization.calibration_target_difficulty(state)
            nearest = sorted(
                (p for p in pool if p.id not in attempted),
                key=lambda p: abs(p.estimated_difficulty - target_difficulty))[:2]
            for p in nearest:
                candidates.append(CandidateAction(
                    ActionType.CALIBRATION_PROBLEM, p.problem_key, None,
                    p.estimated_difficulty,
                    "Cold-start calibration: item near current estimate %.2f "
                    "maximizes information" % target_difficulty,
                    0.5 + information(ability, p.estimated_difficulty),
                    estimate.irt(p)))

        # 2. Gap-directed candidates: easiest suitable unsolved item on the relevant
        #    concept, preferring items that exercise it directly, unattempted items and
        #    items at or below (ability - offset).
        if gap.type is GapType.PREREQUISITE_GAP and gap.prerequisite_code is not None:
            p = self._remediation_problem(gap.prerequisite_code, ability - 1.0,
                                          attempted, solved)
            if p is not None:
                candidates.append(CandidateAction(
                    ActionType.PREREQUISITE_REVIEW, p.problem_key, gap.prerequisite_code,
                    p.estimated_difficulty,
                    "Evidence suggests prerequisite " + gap.prerequisite_code
                    + " is weak; strengthen it before continuing with " + gap.concept_code,
                    0.8, estimate.predict(p)))
        if gap.involves_concept() and gap.concept_code is not None:
            p = self._remediation_problem(gap.concept_code, ability - 1.5,
                                          attempted, solved)
            if p is not None:
                candidates.append(CandidateAction(
                    ActionType.EASIER_PROBLEM, p.problem_key, gap.concept_code,
                    p.estimated_difficulty,
                    "Easier " + gap.concept_code + " problem to rebuild the weak concept",
                    0.75, estimate.predict(p)))

        # 3. Same-level consolidation: unattempted problem closest to the target zone.
        scored = [(p, estimate.predict(p)) for p in pool if p.id not in attempted]
        if scored:
            best_p, best_prob = min(scored,
                                    key=lambda s: (abs(s[1] - target), s[0].problem_key))
            candidates.append(CandidateAction(
                ActionType.SAME_LEVEL_PROBLEM, best_p.problem_key, None,
                best_p.estimated_difficulty,
                "Predicted success %.2f (%s) is closest to the target zone %.2f"
                % (best_prob, estimate.source(), target),
                1.0 - abs(best_prob - target), best_prob))

        # 4. Growth: only after a recent success streak.
        if _recent_success_streak(recent) >= 3:
            harder = [p for p in pool
                      if p.id not in attempted
                      and ability < p.estimated_difficulty <= ability + 2.0]
            if harder:
                p = min(harder, key=lambda p: p.estimated_difficulty)
                candidates.append(CandidateAction(
                    ActionType.HARDER_PROBLEM, p.problem_key, None,
                    p.estimated_difficulty,
                    "Recent success streak suggests readiness for a stretch problem",
                    0.55, estimate.predict(p)))

        # 5. Retry the most recently failed problem.
        failure = next((e for e in recent if not e.result.is_correct), None)
        if failure is not None:
            p = failure.problem
            candidates.append(CandidateAction(
                ActionType.RETRY_PROBLEM, p.problem_key, None, p.estimated_difficulty,
                "Re-attempt the most recently failed problem after state update",
                0.5, estimate.predict(p)))

        # 6. Spaced review of an old solved problem.
        recent_problem_ids = {e.problem_id for e in recent}
        for p in pool:
            if p.id in solved and p.id not in recent_problem_ids:
                candidates.append(CandidateAction(
                    ActionType.SPACED_REVIEW, p.problem_key, None, p.estimated_difficulty,
                    "Spaced review of a previously solved problem",
                    0.4, estimate.predict(p)))
                break

        return candidates

    def _remediation_problem(self, concept_code: str, target_difficulty: float,
                             attempted: Set[uuid.UUID],
                             solved: Set[uuid.UUID]) -> Optional[Problem]:
        """Easiest suitable unsolved free item on a concept (port of remediationProblem).

        Preference order: exercises the concept directly (tag weight >= 0.7), then
        unattempted, then closest below the target difficulty, then easiest. Ties keep
        the first row in repository order, as Java's Stream.min does.
        """
        rows = list(self.session.scalars(
            select(ProblemConcept)
            .where(ProblemConcept.concept.has(code=concept_code))))
        best: Optional[Problem] = None
        best_key = None
        for pc in rows:
            problem = self.session.get(Problem, pc.problem_id)
            if problem.paid_only or problem.id in solved:
                continue
            key = (pc.weight < STRONG_TAG_WEIGHT,
                   problem.id in attempted,
                   max(0.0, problem.estimated_difficulty - target_difficulty),
                   problem.estimated_difficulty)
            if best_key is None or key < best_key:
                best, best_key = problem, key
        return best

    def _attempted_problem_ids(self, learner_id: uuid.UUID) -> Set[uuid.UUID]:
        """Port of InteractionEventRepository.findAttemptedProblemIds."""
        return set(self.session.scalars(
            select(InteractionEvent.problem_id)
            .where(InteractionEvent.learner_id == learner_id)
            .distinct()))

    def _solved_problem_ids(self, learner_id: uuid.UUID) -> Set[uuid.UUID]:
        """Port of InteractionEventRepository.findSolvedProblemIds (ACCEPTED only)."""
        return set(self.session.scalars(
            select(InteractionEvent.problem_id)
            .where(InteractionEvent.learner_id == learner_id)
            .where(InteractionEvent.result == InteractionResult.ACCEPTED)
            .distinct()))


def _recent_success_streak(recent: List[InteractionEvent]) -> int:
    """Consecutive correct events from the most recent backwards (recentSuccessStreak)."""
    streak = 0
    for event in recent:
        if event.result.is_correct:
            streak += 1
        else:
            break
    return streak

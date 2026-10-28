"""Rasch (1PL) item response theory baseline on a 0-10 scale (docs/irt.md).

Port of irt.IrtModel (the pure maths) and irt.IrtService (persistence-aware wrappers).

Model: P(correct | ability theta, difficulty b) = sigmoid(theta - b).

Estimation: maximum-a-posteriori Newton-Raphson with a Gaussian prior
N(prior_mean, prior_sd^2). The prior provides cold-start behaviour and keeps
all-correct / all-wrong response patterns finite. Uncertainty is the posterior
standard deviation 1/sqrt(I) where I is the observed Fisher information plus the
prior precision.

Guaranteed invariants (unit-tested):
  * probability in (0,1), increasing in ability, decreasing in difficulty
  * a correct response never lowers the estimate relative to an incorrect one
  * uncertainty >= 0 and non-increasing as informative evidence accumulates
  * ability and difficulty are always finite and clamped to [0,10]
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from .config import Settings
from .domain import InteractionEvent, LearnerState, Problem, StaticDifficulty

MIN_THETA = 0.0
MAX_THETA = 10.0
_MAX_ITERATIONS = 50
_CONVERGENCE_TOLERANCE = 1e-7


@dataclass(frozen=True)
class ResponseObservation:
    item_difficulty: float
    correct: bool


@dataclass(frozen=True)
class ItemObservation:
    ability: float
    correct: bool


@dataclass(frozen=True)
class AbilityEstimate:
    ability: float
    uncertainty: float
    sample_size: int


@dataclass(frozen=True)
class DifficultyEstimate:
    difficulty: float
    uncertainty: float
    sample_size: int


def clamp(theta: float) -> float:
    if math.isnan(theta) or math.isinf(theta):
        return (MIN_THETA + MAX_THETA) / 2.0
    return min(MAX_THETA, max(MIN_THETA, theta))


def sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def probability_of_success(ability: float, difficulty: float) -> float:
    return sigmoid(ability - difficulty)


def information(ability: float, difficulty: float) -> float:
    """Fisher information of one item for a learner of the given ability. Max at b = theta."""
    p = probability_of_success(ability, difficulty)
    return p * (1.0 - p)


def estimate_ability(responses: List[ResponseObservation],
                     prior_mean: float, prior_sd: float) -> AbilityEstimate:
    """MAP ability estimate from a window of responses (any order)."""
    theta = clamp(prior_mean)
    prior_precision = 1.0 / (prior_sd * prior_sd)

    for _ in range(_MAX_ITERATIONS):
        gradient = -(theta - prior_mean) * prior_precision
        info = prior_precision
        for r in responses:
            p = probability_of_success(theta, r.item_difficulty)
            gradient += (1.0 if r.correct else 0.0) - p
            info += p * (1.0 - p)
        step = gradient / info
        theta = clamp(theta + step)
        if abs(step) < _CONVERGENCE_TOLERANCE:
            break

    info = prior_precision
    for r in responses:
        info += information(theta, r.item_difficulty)
    return AbilityEstimate(theta, 1.0 / math.sqrt(info), len(responses))


def estimate_difficulty(observations: List[ItemObservation],
                        prior_mean: float, prior_sd: float) -> DifficultyEstimate:
    """MAP item difficulty from population responses."""
    b = clamp(prior_mean)
    prior_precision = 1.0 / (prior_sd * prior_sd)

    for _ in range(_MAX_ITERATIONS):
        gradient = -(b - prior_mean) * prior_precision
        info = prior_precision
        for o in observations:
            p = probability_of_success(o.ability, b)
            gradient += p - (1.0 if o.correct else 0.0)   # dL/db = sum(p - y)
            info += p * (1.0 - p)
        step = gradient / info
        b = clamp(b + step)
        if abs(step) < _CONVERGENCE_TOLERANCE:
            break

    info = prior_precision
    for o in observations:
        info += information(o.ability, b)
    return DifficultyEstimate(b, 1.0 / math.sqrt(info), len(observations))


class IrtService:
    """Applies the model above to persisted entities (port of irt.IrtService)."""

    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def estimate_ability_for(self, learner_id) -> AbilityEstimate:
        """Re-estimate a learner's ability from their most recent responses."""
        cfg = self.settings.irt
        events = _first_attempts_by_learner(self.session, learner_id, cfg.window_size) \
            if cfg.first_attempts_only else _recent_by_learner(self.session, learner_id, cfg.window_size)
        responses = [ResponseObservation(e.problem.estimated_difficulty, e.result.is_correct)
                     for e in events]
        return estimate_ability(responses, cfg.ability_prior_mean, cfg.ability_prior_sd)

    def recalibrate_item(self, problem: Problem) -> DifficultyEstimate:
        """Re-calibrate an item's difficulty from recent population responses.

        Known approximation (docs/limitations.md): learner abilities are taken at their
        CURRENT value rather than at response time. Acceptable at prototype scale.
        """
        cfg = self.settings.irt
        recent = _first_attempts_by_problem(self.session, problem.id, cfg.item_window_size) \
            if cfg.first_attempts_only else _recent_by_problem(self.session, problem.id, cfg.item_window_size)
        observations = [ItemObservation(self._current_ability(e.learner_id), e.result.is_correct)
                        for e in recent]
        estimate = estimate_difficulty(observations, problem.prior_difficulty, cfg.difficulty_prior_sd)
        problem.estimated_difficulty = estimate.difficulty
        problem.difficulty_uncertainty = estimate.uncertainty
        problem.sample_size = estimate.sample_size
        problem.updated_at = _now()
        return estimate

    def _current_ability(self, learner_id) -> float:
        state = self.session.get(LearnerState, learner_id)
        return state.ability if state else self.settings.irt.ability_prior_mean

    def static_prior(self, difficulty: StaticDifficulty) -> float:
        """TEMPORARY calibration constants (master plan §12): label -> prior difficulty."""
        cfg = self.settings.irt
        return {StaticDifficulty.EASY: cfg.easy_prior_difficulty,
                StaticDifficulty.MEDIUM: cfg.medium_prior_difficulty,
                StaticDifficulty.HARD: cfg.hard_prior_difficulty}[difficulty]

    def prior(self, difficulty: StaticDifficulty, acceptance_rate: Optional[float]) -> float:
        """Label prior shifted by platform acceptance: within a label, problems accepted
        less often than typical start harder. Mirrors prior_difficulty in
        scripts/build_leetcode_catalog.py so API-registered and catalog problems agree."""
        base = self.static_prior(difficulty)
        if acceptance_rate is None or acceptance_rate <= 0.0:
            return base
        cfg = self.settings.irt
        mean, sd = {
            StaticDifficulty.EASY: (cfg.easy_acceptance_logit_mean, cfg.easy_acceptance_logit_sd),
            StaticDifficulty.MEDIUM: (cfg.medium_acceptance_logit_mean, cfg.medium_acceptance_logit_sd),
            StaticDifficulty.HARD: (cfg.hard_acceptance_logit_mean, cfg.hard_acceptance_logit_sd),
        }[difficulty]
        p = min(max(acceptance_rate, 1e-4), 1 - 1e-4)
        z = (math.log(p / (1 - p)) - mean) / sd
        shift = max(-cfg.acceptance_max_shift, min(cfg.acceptance_max_shift, -cfg.acceptance_shift * z))
        prior = min(MAX_THETA - 0.5, max(MIN_THETA + 0.5, base + shift))
        return round(prior * 100.0) / 100.0


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _first_attempts_by_learner(session: Session, learner_id, limit: int) -> List[InteractionEvent]:
    """The learner's FIRST attempt on each problem, most recent problems first.

    Mirrors InteractionEventRepository.findFirstAttemptsByLearner: an event is a first
    attempt when its occurred_at equals the earliest occurred_at for that
    (learner, problem) pair.
    """
    inner = aliased(InteractionEvent)
    first_times = (select(func.min(inner.occurred_at))
                   .where(inner.learner_id == InteractionEvent.learner_id)
                   .where(inner.problem_id == InteractionEvent.problem_id)
                   .correlate(InteractionEvent)
                   .scalar_subquery())
    stmt = (select(InteractionEvent)
            .where(InteractionEvent.learner_id == learner_id)
            .where(InteractionEvent.occurred_at == first_times)
            .order_by(InteractionEvent.occurred_at.desc())
            .limit(limit))
    return list(session.scalars(stmt))


def _first_attempts_by_problem(session: Session, problem_id, limit: int) -> List[InteractionEvent]:
    """Each learner's FIRST attempt on the problem, most recent first."""
    inner = aliased(InteractionEvent)
    first_times = (select(func.min(inner.occurred_at))
                   .where(inner.learner_id == InteractionEvent.learner_id)
                   .where(inner.problem_id == InteractionEvent.problem_id)
                   .correlate(InteractionEvent)
                   .scalar_subquery())
    stmt = (select(InteractionEvent)
            .where(InteractionEvent.problem_id == problem_id)
            .where(InteractionEvent.occurred_at == first_times)
            .order_by(InteractionEvent.occurred_at.desc())
            .limit(limit))
    return list(session.scalars(stmt))


def _recent_by_learner(session: Session, learner_id, limit: int) -> List[InteractionEvent]:
    stmt = (select(InteractionEvent).where(InteractionEvent.learner_id == learner_id)
            .order_by(InteractionEvent.occurred_at.desc()).limit(limit))
    return list(session.scalars(stmt))


def _recent_by_problem(session: Session, problem_id, limit: int) -> List[InteractionEvent]:
    stmt = (select(InteractionEvent).where(InteractionEvent.problem_id == problem_id)
            .order_by(InteractionEvent.occurred_at.desc()).limit(limit))
    return list(session.scalars(stmt))

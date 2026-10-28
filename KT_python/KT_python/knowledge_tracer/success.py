"""Predicted success probability used to score candidates (docs/recommendation.md).

Port of recommendation.SuccessEstimator (and its inner Estimate class): Rasch IRT,
blended with DKT when a model is loaded and the learner has enough history.

    P(success on q) = (1 - w) * IRT + w * DKT      when a DKT snapshot exists AND the
                                                     snapshot has a prediction for q
                    = IRT                          otherwise

The DKT predictor is DUCK-TYPED (DktPredictorLike): knowledge_tracer.dkt is written in
parallel and is intentionally NOT imported here. Any object with
`snapshot(learner_id) -> Optional[snapshot]`, where the snapshot has
`predict_success(problem) -> Optional[float]` and `mastery() -> Dict[str, float]`,
works (tests use a fake).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Protocol

from sqlalchemy.orm import Session

from .config import Settings
from .domain import Learner, LearnerState, Problem
from .irt import probability_of_success


class DktSnapshotLike(Protocol):
    """The slice of dkt.DktSnapshot that success estimation blends with."""
    def predict_success(self, problem: Problem) -> Optional[float]:
        """Empty when the problem has no concept the model knows."""
        ...
    def mastery(self) -> Dict[str, float]:
        """Concept code -> DKT mastery estimate."""
        ...


class DktPredictorLike(Protocol):
    """The slice of dkt.DktPredictor used here (duck-typed; see module docstring)."""
    def snapshot(self, learner_id) -> Optional[DktSnapshotLike]:
        ...


@dataclass(frozen=True)
class Estimate:
    """Port of the SuccessEstimator.Estimate inner class.

    source() is "IRT+DKT" whenever a snapshot exists — even when a given problem fell
    back to pure IRT because the snapshot had no prediction for it (Java parity).
    """
    ability: float
    dkt_snapshot: Optional[DktSnapshotLike]
    blend_weight: float

    def irt(self, problem: Problem) -> float:
        """Pure Rasch P(correct) = sigmoid(ability - difficulty)."""
        return probability_of_success(self.ability, problem.estimated_difficulty)

    def predict(self, problem: Problem) -> float:
        """Blended estimate: (1-w)*IRT + w*DKT when a DKT prediction exists, else IRT."""
        irt_p = self.irt(problem)
        if self.dkt_snapshot is None:
            return irt_p
        dkt_p = self.dkt_snapshot.predict_success(problem)
        if dkt_p is None:
            return irt_p
        return (1.0 - self.blend_weight) * irt_p + self.blend_weight * dkt_p

    def dkt(self) -> Optional[DktSnapshotLike]:
        return self.dkt_snapshot

    def source(self) -> str:
        return "IRT" if self.dkt_snapshot is None else "IRT+DKT"


class SuccessEstimator:
    """Port of recommendation.SuccessEstimator.

    The Java class injects only KnowledgeTracerProperties and DktPredictor; the replica
    additionally takes the SQLAlchemy session as its first constructor argument for
    wiring uniformity with the other services (it is not used for queries).
    """

    def __init__(self, session: Session, settings: Settings,
                 dkt_predictor: DktPredictorLike):
        self.session = session
        self.settings = settings
        self.dkt_predictor = dkt_predictor

    def for_learner(self, learner: Learner, state: LearnerState) -> Estimate:
        """IRT estimate at the learner's current ability, blended with a DKT snapshot
        when DKT is enabled and the learner has at least min_history events."""
        cfg = self.settings.dkt
        snapshot = (self.dkt_predictor.snapshot(learner.id)
                    if cfg.enabled and state.total_events >= cfg.min_history
                    else None)
        return Estimate(state.ability, snapshot, cfg.blend_weight)

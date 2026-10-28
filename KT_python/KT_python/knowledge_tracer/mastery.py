"""Baseline knowledge tracing: Beta-Bernoulli evidence accumulation per concept.

Port of mastery.ConceptMasteryService.

Evidence is weighted by observation quality (documented heuristics):
  * success with a hint counts 0.7x — the learner needed support
  * success after viewing the solution counts 0.4x — weak evidence of mastery
  * failure after viewing the solution counts 0.5x — ambiguous signal
  * success with self-reported confidence <= 2 counts 0.8x — possibly lucky
  * success after more than 45 minutes counts 0.8x — "slow pass" is weaker evidence
These weights are experimental parameters, not calibrated constants.

A single failure can never destroy mastery: with prior (1,1) one failure moves
mastery from 0.5 to 0.33, and subsequent evidence dominates smoothly. The optional
DKT model only adjusts candidate success estimates; it does not replace this
mastery state (docs/dkt.md).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .domain import Concept, ConceptMastery, InteractionEvent, Learner

SLOW_SUCCESS_SECONDS = 45 * 60


class ConceptMasteryService:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def apply_event(self, learner: Learner, concept: Concept,
                    event: InteractionEvent, tag_weight: float) -> ConceptMastery:
        """Apply one interaction event to the learner's mastery of one concept.

        tag_weight: how strongly the problem exercises the concept (0-1).
        """
        cfg = self.settings.mastery
        mastery = self.session.scalar(
            select(ConceptMastery)
            .where(ConceptMastery.learner_id == learner.id)
            .where(ConceptMastery.concept_id == concept.id))
        if mastery is None:
            mastery = ConceptMastery(learner_id=learner.id, concept_id=concept.id,
                                     alpha=cfg.prior_alpha, beta=cfg.prior_beta,
                                     mastery=cfg.prior_alpha / (cfg.prior_alpha + cfg.prior_beta),
                                     evidence_count=0)
            self.session.add(mastery)

        evidence_weight = tag_weight * credit_weight(event)
        if event.result.is_correct:
            mastery.alpha += evidence_weight
        else:
            mastery.beta += evidence_weight
        mastery.evidence_count += 1
        mastery.mastery = mastery.alpha / (mastery.alpha + mastery.beta)
        mastery.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return mastery


def credit_weight(event: InteractionEvent) -> float:
    """Quality-of-evidence weight for one observation."""
    w = 1.0
    if event.result.is_correct:
        if event.hint_used:
            w *= 0.7
        if event.solution_viewed:
            w *= 0.4
        if event.confidence is not None and event.confidence <= 2:
            w *= 0.8
        if event.time_spent_seconds is not None and event.time_spent_seconds > SLOW_SUCCESS_SECONDS:
            w *= 0.8
    else:
        if event.solution_viewed:
            w *= 0.5
    return w

"""Derived learner state, port of state.LearnerStateService.

Runs INSIDE the event-ingestion transaction so an event and the state transition it
causes are atomic (master plan §54 — documented transactional boundary).

Update order per event: IRT ability -> calibration progress -> item recalibration ->
concept mastery. Item recalibration runs after the ability update so the newest
ability estimate informs the item fit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .domain import (ConceptMastery, InteractionEvent, Learner, LearnerState,
                     Problem, ProblemConcept)
from .irt import IrtService
from .localization import AdaptiveLocalizationService
from .mastery import ConceptMasteryService


class LearnerStateService:
    def __init__(self, session: Session, settings: Settings,
                 irt_service: Optional[IrtService] = None,
                 mastery_service: Optional[ConceptMasteryService] = None,
                 localization_service: Optional[AdaptiveLocalizationService] = None):
        self.session = session
        self.settings = settings
        self.irt = irt_service or IrtService(session, settings)
        self.mastery = mastery_service or ConceptMasteryService(session, settings)
        self.localization = localization_service or AdaptiveLocalizationService(settings)

    def get_or_create_state(self, learner: Learner) -> LearnerState:
        """Current state, creating the cold-start prior state on first access."""
        state = self.session.get(LearnerState, learner.id)
        if state is None:
            cfg = self.settings.irt
            state = LearnerState(learner_id=learner.id, ability=cfg.ability_prior_mean,
                                 uncertainty=cfg.ability_prior_sd)
            self.session.add(state)
            self.session.flush()
        return state

    def apply_event(self, learner: Learner, problem: Problem, event: InteractionEvent) -> None:
        """Fold one persisted event into all derived state. Call inside a transaction."""
        state = self.get_or_create_state(learner)
        state.total_events += 1

        informative_responses = state.total_events
        if self.settings.irt.enabled:
            estimate = self.irt.estimate_ability_for(learner.id)
            state.ability = estimate.ability
            state.uncertainty = estimate.uncertainty
            informative_responses = estimate.sample_size
        state.updated_at = datetime.now(timezone.utc)
        self.localization.update_calibration_progress(state, informative_responses)
        self.session.flush()

        if self.settings.irt.enabled:
            self.irt.recalibrate_item(problem)

        tags = self.session.scalars(
            select(ProblemConcept).where(ProblemConcept.problem_id == problem.id))
        for tag in tags:
            self.mastery.apply_event(learner, tag.concept, event, tag.weight)

    def masteries_of(self, learner: Learner) -> List[ConceptMastery]:
        return list(self.session.scalars(
            select(ConceptMastery).where(ConceptMastery.learner_id == learner.id)))

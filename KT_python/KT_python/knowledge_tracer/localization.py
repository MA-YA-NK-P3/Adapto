"""Adaptive localization (master plan §11): efficiently estimate the learner's current
region on the 0-10 difficulty landscape during cold start.

Port of localization.AdaptiveLocalizationService.

Mechanism: while the learner is uncalibrated, recommend items with difficulty close to
the current ability estimate — for the Rasch model Fisher information I(theta) = p(1-p)
is maximal at b = theta, so each response is maximally informative. Because ability is
re-estimated after every response, the selected difficulty tracks the estimate:
fail -> estimate drops -> easier next item; pass -> harder. This yields the
step-down/step-up behaviour from the plan (5 -> 3 -> 4 -> ~4.1) without hard-coding a
binary search.

Limitation (documented): localization alone is NOT a complete knowledge-tracing model;
it only estimates global ability, not concept structure.
"""
from __future__ import annotations

from .config import Settings
from .domain import LearnerState
from .irt import clamp


class AdaptiveLocalizationService:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.localization.enabled

    def needs_calibration(self, state: LearnerState) -> bool:
        """True while the learner is still in the cold-start calibration phase (§37)."""
        return self.enabled and not state.calibrated

    def calibration_target_difficulty(self, state: LearnerState) -> float:
        """Difficulty target for the next calibration item: at the current estimate."""
        return clamp(state.ability)

    def update_calibration_progress(self, state: LearnerState, informative_responses: int) -> None:
        """Mark the learner calibrated once uncertainty is low enough OR enough evidence
        has accumulated — whichever comes first. Calibration is a phase, not a label.

        informative_responses counts responses that informed the ability estimate
        (first attempts under the Rasch model), so retries of one problem cannot end
        calibration on their own.
        """
        if not self.enabled or state.calibrated:
            return
        cfg = self.settings.localization
        if (state.uncertainty <= cfg.calibration_uncertainty_threshold
                or informative_responses >= cfg.calibration_min_events):
            state.calibrated = True

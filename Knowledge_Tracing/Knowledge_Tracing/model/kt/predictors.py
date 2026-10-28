"""Next-response predictors for the offline prediction evaluation.

Every predictor is evaluated online: predict(pid) is called BEFORE observe(pid, correct).
"""
import math
from typing import Dict, Optional

from .catalog import Catalog
from .irt import STATIC_PRIOR, estimate_ability, p_success, sigmoid
from .mastery import MasteryState, logit


class Predictor:
    name = "predictor"

    def reset(self) -> None:
        raise NotImplementedError

    def predict(self, pid: str) -> float:
        raise NotImplementedError

    def observe(self, pid: str, correct: bool) -> None:
        raise NotImplementedError


class StaticDifficultyPredictor(Predictor):
    """Baseline: assume an average learner (theta=5) and the static label prior."""
    name = "static-difficulty"

    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def reset(self):
        pass

    def predict(self, pid):
        return p_success(5.0, STATIC_PRIOR[self.catalog.labels[pid]])

    def observe(self, pid, correct):
        pass


class SuccessRatePredictor(Predictor):
    """Baseline: Laplace-smoothed running success rate of the learner."""
    name = "success-rate"

    def reset(self):
        self.s, self.n = 0, 0

    def predict(self, pid):
        return (self.s + 1) / (self.n + 2)

    def observe(self, pid, correct):
        self.s += int(correct)
        self.n += 1


class IrtMasteryPredictor(Predictor):
    """logit P = (theta - b) + w_m * concept_term + w_g * prerequisite_term.

    w_m = w_g = 0 is the pure Rasch model ("IRT only").
    concept_term      = weighted mean logit(mastery) over the problem's tagged concepts
    prerequisite_term = min logit(mastery) over direct prerequisites of tagged concepts (0 if none)
    """

    def __init__(self, name: str, catalog: Catalog, difficulty: Dict[str, float],
                 w_mastery: float = 0.0, w_graph: float = 0.0):
        self.name = name
        self.catalog = catalog
        self.difficulty = difficulty
        self.w_mastery = w_mastery
        self.w_graph = w_graph

    def reset(self):
        self.history = []
        self.theta = 5.0
        self.mastery = MasteryState()

    def _concept_term(self, pid: str) -> float:
        tags = self.catalog.tags[pid]
        total = sum(tags.values())
        return sum(logit(self.mastery.mastery_or_prior(c)) * w for c, w in tags.items()) / total

    def _prereq_term(self, pid: str) -> float:
        terms = [logit(self.mastery.mastery_or_prior(p))
                 for c in self.catalog.tags[pid] for p in self.catalog.prerequisites.get(c, [])]
        return min(terms) if terms else 0.0

    def predict(self, pid):
        z = self.theta - self.difficulty[pid]
        if self.w_mastery:
            z += self.w_mastery * self._concept_term(pid)
        if self.w_graph:
            z += self.w_graph * min(0.0, self._prereq_term(pid))
        return sigmoid(z)

    def observe(self, pid, correct):
        self.history.append((self.difficulty[pid], correct))
        self.theta, _ = estimate_ability(self.history)
        self.mastery.update(self.catalog.tags[pid], correct)


class UnavailablePredictor(Predictor):
    """Placeholder row for components that cannot run (e.g. DKT without torch/data)."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason

    def reset(self):
        pass

    def predict(self, pid) -> Optional[float]:
        return None

    def observe(self, pid, correct):
        pass


def safe_mean(xs):
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return sum(xs) / len(xs) if xs else float("nan")

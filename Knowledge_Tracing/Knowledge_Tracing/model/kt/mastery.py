"""Beta-Bernoulli concept mastery — reference implementation of backend ConceptMasteryService."""
import math
from typing import Dict, Optional

WEAK_THRESHOLD = 0.45
MIN_EVIDENCE = 2


class ConceptMastery:
    __slots__ = ("alpha", "beta", "evidence")

    def __init__(self, alpha: float = 1.0, beta: float = 1.0):
        self.alpha = alpha
        self.beta = beta
        self.evidence = 0

    @property
    def mastery(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def uncertainty(self) -> float:
        m = self.mastery
        return math.sqrt(m * (1.0 - m) / (self.alpha + self.beta + 1.0))

    def update(self, correct: bool, weight: float = 1.0) -> None:
        if correct:
            self.alpha += weight
        else:
            self.beta += weight
        self.evidence += 1


class MasteryState:
    """All concept masteries of one learner."""

    def __init__(self):
        self.concepts: Dict[str, ConceptMastery] = {}

    def get(self, code: str) -> ConceptMastery:
        if code not in self.concepts:
            self.concepts[code] = ConceptMastery()
        return self.concepts[code]

    def update(self, tags: Dict[str, float], correct: bool) -> None:
        for code, weight in tags.items():
            self.get(code).update(correct, weight)

    def mastery_or_prior(self, code: str) -> float:
        m = self.concepts.get(code)
        return m.mastery if m else 0.5

    def weakest(self) -> Optional[str]:
        trusted = [(m.mastery, c) for c, m in self.concepts.items()
                   if m.evidence >= MIN_EVIDENCE and m.mastery < WEAK_THRESHOLD]
        return min(trusted)[1] if trusted else None


def logit(p: float) -> float:
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))

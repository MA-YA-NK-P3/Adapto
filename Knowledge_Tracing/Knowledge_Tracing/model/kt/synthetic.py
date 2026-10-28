"""Synthetic learner simulator.

    *** SYNTHETIC / DEVELOPMENT DATA — NOT EVIDENCE OF REAL LEARNING OUTCOMES ***

Generative assumptions (deliberately richer than the estimators, so they can be wrong):
  * each problem has a TRUE difficulty = static prior (EASY 3 / MEDIUM 5 / HARD 7) + N(0, 0.9)
    -> static labels are informative but imperfect;
  * each learner has a global ability and a per-concept skill = ability + N(0, 0.6);
    one designated weak concept gets an extra -2.5 (a concept-specific gap);
  * if a prerequisite of a tagged concept is weak (skill < ability - 1.5) the effective
    skill is reduced by 1.0 (prerequisite gap propagation);
  * P(correct) = sigmoid(effective_skill - true_difficulty);
  * practice changes skill: each attempt adds lr * weight * 4p(1-p) (largest in the
    productive-struggle zone p~0.5), +50% on success; prerequisites gain a fraction.
The learning dynamics are an ASSUMPTION; simulated gains only show how policies behave
under that assumption.
"""
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .catalog import Catalog
from .irt import STATIC_PRIOR, clamp, sigmoid

DIFFICULTY_NOISE_SD = 0.9
CONCEPT_NOISE_SD = 0.6
WEAK_CONCEPT_PENALTY = 2.5
PREREQ_WEAK_MARGIN = 1.5
PREREQ_PENALTY = 1.0
LEARNING_RATE = 0.35
PREREQ_TRANSFER = 0.3


@dataclass
class SyntheticLearner:
    learner_id: str
    ability: float
    skill: Dict[str, float]
    weak_concept: Optional[str]
    initial_skill: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not self.initial_skill:
            self.initial_skill = dict(self.skill)


class SyntheticWorld:
    def __init__(self, catalog: Catalog, seed: int = 7):
        self.catalog = catalog
        self.rng = random.Random(seed)
        self.true_difficulty: Dict[str, float] = {
            pid: clamp(STATIC_PRIOR[label] + self.rng.gauss(0, DIFFICULTY_NOISE_SD))
            for pid, label in catalog.labels.items()
        }

    # ---- population -------------------------------------------------------------
    def make_learner(self, learner_id: str, ability: float, weak_concept: Optional[str] = "random") -> SyntheticLearner:
        concepts = self.catalog.concepts
        if weak_concept == "random":
            weak_concept = self.rng.choice(concepts)
        skill = {c: ability + self.rng.gauss(0, CONCEPT_NOISE_SD) for c in concepts}
        if weak_concept:
            skill[weak_concept] -= WEAK_CONCEPT_PENALTY
        return SyntheticLearner(learner_id, ability, skill, weak_concept)

    def population(self, n: int, abilities=(2.0, 5.0, 8.0), spread: float = 0.8, prefix: str = "SYN") -> List[SyntheticLearner]:
        out = []
        for i in range(n):
            centre = abilities[i % len(abilities)]
            out.append(self.make_learner(f"{prefix}-{i:04d}", clamp(centre + self.rng.gauss(0, spread))))
        return out

    # ---- response model ---------------------------------------------------------
    def effective_skill(self, learner: SyntheticLearner, pid: str) -> float:
        tags = self.catalog.tags[pid]
        total = sum(tags.values())
        eff = sum(learner.skill[c] * w for c, w in tags.items()) / total
        for c in tags:
            if any(learner.skill[p] < learner.ability - PREREQ_WEAK_MARGIN
                   for p in self.catalog.prerequisites.get(c, [])):
                eff -= PREREQ_PENALTY
                break
        return eff

    def p_correct(self, learner: SyntheticLearner, pid: str) -> float:
        return sigmoid(self.effective_skill(learner, pid) - self.true_difficulty[pid])

    def attempt(self, learner: SyntheticLearner, pid: str, learn: bool = True) -> dict:
        p = self.p_correct(learner, pid)
        correct = self.rng.random() < p
        if learn:
            gain = LEARNING_RATE * 4 * p * (1 - p) * (1.5 if correct else 1.0)
            for c, w in self.catalog.tags[pid].items():
                learner.skill[c] = min(10.0, learner.skill[c] + gain * w)
                for pre in self.catalog.prerequisites.get(c, []):
                    learner.skill[pre] = min(10.0, learner.skill[pre] + gain * w * PREREQ_TRANSFER)
        minutes = max(1.0, self.rng.lognormvariate(2.6, 0.4) * (1.6 - p))
        failed_result = "TIME_LIMIT_EXCEEDED" if self.rng.random() < 0.2 else "WRONG_ANSWER"
        return {
            "problemId": pid,
            "correct": correct,
            "result": "ACCEPTED" if correct else failed_result,
            "timeSpentSeconds": int(minutes * 60),
            "attemptNumber": 1 if correct else 1 + int(self.rng.random() * 3),
            "trueP": p,
        }

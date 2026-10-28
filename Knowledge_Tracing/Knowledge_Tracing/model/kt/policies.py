"""Next-problem policies for offline simulation: baselines + the full system with ablation flags.

`SystemPolicy` is a reference re-implementation of the Java decision path
(CandidateGenerationService + DeterministicDecisionLayer + KnowledgeGapService) so that
ablations can be run offline over thousands of synthetic learners. Policies only see
OBSERVED responses, never the simulator's true skills.
"""
import random
from typing import Dict, List, Optional, Set

from .catalog import Catalog
from .irt import STATIC_PRIOR, estimate_ability, p_success
from .mastery import WEAK_THRESHOLD, MasteryState

TARGET_P = 0.65
CALIBRATED_UNCERTAINTY = 0.9
CALIBRATED_EVENTS = 8


class Observed:
    """What a policy is allowed to know about a learner."""

    def __init__(self, catalog: Catalog, difficulty: Dict[str, float]):
        self.catalog = catalog
        self.difficulty = difficulty
        self.history: List[tuple] = []
        self.first_attempts: List[tuple] = []
        self.attempted: Set[str] = set()
        self.solved: Set[str] = set()
        self.mastery = MasteryState()
        self.ability, self.uncertainty = 5.0, 2.0

    def record(self, pid: str, correct: bool) -> None:
        first = pid not in self.attempted
        self.history.append((pid, correct))
        self.attempted.add(pid)
        if correct:
            self.solved.add(pid)
        self.mastery.update(self.catalog.tags[pid], correct)
        if first:  # backend irt.first-attempts-only
            self.first_attempts.append((self.difficulty[pid], correct))
            self.ability, self.uncertainty = estimate_ability(self.first_attempts)

    def unattempted(self) -> List[str]:
        return [p for p in self.catalog.labels if p not in self.attempted]

    def unsolved(self) -> List[str]:
        return [p for p in self.catalog.labels if p not in self.solved]


class Policy:
    name = "policy"

    def choose(self, obs: Observed, rng: random.Random) -> Optional[str]:
        raise NotImplementedError


class RandomPolicy(Policy):
    name = "random"

    def choose(self, obs, rng):
        pool = obs.unsolved()
        return rng.choice(pool) if pool else None


class StaticLadderPolicy(Policy):
    """Baseline 1: static difficulty — EASY -> MEDIUM -> HARD, ignoring the learner."""
    name = "static-difficulty"

    def choose(self, obs, rng):
        pool = obs.unattempted() or obs.unsolved()
        if not pool:
            return None
        lowest = min(STATIC_PRIOR[obs.catalog.labels[p]] for p in pool)
        return rng.choice([p for p in pool if STATIC_PRIOR[obs.catalog.labels[p]] == lowest])


class SuccessRatePolicy(Policy):
    """Baseline 2: move up a static tier after >70% recent success, down after <40%."""
    name = "success-rate"
    tiers = ["EASY", "MEDIUM", "HARD"]

    def choose(self, obs, rng):
        recent = [y for _, y in obs.history[-5:]]
        tier = 0
        if recent:
            rate = sum(recent) / len(recent)
            last = obs.catalog.labels[obs.history[-1][0]]
            tier = self.tiers.index(last)
            if rate > 0.7:
                tier = min(2, tier + 1)
            elif rate < 0.4:
                tier = max(0, tier - 1)
        pool = obs.unattempted() or obs.unsolved()
        if not pool:
            return None
        for offset in (0, -1, 1, -2, 2):
            t = tier + offset
            if 0 <= t < 3:
                cands = [p for p in pool if obs.catalog.labels[p] == self.tiers[t]]
                if cands:
                    return rng.choice(cands)
        return None


class SystemPolicy(Policy):
    """Knowledge Tracer decision path with ablation flags.

    use_mastery=False, use_graph=False, use_localization=False  ->  "IRT only".
    """

    def __init__(self, name="full-system", use_mastery=True, use_graph=True, use_localization=True,
                 max_item_failures: Optional[int] = None):
        self.name = name
        self.use_mastery = use_mastery
        self.use_graph = use_graph
        self.use_localization = use_localization
        self.max_item_failures = max_item_failures

    # --- helpers mirroring CandidateGenerationService --------------------------
    def _remediation(self, obs: Observed, concept: str, target_difficulty: float) -> Optional[str]:
        cands = [p for p in obs.catalog.problems_for(concept) if p not in obs.solved]
        if self.max_item_failures is not None:
            cands = [p for p in cands
                     if sum(1 for q, y in obs.history if q == p and not y) < self.max_item_failures]
        if not cands:
            return None
        return min(cands, key=lambda p: (p in obs.attempted,
                                         max(0.0, obs.difficulty[p] - target_difficulty),
                                         obs.difficulty[p]))

    def _weak_prerequisite(self, obs: Observed, concept: str) -> Optional[str]:
        worst = None
        for pre in obs.catalog.all_prerequisites(concept):
            m = obs.mastery.concepts.get(pre)
            if m and m.evidence >= 1 and m.mastery < WEAK_THRESHOLD:
                if worst is None or m.mastery < obs.mastery.concepts[worst].mastery:
                    worst = pre
        return worst

    def _calibrated(self, obs: Observed) -> bool:
        return obs.uncertainty <= CALIBRATED_UNCERTAINTY or len(obs.history) >= CALIBRATED_EVENTS

    def choose(self, obs, rng):
        theta = obs.ability
        if self.use_mastery:
            weak = obs.mastery.weakest()
            if weak:
                if self.use_graph:
                    pre = self._weak_prerequisite(obs, weak)
                    if pre:
                        pid = self._remediation(obs, pre, theta - 1.0)
                        if pid:
                            return pid
                pid = self._remediation(obs, weak, theta - 1.5)
                if pid:
                    return pid
        pool = obs.unattempted()
        if self.use_localization and not self._calibrated(obs) and pool:
            return min(pool, key=lambda p: abs(obs.difficulty[p] - theta))
        if pool:
            return min(pool, key=lambda p: abs(p_success(theta, obs.difficulty[p]) - TARGET_P))
        failed = [p for p, y in reversed(obs.history) if not y and p not in obs.solved]
        return failed[0] if failed else None


def all_policies() -> List[Policy]:
    return [
        RandomPolicy(),
        StaticLadderPolicy(),
        SuccessRatePolicy(),
        SystemPolicy("irt-only", use_mastery=False, use_graph=False, use_localization=False),
        SystemPolicy("irt+localization", use_mastery=False, use_graph=False, use_localization=True),
        SystemPolicy("irt+mastery", use_mastery=True, use_graph=False, use_localization=True),
        SystemPolicy("full-system", use_mastery=True, use_graph=True, use_localization=True),
        SystemPolicy("full+retry-guard", use_mastery=True, use_graph=True, use_localization=True,
                     max_item_failures=1),
    ]

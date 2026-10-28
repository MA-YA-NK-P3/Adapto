"""Offline recommendation loop: backend decision path + Jev providers on simulated learners.

Mirrors (simplified where noted) the Java pipeline per request:
  KnowledgeGapService (concept / prerequisite gap from Beta mastery)  ->
  CandidateGenerationService (calibration, prerequisite review, easier, same level,
  harder after a streak, retry last failure, spaced review)  ->  provider decision  ->
  learner attempts the problem (kt.simulator.World ground truth)  ->  reward
  (OutcomeRewardService formula)  ->  bandit update.
Simplifications: success estimates are IRT only (no DKT blend), item difficulties are the
catalog priors (no online recalibration), no hints/solutions in the loop, time/TLE gaps
are not modelled.
"""
import math
import random
from typing import Dict, List, Optional

from kt.irt import estimate_ability, information, p_success
from kt.mastery import WEAK_THRESHOLD, MasteryState
from kt.simulator import World

from .linucb import LinUcb, features

TARGET_P = 0.65
CALIBRATED_UNCERTAINTY = 0.9
CALIBRATED_EVENTS = 8
STRONG_TAG_WEIGHT = 0.7
RECENT_WINDOW = 10
QUALITY_WEIGHT = 0.0  # backend default (growth-only reward), see jev-simulation.md
GROWTH_SCALE = 0.5


def reward(correct: bool, ability_before: float, ability_after: float, hint=False, solution=False) -> float:
    quality = 0.0 if not correct else 0.3 if solution else 0.6 if hint else 1.0
    delta = max(-1.0, min(1.0, (ability_after - ability_before) / GROWTH_SCALE))
    return QUALITY_WEIGHT * quality + (1 - QUALITY_WEIGHT) * (delta + 1) / 2


class Observed:
    """What the backend knows about one learner."""

    def __init__(self, catalog, difficulty: Dict[str, float]):
        self.catalog = catalog
        self.difficulty = difficulty
        self.history: List[tuple] = []
        self.first_attempts: List[tuple] = []
        self.attempted, self.solved = set(), set()
        self.mastery = MasteryState()
        self.ability, self.uncertainty = 5.0, 2.0

    @property
    def calibrated(self) -> bool:
        return self.uncertainty <= CALIBRATED_UNCERTAINTY or len(self.history) >= CALIBRATED_EVENTS

    def record(self, pid: str, correct: bool) -> None:
        first = pid not in self.attempted
        self.history.append((pid, correct))
        self.attempted.add(pid)
        if correct:
            self.solved.add(pid)
        self.mastery.update(self.catalog.tags[pid], correct)
        if first:  # backend irt.first-attempts-only: one Rasch response per problem
            self.first_attempts.append((self.difficulty[pid], correct))
            self.ability, self.uncertainty = estimate_ability(self.first_attempts[-100:])


def detect_gap(obs: Observed) -> dict:
    weak = obs.mastery.weakest()
    if weak is None:
        return {"type": "NONE" if len(obs.history) >= 3 else "INSUFFICIENT_PRACTICE", "confidence": 0.6}
    m = obs.mastery.concepts[weak]
    worst = None
    for pre in obs.catalog.all_prerequisites(weak):
        pm = obs.mastery.concepts.get(pre)
        if pm and pm.evidence >= 1 and pm.mastery < WEAK_THRESHOLD:
            if worst is None or pm.mastery < obs.mastery.concepts[worst].mastery:
                worst = pre
    if worst:
        ev = m.evidence + obs.mastery.concepts[worst].evidence
        return {"type": "PREREQUISITE_GAP", "concept": weak, "prerequisite": worst,
                "confidence": min(0.9, 0.5 + 0.08 * ev)}
    return {"type": "CONCEPT_GAP", "concept": weak, "confidence": min(0.9, 0.5 + 0.08 * m.evidence)}


class CandidateGenerator:
    def __init__(self, catalog, difficulty: Dict[str, float]):
        self.catalog = catalog
        self.difficulty = difficulty
        self.order = [p["problemId"] for p in catalog.problems]
        self.by_concept: Dict[str, List[tuple]] = {}
        for pid, tags in catalog.tags.items():
            for c, w in tags.items():
                self.by_concept.setdefault(c, []).append((pid, w))

    def _remediation(self, obs, concept, target) -> Optional[str]:
        cands = [(pid, w) for pid, w in self.by_concept.get(concept, []) if pid not in obs.solved]
        if not cands:
            return None
        return min(cands, key=lambda t: (t[1] < STRONG_TAG_WEIGHT, t[0] in obs.attempted,
                                         max(0.0, self.difficulty[t[0]] - target), self.difficulty[t[0]]))[0]

    def _cand(self, action, pid, concept, ev, obs):
        d = self.difficulty[pid]
        return {"action": action, "problemKey": pid, "conceptCode": concept, "difficulty": d,
                "expectedValue": ev, "predictedSuccess": p_success(obs.ability, d)}

    def generate(self, obs: Observed, gap: dict) -> List[dict]:
        theta, out = obs.ability, []
        unattempted = [p for p in self.order if p not in obs.attempted]
        recent = obs.history[-RECENT_WINDOW:][::-1]
        if not obs.calibrated:
            for pid in sorted(unattempted, key=lambda p: abs(self.difficulty[p] - theta))[:2]:
                out.append(self._cand("CALIBRATION_PROBLEM", pid, None,
                                      0.5 + information(theta, self.difficulty[pid]), obs))
        if gap["type"] == "PREREQUISITE_GAP":
            pid = self._remediation(obs, gap["prerequisite"], theta - 1.0)
            if pid:
                out.append(self._cand("PREREQUISITE_REVIEW", pid, gap["prerequisite"], 0.8, obs))
        if gap["type"] in ("CONCEPT_GAP", "PREREQUISITE_GAP"):
            pid = self._remediation(obs, gap["concept"], theta - 1.5)
            if pid:
                out.append(self._cand("EASIER_PROBLEM", pid, gap["concept"], 0.75, obs))
        if unattempted:
            pid = min(unattempted, key=lambda p: (abs(p_success(theta, self.difficulty[p]) - TARGET_P), p))
            p = p_success(theta, self.difficulty[pid])
            out.append(self._cand("SAME_LEVEL_PROBLEM", pid, None, 1.0 - abs(p - TARGET_P), obs))
        streak = 0
        for _, y in recent:
            if not y:
                break
            streak += 1
        if streak >= 3:
            harder = [p for p in unattempted if theta < self.difficulty[p] <= theta + 2.0]
            if harder:
                out.append(self._cand("HARDER_PROBLEM", min(harder, key=lambda p: self.difficulty[p]),
                                      None, 0.55, obs))
        failed = next((p for p, y in recent if not y), None)
        if failed:
            out.append(self._cand("RETRY_PROBLEM", failed, None, 0.5, obs))
        seen = {p for p, _ in recent}
        review = next((p for p in self.order if p in obs.solved and p not in seen), None)
        if review:
            out.append(self._cand("SPACED_REVIEW", review, None, 0.4, obs))
        return out


# ------------------------------------------------------------------------------ providers
class Deterministic:
    """DeterministicDecisionLayer."""
    name = "deterministic"

    def choose(self, cands, ctx, rng):
        gap = ctx["gap"]["type"]
        if gap == "PREREQUISITE_GAP":
            for c in cands:
                if c["action"] == "PREREQUISITE_REVIEW":
                    return c
        if gap in ("CONCEPT_GAP", "PREREQUISITE_GAP"):
            for c in cands:
                if c["action"] == "EASIER_PROBLEM":
                    return c
        if not ctx["calibrated"]:
            cal = [c for c in cands if c["action"] == "CALIBRATION_PROBLEM"]
            if cal:
                return max(cal, key=lambda c: c["expectedValue"])
        return max(cands, key=lambda c: c["expectedValue"])

    def learn(self, cand, ctx, r):
        pass


class Bandit:
    """BanditDecisionLayer (LinUCB, optionally guarded)."""

    def __init__(self, alpha=0.5, lam=1.0, guarded=True):
        self.name = f"linucb{'-guarded' if guarded else ''}(a={alpha})"
        self.guarded = guarded
        self.model = LinUcb(alpha, lam)
        self.rules = Deterministic()

    def choose(self, cands, ctx, rng):
        if self.guarded and ctx["gap"]["type"] in ("CONCEPT_GAP", "PREREQUISITE_GAP") and any(
                c["action"] in ("PREREQUISITE_REVIEW", "EASIER_PROBLEM") for c in cands):
            return self.rules.choose(cands, ctx, rng)
        return self.model.choose(cands, ctx)[0]

    def learn(self, cand, ctx, r):
        self.model.update(cand["action"], features(cand, ctx), r)


class RandomCandidate:
    name = "random-candidate"

    def choose(self, cands, ctx, rng):
        return rng.choice(cands)

    def learn(self, cand, ctx, r):
        pass


# ------------------------------------------------------------------------------ simulation
def run(world: World, provider, n_learners: int, steps: int, seed: int, prefix: str = "JEV") -> dict:
    """Every provider sees the SAME learners (same seeds) so differences are due to decisions."""
    catalog = world.catalog
    difficulty = {pid: world.prior[pid] for pid in world.problems}
    gen = CandidateGenerator(catalog, difficulty)
    rng = random.Random(seed)
    curve, actions, learners_out = [], {}, []
    for i in range(n_learners):
        world.rng = random.Random(seed * 100003 + i)
        learner = world.make_learner(f"{prefix}-{i:05d}")
        obs = Observed(catalog, difficulty)
        t = 0.0
        start = learner.mastery(t)
        solved_first, in_zone = 0, 0
        for _ in range(steps):
            gap = detect_gap(obs)
            cands = gen.generate(obs, gap)
            if not cands:
                break
            ctx = {"ability": obs.ability, "uncertainty": obs.uncertainty,
                   "calibrated": obs.calibrated, "gap": gap}
            choice = provider.choose(cands, ctx, rng)
            pid = choice["problemKey"]
            attempt = 1 + sum(1 for q, _ in obs.history if q == pid)
            p_true = world.p_correct(learner, pid, t, attempt)
            correct = world.rng.random() < p_true
            world.learn(learner, pid, t, p_true, correct, False)
            before = obs.ability
            obs.record(pid, correct)
            r = reward(correct, before, obs.ability)
            provider.learn(choice, ctx, r)
            curve.append(r)
            actions[choice["action"]] = actions.get(choice["action"], 0) + 1
            solved_first += int(correct and attempt == 1)
            in_zone += int(0.5 <= p_true <= 0.8)
            t += 0.35 + world.rng.expovariate(1 / 0.5)
        end = learner.mastery(t)
        weak = learner.weak
        learners_out.append({
            "gain": sum(end[c] - start[c] for c in start) / len(start),
            "weakGain": sum(end[c] - start[c] for c in weak) / len(weak),
            "solvedFirst": solved_first / steps, "inZone": in_zone / steps,
        })
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
    n = len(learners_out)

    def ci(key):
        xs = [l[key] for l in learners_out]
        m = mean(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, n - 1))
        return {"mean": round(m, 5), "ci95": round(1.96 * sd / math.sqrt(n), 5)}

    return {"provider": provider.name, "learners": n, "steps": steps,
            "meanReward": round(mean(curve), 5),
            "trueMasteryGain": ci("gain"), "weakConceptGain": ci("weakGain"),
            "firstAttemptSolveRate": ci("solvedFirst"), "productiveZoneRate": ci("inZone"),
            "actions": dict(sorted(actions.items())), "rewardCurve": curve}

"""Synthetic learner simulator v2 over the 1000-problem LeetCode catalog.

    *** SYNTHETIC / DEVELOPMENT DATA — NOT EVIDENCE OF REAL LEARNING OUTCOMES ***

Every quantity below is an explicit, documented ASSUMPTION (docs/data.md). The generative
model is deliberately richer than any estimator in this repository (IRT, Beta mastery,
DKT), so each estimator can be measured against a known ground truth and can be wrong.

Latent state (logit scale, per learner i and concept k)
-------------------------------------------------------
  cohort c_i          beginner 35% (mu=-1.0) | intermediate 45% (0.0) | advanced 20% (+1.0)
  general ability g_i ~ N(mu_c, 0.5)
  base skill      s0_ik = g_i + 0.55 * eps_ik,  eps ~ N(0,1)
  weak concepts   1-3 per learner (P = .50/.35/.15), s0_ik -= 1.5
  prerequisite    in topological order: s0_ik += 0.4 * min(0, mean_p(s0_ip) - g_i)
  coupling          (a weak prerequisite drags its dependents down, never up)
  learned part    L_ik >= 0, decays: L_ik(t) = L_ik * exp(-(t - T_ik) / tau_ik)
  skill           s_ik(t) = s0_ik + L_ik(t)
  memory          tau_ik starts at tau_i ~ LogNormal(log 45 days, 0.5) and grows x1.15
                    on every practice of k (spacing effect)
  learning rate   eta_i ~ LogNormal(0, 0.3)

Items
-----
  true difficulty b_j = (priorDifficulty_j - 5) / 1.25 + N(0, 0.4)
    -> the acceptance-adjusted catalog prior is informative but noisy, so item
       calibration (IRT) has something real to recover.
  concept weights w_jk from the catalog (normalised to sum 1 inside the response model)

Response model (one submission)
-------------------------------
  z = 0.75 * sum_k w~_jk s_ik + 0.25 * min_k s_ik - b_j + bonus
      (conjunctive "weakest link" term: a single missing concept hurts)
  bonus = 0.45 * (attempt - 1)   debugging on retries
        + 0.60 * hint            a hint was revealed before this submission
        + 1.50 * solution        the editorial/solution was viewed
  P(correct) = 0.02 + (1 - 0.02 - 0.04) * sigmoid(z)     guess .02, slip .04

Learning (after every submission on j, for each tagged k)
---------------------------------------------------------
  gain = eta_i * 0.10 * w~_jk * (0.5 + 2 p (1-p)) * (1.3 if correct else 1.0)
         * (0.5 if solution viewed else 1.0) * sigmoid(3 - s_ik)
  L_ik += gain;  each direct prerequisite of k receives 0.3 * gain.
  ("productive struggle": gains peak at p = 0.5; diminishing returns at high skill)

Behaviour
---------
  sessions/learner   round(LogNormal(log 10, 0.7)) clipped to [2, 80]
  problems/session   1 + Poisson(2)
  inter-session gap  Exp(mean 2.5 days * LogNormal(0, 0.5) learner factor)
  problem choice     per-learner mixture of focus (study plan over the concept DAG,
                     easiest-unsolved first), comfort (true p in [0.55, 0.9]),
                     popular (weighted by likes) and review (re-attempt a failure)
  retry              after a failure with P = 0.6 * persistence_i (Beta(4,2)), max 5
  hint               (problems with hints only) on first look when true p < 0.5 with
                     P = 0.5 * hint_propensity_i (Beta(2,5)); before a retry with P = hint_propensity_i
  solution           after >= 2 failures with P = min(1, 2 * solution_propensity_i) (Beta(1.5,6))
  abandon            first look with true p < 0.25: ABANDONED with P = 0.25
  failure type       TLE with P = 0.12 + 0.25 sigmoid(b_j); RUNTIME_ERROR 0.12; else WA
  time spent         first attempt LogNormal(log 14 min + 0.35 b_j - 0.25 skill, 0.45),
                     retries LogNormal(log 6 min, 0.5)

Output: kt-events-v1 records (model/kt/events.py) plus a `truth` block per event and
per-learner ground truth (initial/final mastery = sigmoid(s_ik)).
"""
import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .catalog import Catalog

COHORTS = (("beginner", 0.35, -1.0), ("intermediate", 0.45, 0.0), ("advanced", 0.20, 1.0))
ABILITY_SD = 0.5
CONCEPT_SD = 0.55
WEAK_COUNT_P = ((1, 0.50), (2, 0.35), (3, 0.15))
WEAK_PENALTY = 1.5
PREREQ_COUPLING = 0.4
DIFFICULTY_SCALE = 1.25
ITEM_NOISE_SD = 0.4
MEAN_WEIGHT, MIN_WEIGHT = 0.75, 0.25
RETRY_BONUS, HINT_BONUS, SOLUTION_BONUS = 0.45, 0.60, 1.50
GUESS, SLIP = 0.02, 0.04
BASE_LR = 0.10
PREREQ_TRANSFER = 0.3
TAU_DAYS = 45.0
SPACING_FACTOR = 1.15
MAX_ATTEMPTS = 5
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
START_SPREAD_DAYS = 180


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def topological_order(prereqs: Dict[str, List[str]]) -> List[str]:
    indeg = {c: 0 for c in prereqs}
    children: Dict[str, List[str]] = {c: [] for c in prereqs}
    for c, ps in prereqs.items():
        for p in ps:
            if p in prereqs:
                indeg[c] += 1
                children[p].append(c)
    queue = deque(sorted(c for c, d in indeg.items() if d == 0))
    out = []
    while queue:
        c = queue.popleft()
        out.append(c)
        for ch in sorted(children[c]):
            indeg[ch] -= 1
            if indeg[ch] == 0:
                queue.append(ch)
    if len(out) != len(prereqs):
        raise ValueError("prerequisite graph has a cycle")
    return out


@dataclass
class Learner:
    learner_id: str
    cohort: str
    ability: float
    base: Dict[str, float]
    weak: List[str]
    eta: float
    tau: Dict[str, float]
    persistence: float
    hint_propensity: float
    solution_propensity: float
    mode_weights: Dict[str, float]
    gap_factor: float
    tau_base: float
    learned: Dict[str, float] = field(default_factory=dict)
    last_practice: Dict[str, float] = field(default_factory=dict)  # days since EPOCH

    def skill(self, concept: str, t_days: float) -> float:
        learned = self.learned.get(concept, 0.0)
        if learned:
            learned *= math.exp(-(t_days - self.last_practice[concept]) / self.tau[concept])
        return self.base[concept] + learned

    def mastery(self, t_days: float) -> Dict[str, float]:
        return {c: sigmoid(self.skill(c, t_days)) for c in self.base}


class World:
    """Ground-truth world: items with true difficulty, learners with latent skills."""

    def __init__(self, catalog: Catalog, seed: int = 20260929):
        self.catalog = catalog
        self.rng = random.Random(seed)
        self.order = topological_order(catalog.prerequisites)
        self.problems = [p["problemId"] for p in catalog.problems]
        self.prior = {p["problemId"]: float(p.get("priorDifficulty", 5.0)) for p in catalog.problems}
        self.likes = {p["problemId"]: max(1, int(p.get("likes") or 1)) for p in catalog.problems}
        self.hint_count = {p["problemId"]: int(p.get("hintCount") or 0) for p in catalog.problems}
        self.weights: Dict[str, Dict[str, float]] = {}
        for pid, tags in catalog.tags.items():
            total = sum(tags.values())
            self.weights[pid] = {c: w / total for c, w in tags.items()}
        self.true_b = {pid: (self.prior[pid] - 5.0) / DIFFICULTY_SCALE + self.rng.gauss(0.0, ITEM_NOISE_SD)
                       for pid in self.problems}
        self.by_concept: Dict[str, List[str]] = {c: [] for c in catalog.concepts}
        for pid, tags in catalog.tags.items():
            for c, w in tags.items():
                if w >= 0.7:
                    self.by_concept[c].append(pid)
        for c in self.by_concept:
            self.by_concept[c].sort(key=lambda q: self.prior[q])

    # ------------------------------------------------------------------ population
    def make_learner(self, learner_id: str) -> Learner:
        r = self.rng
        u, acc = r.random(), 0.0
        cohort, mu = COHORTS[-1][0], COHORTS[-1][2]
        for name, share, m in COHORTS:
            acc += share
            if u < acc:
                cohort, mu = name, m
                break
        g = r.gauss(mu, ABILITY_SD)
        base = {c: g + CONCEPT_SD * r.gauss(0.0, 1.0) for c in self.catalog.concepts}
        u, acc, n_weak = r.random(), 0.0, 1
        for n, pr in WEAK_COUNT_P:
            acc += pr
            if u < acc:
                n_weak = n
                break
        weak = r.sample(self.catalog.concepts, n_weak)
        for c in weak:
            base[c] -= WEAK_PENALTY
        for c in self.order:
            pres = [p for p in self.catalog.prerequisites.get(c, []) if p in base]
            if pres:
                base[c] += PREREQ_COUPLING * min(0.0, sum(base[p] for p in pres) / len(pres) - g)
        tau_i = TAU_DAYS * r.lognormvariate(0.0, 0.5)
        modes = {"focus": 0.45, "comfort": 0.25, "popular": 0.20, "review": 0.10}
        noisy = {k: v * r.lognormvariate(0.0, 0.4) for k, v in modes.items()}
        s = sum(noisy.values())
        return Learner(
            learner_id=learner_id, cohort=cohort, ability=g, base=base, weak=sorted(weak),
            eta=r.lognormvariate(0.0, 0.3), tau={c: tau_i for c in base},
            persistence=r.betavariate(4, 2), hint_propensity=r.betavariate(2, 5),
            solution_propensity=r.betavariate(1.5, 6),
            mode_weights={k: v / s for k, v in noisy.items()},
            gap_factor=r.lognormvariate(0.0, 0.5), tau_base=tau_i)

    # ------------------------------------------------------------------ response model
    def logit_success(self, learner: Learner, pid: str, t: float) -> float:
        w = self.weights[pid]
        skills = {c: learner.skill(c, t) for c in w}
        mean = sum(skills[c] * wc for c, wc in w.items())
        return MEAN_WEIGHT * mean + MIN_WEIGHT * min(skills.values()) - self.true_b[pid]

    def p_correct(self, learner: Learner, pid: str, t: float, attempt: int = 1,
                  hint: bool = False, solution: bool = False) -> float:
        z = self.logit_success(learner, pid, t)
        z += RETRY_BONUS * (attempt - 1) + (HINT_BONUS if hint else 0.0) + (SOLUTION_BONUS if solution else 0.0)
        return GUESS + (1.0 - GUESS - SLIP) * sigmoid(z)

    def learn(self, learner: Learner, pid: str, t: float, p: float, correct: bool, solution: bool) -> None:
        for c, w in self.weights[pid].items():
            gain = (learner.eta * BASE_LR * w * (0.5 + 2.0 * p * (1.0 - p))
                    * (1.3 if correct else 1.0) * (0.5 if solution else 1.0)
                    * sigmoid(3.0 - learner.skill(c, t)))
            self._add(learner, c, t, gain)
            learner.tau[c] *= SPACING_FACTOR
            for pre in self.catalog.prerequisites.get(c, []):
                self._add(learner, pre, t, PREREQ_TRANSFER * gain)

    @staticmethod
    def _add(learner: Learner, concept: str, t: float, gain: float) -> None:
        current = learner.skill(concept, t) - learner.base[concept]
        learner.learned[concept] = current + gain
        learner.last_practice[concept] = t

    # ------------------------------------------------------------------ behaviour
    def choose_problem(self, learner: Learner, t: float, state: dict) -> Optional[str]:
        r = self.rng
        solved, failed = state["solved"], state["failed"]
        mode = self._weighted_choice(learner.mode_weights)
        if mode == "review" and failed:
            return r.choice(sorted(failed))
        if mode == "comfort":
            pool = [q for q in r.sample(self.problems, 60) if q not in solved]
            good = [q for q in pool if 0.55 <= self.p_correct(learner, q, t) <= 0.90]
            if good:
                return r.choice(good)
        if mode == "popular":
            pool = [q for q in r.sample(self.problems, 80) if q not in solved]
            if pool:
                return self._weighted_choice({q: self.likes[q] for q in pool})
        return self._focus_problem(learner, t, state)

    def _focus_problem(self, learner: Learner, t: float, state: dict) -> Optional[str]:
        r = self.rng
        focus = state.get("focus")
        if focus is None or state["focus_solved"] >= 4 or state["focus_attempts"] >= 8:
            focus = self._next_focus(learner, state)
            state.update(focus=focus, focus_solved=0, focus_attempts=0)
        candidates = [q for q in self.by_concept.get(focus, []) if q not in state["solved"]]
        if not candidates:
            state["focus"] = None
            candidates = [q for q in self.problems if q not in state["solved"]]
            if not candidates:
                return None
            return r.choice(candidates)
        return candidates[min(len(candidates) - 1, int(r.expovariate(1.0 / 1.5)))]

    def _next_focus(self, learner: Learner, state: dict) -> str:
        """Study plan: prefer concepts whose prerequisites were visited, in DAG order."""
        visited = state["visited_focus"]
        ready = [c for c in self.order if c not in visited and self.by_concept.get(c)
                 and all(p in visited for p in self.catalog.prerequisites.get(c, []))]
        pool = ready[:4] or [c for c in self.order if self.by_concept.get(c)]
        choice = self.rng.choice(pool)
        visited.add(choice)
        return choice

    def _weighted_choice(self, weights: Dict[str, float]) -> str:
        total = sum(weights.values())
        u, acc = self.rng.random() * total, 0.0
        for k, w in weights.items():
            acc += w
            if u < acc:
                return k
        return next(reversed(list(weights)))

    # ------------------------------------------------------------------ one learner
    def simulate_learner(self, learner: Learner) -> Tuple[List[dict], dict]:
        r = self.rng
        initial = learner.mastery(0.0)
        state = {"solved": set(), "failed": set(), "focus": None, "focus_solved": 0,
                 "focus_attempts": 0, "visited_focus": set()}
        n_sessions = int(min(80, max(2, round(r.lognormvariate(math.log(10), 0.7)))))
        t = r.random() * START_SPREAD_DAYS
        events: List[dict] = []
        attempts_by_problem: Dict[str, int] = {}
        for s in range(n_sessions):
            session_id = f"{learner.learner_id}-s{s:03d}"
            clock = t + (18 + 4 * r.random()) / 24.0
            for _ in range(1 + self._poisson(2.0)):
                pid = self.choose_problem(learner, clock, state)
                if pid is None:
                    break
                clock = self._attempt_problem(learner, pid, clock, session_id, state, attempts_by_problem, events)
            t += r.expovariate(1.0 / (2.5 * learner.gap_factor)) + 0.3
        truth = {
            "learnerId": learner.learner_id, "cohort": learner.cohort,
            "ability": round(learner.ability, 4), "eta": round(learner.eta, 4),
            "tauDays": round(learner.tau_base, 2),
            "weakConcepts": learner.weak, "persistence": round(learner.persistence, 3),
            "hintPropensity": round(learner.hint_propensity, 3),
            "solutionPropensity": round(learner.solution_propensity, 3),
            "modeWeights": {k: round(v, 3) for k, v in learner.mode_weights.items()},
            "initialMastery": {c: round(v, 4) for c, v in initial.items()},
            "finalMastery": {c: round(v, 4) for c, v in learner.mastery(t).items()},
            "events": len(events),
        }
        return events, truth

    def _attempt_problem(self, learner, pid, clock, session_id, state, attempts_by_problem, events) -> float:
        r = self.rng
        hint = solution = False
        failures = 0
        in_focus = state.get("focus") in self.weights[pid]
        for local_attempt in range(1, MAX_ATTEMPTS + 1):
            attempt_number = attempts_by_problem.get(pid, 0) + 1
            p_first = self.p_correct(learner, pid, clock)
            if local_attempt == 1 and p_first < 0.25 and r.random() < 0.25:
                minutes = r.lognormvariate(math.log(6.0), 0.6)
                attempts_by_problem[pid] = attempt_number
                events.append(self._event(learner, pid, clock, session_id, attempt_number, "ABANDONED",
                                          minutes, False, False, p_first))
                state["failed"].add(pid)
                return clock + minutes / 1440.0
            if (local_attempt == 1 and self.hint_count[pid] > 0 and p_first < 0.5
                    and r.random() < 0.5 * learner.hint_propensity):
                hint = True
            p = self.p_correct(learner, pid, clock, local_attempt, hint, solution)
            correct = r.random() < p
            if local_attempt == 1:
                skill = sum(learner.skill(c, clock) * w for c, w in self.weights[pid].items())
                minutes = r.lognormvariate(math.log(14.0) + 0.35 * self.true_b[pid] - 0.25 * skill, 0.45)
            else:
                minutes = r.lognormvariate(math.log(6.0), 0.5)
            minutes = min(240.0, max(0.5, minutes))
            result = "ACCEPTED" if correct else self._failure_type(pid)
            attempts_by_problem[pid] = attempt_number
            events.append(self._event(learner, pid, clock, session_id, attempt_number, result,
                                      minutes, hint, solution, p))
            self.learn(learner, pid, clock, p, correct, solution)
            clock += minutes / 1440.0
            if in_focus:
                state["focus_attempts"] += 1
            if correct:
                state["solved"].add(pid)
                state["failed"].discard(pid)
                if in_focus:
                    state["focus_solved"] += 1
                return clock
            failures += 1
            state["failed"].add(pid)
            if r.random() >= 0.6 * learner.persistence:
                return clock
            if not hint and self.hint_count[pid] > 0 and r.random() < learner.hint_propensity:
                hint = True
            if not solution and failures >= 2 and r.random() < min(1.0, 2.0 * learner.solution_propensity):
                solution = True
        return clock

    def _failure_type(self, pid: str) -> str:
        u = self.rng.random()
        p_tle = 0.12 + 0.25 * sigmoid(self.true_b[pid])
        if u < p_tle:
            return "TIME_LIMIT_EXCEEDED"
        if u < p_tle + 0.12:
            return "RUNTIME_ERROR"
        return "WRONG_ANSWER"

    def _event(self, learner, pid, clock, session_id, attempt, result, minutes, hint, solution, p) -> dict:
        ts = EPOCH + timedelta(days=clock)
        return {
            "learnerId": learner.learner_id,
            "problemId": pid,
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessionId": session_id,
            "attemptNumber": attempt,
            "result": result,
            "correct": 1 if result == "ACCEPTED" else 0,
            "timeSpentSeconds": int(round(minutes * 60)),
            "hintUsed": hint,
            "solutionViewed": solution,
            "truth": {
                "pCorrect": round(p, 4),
                "mastery": {c: round(sigmoid(learner.skill(c, clock)), 4) for c in self.weights[pid]},
            },
        }

    def _poisson(self, lam: float) -> int:
        limit, k, prod = math.exp(-lam), 0, self.rng.random()
        while prod > limit:
            k += 1
            prod *= self.rng.random()
        return k

    # ------------------------------------------------------------------ dataset
    def generate(self, n_learners: int, prefix: str = "SYN"):
        """Yields (events, learner_truth) per learner, deterministically for a seed."""
        for i in range(n_learners):
            learner = self.make_learner(f"{prefix}-{i:05d}")
            yield self.simulate_learner(learner)

    def item_truth(self) -> List[dict]:
        return [{"problemId": pid, "priorDifficulty": self.prior[pid],
                 "trueDifficultyLogit": round(self.true_b[pid], 4),
                 "trueDifficulty0to10": round(5.0 + DIFFICULTY_SCALE * self.true_b[pid], 3)}
                for pid in self.problems]

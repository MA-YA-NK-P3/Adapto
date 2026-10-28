"""Disjoint LinUCB + bandit features — mirror of backend decision/bandit (LinUcb, BanditFeatures).

Per arm (one per action type):  A = lambda*I + sum x x^T,  b = lambda*theta0 + sum r x
    theta = A^-1 b,  mean = theta.x,  width = sqrt(x^T A^-1 x),  ucb = mean + alpha*width
theta0 puts weight 1 on the heuristic expectedValue feature (warm start).
"""
import math
from typing import Dict, List, Optional, Sequence

FEATURE_NAMES = ["bias", "predictedSuccess", "information", "relativeDifficulty",
                 "abilityUncertainty", "calibrated", "gapMatch", "expectedValue"]
DIM = len(FEATURE_NAMES)
THETA0 = [0.0] * (DIM - 1) + [1.0]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def features(candidate: dict, ctx: dict) -> List[float]:
    """candidate: action, conceptCode, difficulty, predictedSuccess, expectedValue.
    ctx: ability, uncertainty, calibrated, gap {type, concept, prerequisite, confidence}."""
    p = candidate.get("predictedSuccess")
    p = 0.5 if p is None else p
    d = candidate.get("difficulty")
    rel = _clamp((d - ctx["ability"]) / 3.0, -1, 1) if d is not None else 0.0
    gap = ctx.get("gap") or {}
    code = candidate.get("conceptCode")
    match = code is not None and code in (gap.get("concept"), gap.get("prerequisite"))
    return [1.0, p, 4.0 * p * (1.0 - p), rel, _clamp(ctx["uncertainty"] / 2.0, 0, 1),
            1.0 if ctx["calibrated"] else 0.0, _clamp(gap.get("confidence", 0.0), 0, 1) if match else 0.0,
            _clamp(candidate["expectedValue"], 0, 1.5)]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def cholesky(a: List[List[float]]) -> List[List[float]]:
    n = len(a)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = a[i][j] - sum(low[i][k] * low[j][k] for k in range(j))
            if i == j:
                if s <= 0:
                    raise ValueError("matrix not positive definite")
                low[i][i] = math.sqrt(s)
            else:
                low[i][j] = s / low[j][j]
    return low


def solve(low: List[List[float]], y: Sequence[float]) -> List[float]:
    n = len(y)
    z = [0.0] * n
    for i in range(n):
        z[i] = (y[i] - sum(low[i][k] * z[k] for k in range(i))) / low[i][i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (z[i] - sum(low[k][i] * x[k] for k in range(i + 1, n))) / low[i][i]
    return x


class Arm:
    def __init__(self, lam: float = 1.0, theta0: Sequence[float] = THETA0):
        n = len(theta0)
        self.a = [[lam if i == j else 0.0 for j in range(n)] for i in range(n)]
        self.b = [lam * t for t in theta0]
        self.pulls = 0
        self.reward_sum = 0.0
        self._low: Optional[List[List[float]]] = None

    def update(self, x: Sequence[float], reward: float) -> None:
        for i, xi in enumerate(x):
            row = self.a[i]
            for j, xj in enumerate(x):
                row[j] += xi * xj
            self.b[i] += reward * xi
        self.pulls += 1
        self.reward_sum += reward
        self._low = None

    def _chol(self):
        if self._low is None:
            self._low = cholesky(self.a)
        return self._low

    def theta(self) -> List[float]:
        return solve(self._chol(), self.b)

    def score(self, x: Sequence[float], alpha: float):
        low = self._chol()
        mean = dot(solve(low, self.b), x)
        width = math.sqrt(max(0.0, dot(x, solve(low, x))))
        return mean, width, mean + alpha * width


class LinUcb:
    """Population-level bandit: arms shared by all learners, as in the backend (jev_bandit_arms)."""

    def __init__(self, alpha: float = 0.5, lam: float = 1.0):
        self.alpha = alpha
        self.lam = lam
        self.arms: Dict[str, Arm] = {}

    def arm(self, action: str) -> Arm:
        if action not in self.arms:
            self.arms[action] = Arm(self.lam)
        return self.arms[action]

    def choose(self, candidates: List[dict], ctx: dict):
        best, best_score = None, None
        for c in candidates:
            s = self.arm(c["action"]).score(features(c, ctx), self.alpha)
            if best_score is None or s[2] > best_score[2]:
                best, best_score = c, s
        confidence = _clamp(1.0 / (1.0 + best_score[1]), 0.35, 0.95)
        return best, confidence

    def update(self, action: str, x: Sequence[float], reward: float) -> None:
        self.arm(action).update(x, reward)

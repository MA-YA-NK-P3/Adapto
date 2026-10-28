"""Evaluation metrics: accuracy, AUC, log loss, Brier, MAE, RMSE, calibration, Spearman."""
import math
from typing import List, Sequence, Tuple

_EPS = 1e-9


def accuracy(y: Sequence[bool], p: Sequence[float], threshold: float = 0.5) -> float:
    return sum((pi >= threshold) == bool(yi) for yi, pi in zip(y, p)) / len(y)


def log_loss(y: Sequence[bool], p: Sequence[float]) -> float:
    total = 0.0
    for yi, pi in zip(y, p):
        pi = min(1 - _EPS, max(_EPS, pi))
        total += -math.log(pi) if yi else -math.log(1 - pi)
    return total / len(y)


def brier(y: Sequence[bool], p: Sequence[float]) -> float:
    return sum((pi - (1.0 if yi else 0.0)) ** 2 for yi, pi in zip(y, p)) / len(y)


def auc(y: Sequence[bool], p: Sequence[float]) -> float:
    """ROC AUC via the rank-sum (Mann-Whitney) formula with tie-averaged ranks."""
    pairs = sorted(zip(p, y))
    ranks = [0.0] * len(pairs)
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    n_pos = sum(1 for _, yi in pairs if yi)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    rank_sum = sum(r for r, (_, yi) in zip(ranks, pairs) if yi)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def mae(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def rmse(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)) / len(a))


def calibration_bins(y: Sequence[bool], p: Sequence[float], bins: int = 10) -> List[Tuple[float, float, int]]:
    """Returns (mean predicted, observed rate, count) per non-empty bin."""
    buckets = [[] for _ in range(bins)]
    for yi, pi in zip(y, p):
        buckets[min(bins - 1, int(pi * bins))].append((pi, yi))
    return [(sum(x for x, _ in b) / len(b), sum(1 for _, t in b if t) / len(b), len(b)) for b in buckets if b]


def expected_calibration_error(y: Sequence[bool], p: Sequence[float], bins: int = 10) -> float:
    n = len(y)
    return sum(cnt / n * abs(pred - obs) for pred, obs, cnt in calibration_bins(y, p, bins))


def _ranks(xs: Sequence[float]) -> List[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0
        i = j + 1
    return ranks


def pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return cov / (va * vb) if va and vb else float("nan")


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    return pearson(_ranks(a), _ranks(b))


def summary(y: Sequence[bool], p: Sequence[float]) -> dict:
    return {
        "n": len(y),
        "accuracy": accuracy(y, p),
        "auc": auc(y, p),
        "logLoss": log_loss(y, p),
        "brier": brier(y, p),
        "ece": expected_calibration_error(y, p),
    }

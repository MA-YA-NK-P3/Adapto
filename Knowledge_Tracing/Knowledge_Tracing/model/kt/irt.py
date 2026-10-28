"""Rasch (1PL) IRT on a 0-10 scale — reference implementation of backend IrtModel.java.

P(correct | theta, b) = sigmoid(theta - b)
Estimation: MAP Newton-Raphson with a Gaussian prior; uncertainty = 1/sqrt(information).
"""
import math
from typing import Iterable, List, Tuple

MIN_THETA = 0.0
MAX_THETA = 10.0
_MAX_ITER = 50
_TOL = 1e-7


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def clamp(theta: float) -> float:
    if theta != theta or math.isinf(theta):
        return (MIN_THETA + MAX_THETA) / 2.0
    return min(MAX_THETA, max(MIN_THETA, theta))


def p_success(ability: float, difficulty: float) -> float:
    return sigmoid(ability - difficulty)


def information(ability: float, difficulty: float) -> float:
    p = p_success(ability, difficulty)
    return p * (1.0 - p)


def estimate_ability(responses: Iterable[Tuple[float, bool]], prior_mean: float = 5.0,
                     prior_sd: float = 2.0) -> Tuple[float, float]:
    """responses: (item_difficulty, correct). Returns (ability, uncertainty)."""
    responses = list(responses)
    precision = 1.0 / (prior_sd * prior_sd)
    theta = clamp(prior_mean)
    for _ in range(_MAX_ITER):
        grad = -(theta - prior_mean) * precision
        info = precision
        for b, y in responses:
            p = p_success(theta, b)
            grad += (1.0 if y else 0.0) - p
            info += p * (1.0 - p)
        step = grad / info
        theta = clamp(theta + step)
        if abs(step) < _TOL:
            break
    info = precision + sum(information(theta, b) for b, _ in responses)
    return theta, 1.0 / math.sqrt(info)


def estimate_difficulty(observations: Iterable[Tuple[float, bool]], prior_mean: float,
                        prior_sd: float = 1.5) -> Tuple[float, float]:
    """observations: (learner_ability, correct). Returns (difficulty, uncertainty)."""
    observations = list(observations)
    precision = 1.0 / (prior_sd * prior_sd)
    b = clamp(prior_mean)
    for _ in range(_MAX_ITER):
        grad = -(b - prior_mean) * precision
        info = precision
        for theta, y in observations:
            p = p_success(theta, b)
            grad += p - (1.0 if y else 0.0)
            info += p * (1.0 - p)
        step = grad / info
        b = clamp(b + step)
        if abs(step) < _TOL:
            break
    info = precision + sum(information(t, b) for t, _ in observations)
    return b, 1.0 / math.sqrt(info)


STATIC_PRIOR = {"EASY": 3.0, "MEDIUM": 5.0, "HARD": 7.0}
"""TEMPORARY calibration constants — identical to backend application.yml."""


def calibrate_items(sequences: List[List[Tuple[str, bool]]], static_labels: dict,
                    rounds: int = 5) -> dict:
    """Joint (alternating) MAP calibration of item difficulties on a training population.

    sequences: per learner, list of (problem_id, correct).
    Returns {problem_id: difficulty}.
    """
    difficulty = {pid: STATIC_PRIOR[label] for pid, label in static_labels.items()}
    for _ in range(rounds):
        abilities = [estimate_ability([(difficulty[p], y) for p, y in seq])[0] for seq in sequences]
        by_item = {}
        for theta, seq in zip(abilities, sequences):
            for p, y in seq:
                by_item.setdefault(p, []).append((theta, y))
        for pid, obs in by_item.items():
            difficulty[pid] = estimate_difficulty(obs, STATIC_PRIOR[static_labels[pid]])[0]
    return difficulty

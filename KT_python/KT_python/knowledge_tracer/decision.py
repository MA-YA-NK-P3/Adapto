"""Jev — the bounded decision layer (master plan §6, §46; docs/jev.md).

Port of the whole decision package: decision.DecisionLayer, decision.DecisionLayerRouter,
decision.DecisionContext, decision.DecisionOutcome, decision.DeterministicDecisionLayer,
decision.PolicyValidator, decision.FallbackPolicy, decision.bandit.BanditDecisionLayer,
decision.bandit.LinUcb, decision.bandit.BanditStore, decision.bandit.BanditFeatures and
decision.http.HttpDecisionLayer (kt-jev-v1 protocol spec: docs/jev.md).

Jev chooses among pre-computed candidate actions. It must NOT replace IRT calculations,
statistical estimation, database queries, deterministic safety checks or graph
traversal — those have already happened and are summarized in the DecisionContext.
The policy validator and fallback policy apply regardless of provider, so an
unconstrained decision never reaches the learner.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol, Set

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pseudonyms
from .candidates import CandidateAction
from .config import Settings
from .domain import ActionType, BanditArm, Problem
from .knowledge_gap import GapReport, GapType

logger = logging.getLogger(__name__)


class DecisionLayerError(RuntimeError):
    """Port of the IllegalStateException the Java decision layer throws (unknown
    provider, insecure pseudonym salt, transport error, malformed answer). The
    recommendation pipeline catches it — like any provider exception — and falls back."""


@dataclass(frozen=True)
class DecisionContext:
    """Structured input to the decision layer (master plan §6).

    Port of the decision.DecisionContext record. The decision layer receives ONLY this
    bounded view — never raw database access.

    solved_problem_ids: problems the learner has already solved (policy input)
    calibrated:         whether cold-start localization has finished
    """
    learner_ref: str
    ability: float
    uncertainty: float
    calibrated: bool
    gap: GapReport
    candidates: List[CandidateAction]
    solved_problem_ids: Set[uuid.UUID]


@dataclass(frozen=True)
class DecisionOutcome:
    """The decision layer's choice plus a self-reported confidence and rationale.

    Port of the decision.DecisionOutcome record. Confidence feeds the policy validator:
    below the configured threshold the deterministic fallback takes over (§46).
    """
    chosen: Optional[CandidateAction]
    confidence: float
    rationale: str


class DecisionLayer(Protocol):
    """Port of the decision.DecisionLayer interface (the bounded Jev provider SPI)."""
    def decide(self, context: DecisionContext) -> DecisionOutcome:
        ...
    def name(self) -> str:
        """Provider id used in configuration and in the audit record."""
        ...


# --------------------------------------------------------------------------
# Policy validation (decision.PolicyValidator)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationResult:
    """Port of the PolicyValidator.ValidationResult record."""
    valid: bool
    violations: List[str]


class PolicyValidator:
    """Deterministic safety net between the decision layer and the learner (§6, §46).

    Port of decision.PolicyValidator. A decision is valid only if:
      * a candidate was chosen and it came from the generated candidate set
      * the referenced problem exists in the database
      * difficulty is finite and inside [0,10]
      * already-solved problems are only recommended as RETRY/SPACED_REVIEW
      * decision confidence meets the configured threshold
    """

    def __init__(self, session: Session):
        self.session = session

    def validate(self, outcome: Optional[DecisionOutcome], context: DecisionContext,
                 confidence_threshold: float) -> ValidationResult:
        violations: List[str] = []

        if outcome is None or outcome.chosen is None:
            violations.append("decision layer returned no candidate")
            return ValidationResult(False, violations)
        chosen = outcome.chosen

        from_candidate_set = any(
            c.action == chosen.action and c.problem_key == chosen.problem_key
            for c in context.candidates)
        if not from_candidate_set:
            violations.append("chosen action was not in the generated candidate set")

        if chosen.problem_key is not None:
            problem = self.session.scalar(
                select(Problem).where(Problem.problem_key == chosen.problem_key))
            if problem is None:
                violations.append("referenced problem does not exist: " + chosen.problem_key)
            else:
                if (chosen.difficulty is not None
                        and (chosen.difficulty < 0.0 or chosen.difficulty > 10.0
                             or math.isnan(chosen.difficulty))):
                    violations.append("difficulty outside [0,10]")
                already_solved = problem.id in context.solved_problem_ids
                retry_like = chosen.action in (ActionType.RETRY_PROBLEM,
                                               ActionType.SPACED_REVIEW)
                if already_solved and not retry_like:
                    violations.append(
                        "problem already solved; only RETRY/SPACED_REVIEW may reference it")

        if outcome.confidence < confidence_threshold:
            violations.append(
                "decision confidence %.2f below threshold %.2f"
                % (outcome.confidence, confidence_threshold))

        return ValidationResult(not violations, violations)


# --------------------------------------------------------------------------
# Deterministic provider (decision.DeterministicDecisionLayer)
# --------------------------------------------------------------------------

class DeterministicDecisionLayer:
    """Deterministic in-process Jev provider (default). Port of
    decision.DeterministicDecisionLayer — the decision policy as explicit, testable
    rules, so behaviour is reproducible without any external service:

      1. prerequisite gap -> prerequisite review (remediation outranks calibration: a
         well-evidenced gap is more actionable than more ability measurement)
      2. concept gap -> easier problem on that concept
      3. uncalibrated learner -> calibration item (exploration, §38)
      4. otherwise -> highest expected-value candidate (exploitation)

    Remediation items are still informative responses, so calibration continues while
    the learner works on the gap.
    """

    def name(self) -> str:
        return "deterministic"

    def decide(self, context: DecisionContext) -> DecisionOutcome:
        if not context.candidates:
            return DecisionOutcome(None, 0.0, "No candidates available")

        gap = context.gap.type
        if gap is GapType.PREREQUISITE_GAP:
            review = self._first(context, ActionType.PREREQUISITE_REVIEW,
                                 "Evidence suggests prerequisite "
                                 + str(context.gap.prerequisite_code)
                                 + " is weak for " + str(context.gap.concept_code))
            if review is not None:
                return review
        if gap in (GapType.CONCEPT_GAP, GapType.PREREQUISITE_GAP):
            easier = self._first(context, ActionType.EASIER_PROBLEM,
                                 "Evidence suggests concept gap for "
                                 + str(context.gap.concept_code))
            if easier is not None:
                return easier

        if not context.calibrated:
            calibration = [c for c in context.candidates
                           if c.action is ActionType.CALIBRATION_PROBLEM]
            if calibration:
                best = max(calibration, key=lambda c: c.expected_value)
                return DecisionOutcome(
                    best, 0.8, "Cold start: maximizing information about learner ability")

        best = max(context.candidates, key=lambda c: c.expected_value)
        return DecisionOutcome(best, min(0.9, best.expected_value),
                               "No actionable gap: selecting highest expected-value candidate")

    def _first(self, context: DecisionContext, action: ActionType,
               rationale: str) -> Optional[DecisionOutcome]:
        for c in context.candidates:
            if c.action is action:
                return DecisionOutcome(c, context.gap.confidence, rationale)
        return None


# --------------------------------------------------------------------------
# Bandit provider (decision.bandit.*)
# --------------------------------------------------------------------------

class BanditFeatures:
    """Context vector of one candidate (docs/jev.md §features).

    Port of decision.bandit.BanditFeatures. Every feature is bounded so one arm's
    statistics stay comparable across learners. Changing the layout requires a new
    VERSION (stored arms with another dimension are reset).
    """
    VERSION = 1
    NAMES = ["bias", "predictedSuccess", "information", "relativeDifficulty",
             "abilityUncertainty", "calibrated", "gapMatch", "expectedValue"]
    DIM = len(NAMES)
    #: Warm start: predicted reward = the heuristic expected value.
    THETA0 = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]

    @staticmethod
    def of(c: CandidateAction, ctx: DecisionContext) -> List[float]:
        p = c.predicted_success if c.predicted_success is not None else 0.5
        rel_diff = (_clamp((c.difficulty - ctx.ability) / 3.0, -1.0, 1.0)
                    if c.difficulty is not None else 0.0)
        gap_match = (c.concept_code is not None and ctx.gap is not None
                     and (c.concept_code == ctx.gap.concept_code
                          or c.concept_code == ctx.gap.prerequisite_code))
        return [
            1.0,                                            # bias
            p,                                              # predictedSuccess
            4.0 * p * (1.0 - p),                            # information
            rel_diff,                                       # relativeDifficulty
            _clamp(ctx.uncertainty / 2.0, 0.0, 1.0),        # abilityUncertainty
            1.0 if ctx.calibrated else 0.0,                 # calibrated
            _clamp(ctx.gap.confidence, 0.0, 1.0) if gap_match else 0.0,  # gapMatch
            _clamp(c.expected_value, 0.0, 1.5),             # expectedValue
        ]


@dataclass(frozen=True)
class BanditScore:
    """Port of the LinUcb.Score record: mean = predicted reward, width = uncertainty,
    ucb = mean + alpha * width."""
    mean: float
    width: float
    ucb: float


class LinUCB:
    """Disjoint LinUCB (Li et al., 2010) for one arm — port of decision.bandit.LinUcb,
    with a ridge prior centred on a warm start theta0 instead of 0:

        A = lambda*I + sum x x'        b = lambda*theta0 + sum r x        theta = A^-1 b
        score(x) = theta . x + alpha * sqrt(x' A^-1 x)

    With no data theta = theta0, so a fresh bandit ranks candidates exactly like the
    deterministic heuristic (theta0 puts weight 1 on the expectedValue feature) plus an
    exploration bonus that shrinks as the arm accumulates evidence.

    numpy arrays replace the Java double[]/double[][]; the Cholesky solve mirrors
    LinUcb.cholesky/solve (A is symmetric positive definite by construction;
    numpy raises LinAlgError where the Java code throws IllegalStateException).
    """

    @staticmethod
    def prior_a(dim: int, lambda_: float) -> np.ndarray:
        a = np.zeros((dim, dim))
        np.fill_diagonal(a, lambda_)
        return a

    @staticmethod
    def prior_b(theta0: List[float], lambda_: float) -> np.ndarray:
        return lambda_ * np.asarray(theta0, dtype=float)

    @staticmethod
    def update(a: np.ndarray, b: np.ndarray, x, r: float) -> None:
        """In-place rank-one update with reward r."""
        xv = np.asarray(x, dtype=float)
        a += np.outer(xv, xv)
        b += r * xv

    @staticmethod
    def score(a: np.ndarray, b: np.ndarray, x, alpha: float) -> BanditScore:
        xv = np.asarray(x, dtype=float)
        lower = np.linalg.cholesky(a)
        theta = LinUCB._solve(lower, np.asarray(b, dtype=float))
        ainv_x = LinUCB._solve(lower, xv)
        mean = float(np.dot(theta, xv))
        width = math.sqrt(max(0.0, float(np.dot(xv, ainv_x))))
        return BanditScore(mean, width, mean + alpha * width)

    @staticmethod
    def theta(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return LinUCB._solve(np.linalg.cholesky(a), np.asarray(b, dtype=float))

    @staticmethod
    def _solve(lower: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Solves L L' z = y (forward then back substitution, as LinUcb.solve)."""
        n = y.shape[0]
        w = np.zeros(n)
        for i in range(n):
            w[i] = (y[i] - float(np.dot(lower[i, :i], w[:i]))) / lower[i, i]
        z = np.zeros(n)
        for i in range(n - 1, -1, -1):
            z[i] = (w[i] - float(np.dot(lower[i + 1:, i], z[i + 1:]))) / lower[i, i]
        return z


@dataclass
class ArmStats:
    """Sufficient statistics of one arm. Port of the BanditStore.Stats record."""
    a: np.ndarray
    b: np.ndarray
    pulls: int
    reward_sum: float


class BanditStore:
    """Loads and updates LinUCB arm statistics (port of decision.bandit.BanditStore).

    Arms without data use the warm-start prior. Statistics are persisted as JSON in
    jev_bandit_arms rows (one per ActionType, global — not per learner). The Java
    repository takes a pessimistic row lock (BanditArmRepository.findForUpdate);
    the replica joins the caller's transaction instead, like the other services.
    """

    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def snapshot(self) -> Dict[str, ArmStats]:
        """Arm name -> stats for every stored arm compatible with the feature layout."""
        arms: Dict[str, ArmStats] = {}
        for row in self.session.scalars(select(BanditArm)):
            if self._compatible(row):
                arms[row.arm] = self._to_stats(row)
        return arms

    def prior(self) -> ArmStats:
        lambda_ = self.settings.jev.bandit.lambda_
        return ArmStats(LinUCB.prior_a(BanditFeatures.DIM, lambda_),
                        LinUCB.prior_b(BanditFeatures.THETA0, lambda_), 0, 0.0)

    def update(self, arm: str, x, reward: float) -> None:
        """Applies one observed reward to an arm; joins the caller's transaction.

        An arm stored with another feature dimension or version is reset to the
        warm-start prior before the update (Java parity).
        """
        if len(x) != BanditFeatures.DIM:
            logger.warning("Ignoring bandit update for %s: feature dim %d != %d",
                           arm, len(x), BanditFeatures.DIM)
            return
        row = self.session.get(BanditArm, arm)
        if row is None or not self._compatible(row):
            if row is None:
                row = BanditArm(arm=arm)
                self.session.add(row)
            self._write(row, self.prior())
            row.pulls = 0
            row.reward_sum = 0.0
        stats = self._to_stats(row)
        LinUCB.update(stats.a, stats.b, x, reward)
        self._write(row, stats)
        row.pulls += 1
        row.reward_sum += reward
        row.updated_at = datetime.now(timezone.utc)
        self.session.flush()

    def _compatible(self, arm: BanditArm) -> bool:
        return arm.dim == BanditFeatures.DIM and arm.feature_version == BanditFeatures.VERSION

    def _to_stats(self, arm: BanditArm) -> ArmStats:
        try:
            a = np.asarray(json.loads(arm.a_matrix), dtype=float)
            b = np.asarray(json.loads(arm.b_vector), dtype=float)
            return ArmStats(a, b, arm.pulls, arm.reward_sum)
        except (ValueError, TypeError):
            logger.error("Corrupt bandit arm %s; using prior", arm.arm)
            return self.prior()

    def _write(self, arm: BanditArm, stats: ArmStats) -> None:
        arm.dim = BanditFeatures.DIM
        arm.feature_version = BanditFeatures.VERSION
        arm.a_matrix = json.dumps(np.asarray(stats.a, dtype=float).tolist())
        arm.b_vector = json.dumps(np.asarray(stats.b, dtype=float).tolist())


class BanditDecisionLayer:
    """Jev provider "bandit" — port of decision.bandit.BanditDecisionLayer.

    A LinUCB contextual bandit over the candidate set, one arm per ActionType, learning
    online from the outcome of each recommendation (recommendation.OutcomeRewardService).
    Bounded like every provider: it only ranks candidates it was given, and the policy
    validator still checks its choice.

    Guarded mode (default): an actionable concept/prerequisite gap is remediated by the
    deterministic rules, so exploration never postpones a well-evidenced remediation.
    """

    def __init__(self, settings: Settings, store: BanditStore,
                 rules: DeterministicDecisionLayer):
        self.settings = settings
        self.store = store
        self.rules = rules

    def name(self) -> str:
        return "bandit"

    def decide(self, context: DecisionContext) -> DecisionOutcome:
        if not context.candidates:
            return DecisionOutcome(None, 0.0, "No candidates available")
        cfg = self.settings.jev.bandit
        gap = context.gap.type if context.gap is not None else GapType.UNKNOWN
        if cfg.guarded and gap in (GapType.CONCEPT_GAP, GapType.PREREQUISITE_GAP):
            remediable = any(
                c.action in (ActionType.PREREQUISITE_REVIEW, ActionType.EASIER_PROBLEM)
                for c in context.candidates)
            if remediable:
                ruled = self.rules.decide(context)
                return DecisionOutcome(ruled.chosen, ruled.confidence,
                                       "Guarded bandit: gap remediation rule. "
                                       + ruled.rationale)

        arms = self.store.snapshot()
        prior: Optional[ArmStats] = None
        best: Optional[CandidateAction] = None
        best_score: Optional[BanditScore] = None
        for c in context.candidates:
            stats = arms.get(c.action.value)
            if stats is None:
                if prior is None:
                    prior = self.store.prior()
                stats = prior
            score = LinUCB.score(stats.a, stats.b, BanditFeatures.of(c, context), cfg.alpha)
            if best_score is None or score.ucb > best_score.ucb:
                best, best_score = c, score

        chosen_arm = arms.get(best.action.value)
        pulls = chosen_arm.pulls if chosen_arm is not None else 0
        confidence = max(0.35, min(0.95, 1.0 / (1.0 + best_score.width)))
        return DecisionOutcome(best, confidence,
                               "LinUCB: arm %s predicted reward %.2f + exploration %.2f "
                               "(alpha %.2f, %d updates)"
                               % (best.action.value, best_score.mean,
                                  cfg.alpha * best_score.width, cfg.alpha, pulls))


# --------------------------------------------------------------------------
# HTTP provider (decision.http.HttpDecisionLayer)
# --------------------------------------------------------------------------

class HttpDecisionLayer:
    """Jev provider "http" — port of decision.http.HttpDecisionLayer.

    Delegates the choice to an external service speaking the kt-jev-v1 JSON protocol
    (docs/jev.md; reference server: scripts/jev_llm_server.py). The service only sees
    the bounded DecisionContext (keyed learner pseudonym, estimates, candidate list)
    and may only answer with an index into that list. Any transport error, timeout or
    malformed answer raises DecisionLayerError, which makes the recommendation pipeline
    use the deterministic fallback policy; so does an insecure pseudonym salt, which is
    refused BEFORE any request is made.

    The Java RestClient is replaced by urllib.request (stdlib); the timeout comes from
    settings.jev.http.timeout_ms.
    """

    PROTOCOL = "kt-jev-v1"

    def __init__(self, settings: Settings):
        self.settings = settings

    def name(self) -> str:
        return "http"

    def decide(self, context: DecisionContext) -> DecisionOutcome:
        salt = self.settings.research.pseudonym_salt
        if pseudonyms.is_insecure_salt(salt):
            raise DecisionLayerError(
                "Jev http provider refuses to send pseudonyms keyed with a blank or "
                "default salt; set KT_PSEUDONYM_SALT")
        cfg = self.settings.jev.http
        pseudonym = pseudonyms.of(context.learner_ref, salt)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if cfg.api_key and cfg.api_key.strip():
            headers["X-API-Key"] = cfg.api_key
        request = urllib.request.Request(
            cfg.url,
            data=json.dumps(self.request_body(context, pseudonym)).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout_ms / 1000.0) as response:
                payload = response.read().decode("utf-8")
        except Exception as exc:
            raise DecisionLayerError(
                "Jev http request to %s failed: %s" % (cfg.url, exc)) from exc
        try:
            answer = json.loads(payload)
        except ValueError as exc:
            raise DecisionLayerError(
                "Jev http provider returned malformed JSON: %s" % exc) from exc

        index = answer.get("chosenIndex") if isinstance(answer, dict) else None
        if (index is None or isinstance(index, bool) or not isinstance(index, int)
                or index < 0 or index >= len(context.candidates)):
            raise DecisionLayerError(
                "Jev http provider returned no valid candidate index: %s" % (answer,))
        raw_confidence = answer.get("confidence")
        confidence = (max(0.0, min(1.0, float(raw_confidence)))
                      if raw_confidence is not None else 0.5)
        raw_rationale = answer.get("rationale")
        rationale = (_truncate(raw_rationale, 500)
                     if isinstance(raw_rationale, str) and raw_rationale.strip()
                     else "external Jev decision")
        return DecisionOutcome(context.candidates[index], confidence,
                               "External Jev: " + rationale)

    @staticmethod
    def request_body(context: DecisionContext, learner_pseudonym: str) -> Dict[str, object]:
        """The kt-jev-v1 request document (public so the protocol can be tested and
        documented). Port of HttpDecisionLayer.requestBody."""
        gap: Dict[str, object] = {}
        if context.gap is not None:
            gap["type"] = context.gap.type.value
            gap["concept"] = context.gap.concept_code
            gap["prerequisite"] = context.gap.prerequisite_code
            gap["confidence"] = context.gap.confidence
            gap["evidence"] = list(context.gap.evidence)
        candidates: List[Dict[str, object]] = []
        for i, c in enumerate(context.candidates):
            candidates.append({
                "index": i,
                "action": c.action.value,
                "problemKey": c.problem_key,
                "conceptCode": c.concept_code,
                "difficulty": c.difficulty,
                "predictedSuccess": c.predicted_success,
                "expectedValue": c.expected_value,
                "reason": c.reason,
            })
        return {
            "protocol": HttpDecisionLayer.PROTOCOL,
            "learner": {
                "id": learner_pseudonym,
                "ability": context.ability,
                "uncertainty": context.uncertainty,
                "calibrated": context.calibrated,
            },
            "gap": gap,
            "candidates": candidates,
            "rules": [
                "choose exactly one candidate by index",
                "prefer remediating a well-evidenced prerequisite or concept gap",
                "target predicted success near 0.65 (productive struggle)",
                "never invent problems or actions",
            ],
        }


# --------------------------------------------------------------------------
# Router and fallback (decision.DecisionLayerRouter, decision.FallbackPolicy)
# --------------------------------------------------------------------------

class DecisionLayerRouter:
    """The DecisionLayer the recommendation pipeline talks to — port of
    decision.DecisionLayerRouter: dispatches to the provider named by
    settings.jev.provider. An unknown provider raises, which triggers the
    deterministic fallback (and is visible as decided_by=FALLBACK).
    """

    def __init__(self, settings: Settings, deterministic: DeterministicDecisionLayer,
                 bandit: BanditDecisionLayer, http: HttpDecisionLayer):
        self.settings = settings
        self.providers: Dict[str, DecisionLayer] = {
            layer.name(): layer for layer in (deterministic, bandit, http)}

    def name(self) -> str:
        return self.provider().name()

    def decide(self, context: DecisionContext) -> DecisionOutcome:
        return self.provider().decide(context)

    def provider(self) -> DecisionLayer:
        provider_id = self.settings.jev.provider.strip().lower()
        layer = self.providers.get(provider_id)
        if layer is None:
            raise DecisionLayerError(
                "Unknown Jev provider '%s'; expected one of [%s]"
                % (provider_id, ", ".join(self.providers)))
        return layer


class FallbackPolicy:
    """Deterministic fallback (master plan §46) — port of decision.FallbackPolicy.

    When Jev is unavailable, returns an invalid action, or reports low confidence, this
    policy keeps the system operating: pick the highest expected-value candidate that
    passes policy validation (at threshold 0); if none qualifies, fall back to the
    unsolved free problem nearest the ability estimate; if the pool is exhausted, ask
    the learner for feedback.
    """

    def __init__(self, session: Session, validator: PolicyValidator):
        self.session = session
        self.validator = validator

    def select(self, context: DecisionContext) -> DecisionOutcome:
        logger.warning("Fallback policy triggered for learner=%s (candidates=%d)",
                       context.learner_ref, len(context.candidates))
        by_value = sorted(context.candidates, key=lambda c: c.expected_value,
                          reverse=True)
        for c in by_value:
            if self.validator.validate(DecisionOutcome(c, 1.0, "fallback"),
                                       context, 0.0).valid:
                return DecisionOutcome(
                    c, 0.5,
                    "Deterministic fallback: highest expected-value valid candidate")
        return self._nearest_unsolved(context)

    def _nearest_unsolved(self, context: DecisionContext) -> DecisionOutcome:
        pool = [p for p in self.session.scalars(select(Problem))
                if not p.paid_only and p.id not in context.solved_problem_ids]
        if pool:
            nearest = min(pool,
                          key=lambda p: abs(p.estimated_difficulty - context.ability))
            return DecisionOutcome(
                CandidateAction(
                    ActionType.SAME_LEVEL_PROBLEM, nearest.problem_key, None,
                    nearest.estimated_difficulty,
                    "Fallback: unsolved problem nearest ability %.2f" % context.ability,
                    0.3),
                0.3, "Deterministic fallback: nearest-difficulty unsolved problem")
        return DecisionOutcome(
            CandidateAction(
                ActionType.FEEDBACK_REQUEST, None, None, None,
                "No suitable problem available; ask the learner for feedback", 0.1),
            0.1, "Deterministic fallback: exhausted problem pool")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _truncate(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[:max_len]

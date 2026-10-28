"""Recommendation orchestration and outcome rewards (master plan §10, §17, §19;
docs/recommendation.md, docs/jev.md, docs/api-flow.md §5.4/§5.5).

Port of recommendation.RecommendationService, recommendation.OutcomeRewardService and
the recommendation.api.dto.NextActionResponse record family.

The pipeline:

    learner state -> gap detection -> candidate generation -> Jev decision
      -> policy validation -> (fallback if invalid) -> persist audit record -> response

Field names on the response dataclasses are snake_case mirrors of the Java record
components (userId -> user_id, totalEvents -> total_events, ...); to_dict() produces
the exact camelCase JSON shape from docs/api-flow.md for the API layer.
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .candidates import CandidateAction, CandidateGenerationService
from .config import Settings
from .decision import (BanditFeatures, BanditStore, DecisionContext, DecisionLayer,
                       DecisionOutcome, FallbackPolicy, PolicyValidator,
                       ValidationResult)
from .domain import (ActionType, Concept, InteractionEvent, InteractionResult, Learner,
                     LearnerState, Problem, Recommendation)
from .errors import BusinessRuleError, NotFoundError
from .knowledge_gap import GapReport, GapType, KnowledgeGapService
from .state import LearnerStateService
from .success import DktPredictorLike, SuccessEstimator

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Response DTOs (recommendation.api.dto.NextActionResponse and nested records)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class StateView:
    """Port of NextActionResponse.StateView. Values are rounded estimates."""
    ability: float
    uncertainty: float
    calibrated: bool
    total_events: int


@dataclass(frozen=True)
class GapView:
    """Port of NextActionResponse.GapView."""
    type: str
    concept: Optional[str]
    prerequisite: Optional[str]
    evidence: List[str]
    confidence: float


@dataclass(frozen=True)
class RecommendationView:
    """Port of NextActionResponse.RecommendationView.

    topic is the candidate's concept code (set for PREREQUISITE_REVIEW and
    EASIER_PROBLEM). evidence is the gap evidence only when the chosen action targets
    the gap concept or prerequisite, otherwise [reason].
    """
    recommendation_id: uuid.UUID
    type: str
    problem_id: Optional[str]
    topic: Optional[str]
    difficulty: Optional[float]
    reason: Optional[str]
    evidence: List[str]
    confidence: Optional[float]
    expected_value: Optional[float]
    predicted_success: Optional[float]
    decided_by: str
    provider: Optional[str]
    fallback_used: bool


@dataclass(frozen=True)
class FeedbackView:
    """Port of NextActionResponse.FeedbackView."""
    required: bool
    questions: List[str]


@dataclass(frozen=True)
class HistoryEntry:
    """One persisted recommendation (audit trail). Port of NextActionResponse.HistoryEntry."""
    recommendation_id: uuid.UUID
    created_at: datetime
    type: str
    problem_id: Optional[str]
    topic: Optional[str]
    difficulty: Optional[float]
    reason: Optional[str]
    confidence: Optional[float]
    decided_by: str
    provider: Optional[str]
    fallback_used: bool
    reward: Optional[float]

    def to_dict(self) -> Dict[str, object]:
        """The camelCase JSON shape of GET .../recommendations (docs/api-flow.md §5.5)."""
        return {
            "recommendationId": str(self.recommendation_id),
            "createdAt": _iso_instant(self.created_at),
            "type": self.type,
            "problemId": self.problem_id,
            "topic": self.topic,
            "difficulty": self.difficulty,
            "reason": self.reason,
            "confidence": self.confidence,
            "decidedBy": self.decided_by,
            "provider": self.provider,
            "fallbackUsed": self.fallback_used,
            "reward": self.reward,
        }


@dataclass(frozen=True)
class NextActionResponse:
    """Response shape for GET /api/learners/{userId}/next-action (master plan §19).

    Port of the NextActionResponse record. Model estimates (ability, mastery,
    confidence) are explicitly estimates — see docs/limitations.md.

    models:       which model components produced this response (ablation/auditability)
    dkt_knowledge: DKT concept-mastery estimates; empty when DKT is disabled/unavailable
                  or the learner has too little history
    """
    user_id: str
    state: StateView
    knowledge: Dict[str, float]
    gap: GapView
    recommendation: RecommendationView
    feedback: FeedbackView
    models: Dict[str, str]
    dkt_knowledge: Dict[str, float]

    def to_dict(self) -> Dict[str, object]:
        """The camelCase JSON shape of the next-action response (docs/api-flow.md §5.4)."""
        r = self.recommendation
        return {
            "userId": self.user_id,
            "state": {
                "ability": self.state.ability,
                "uncertainty": self.state.uncertainty,
                "calibrated": self.state.calibrated,
                "totalEvents": self.state.total_events,
            },
            "knowledge": dict(self.knowledge),
            "gap": {
                "type": self.gap.type,
                "concept": self.gap.concept,
                "prerequisite": self.gap.prerequisite,
                "evidence": list(self.gap.evidence),
                "confidence": self.gap.confidence,
            },
            "recommendation": {
                "recommendationId": str(r.recommendation_id),
                "type": r.type,
                "problemId": r.problem_id,
                "topic": r.topic,
                "difficulty": r.difficulty,
                "reason": r.reason,
                "evidence": list(r.evidence),
                "confidence": r.confidence,
                "expectedValue": r.expected_value,
                "predictedSuccess": r.predicted_success,
                "decidedBy": r.decided_by,
                "provider": r.provider,
                "fallbackUsed": r.fallback_used,
            },
            "feedback": {
                "required": self.feedback.required,
                "questions": list(self.feedback.questions),
            },
            "models": dict(self.models),
            "dktKnowledge": dict(self.dkt_knowledge),
        }


# --------------------------------------------------------------------------
# RecommendationService
# --------------------------------------------------------------------------

class RecommendationService:
    """Port of recommendation.RecommendationService — orchestrates the pipeline.

    The Java constructor injects LearnerService, the three repositories
    (InteractionEventRepository, ProblemRepository, ConceptRepository,
    RecommendationRepository) and an ObjectMapper alongside the pipeline services; the
    replica collapses learner lookup and the repositories into the SQLAlchemy session
    and uses the json module, as the other foundation services do. dkt_predictor is
    only used for the model report (Java: dktPredictor.status()); it is duck-typed —
    knowledge_tracer.dkt is intentionally not imported.
    """

    def __init__(self, session: Session, settings: Settings,
                 state_service: LearnerStateService,
                 gap_service: KnowledgeGapService,
                 candidate_generation_service: CandidateGenerationService,
                 decision_layer: DecisionLayer,
                 policy_validator: PolicyValidator,
                 fallback_policy: FallbackPolicy,
                 success_estimator: SuccessEstimator,
                 dkt_predictor: Optional[DktPredictorLike] = None):
        self.session = session
        self.settings = settings
        self.state_service = state_service
        self.gap_service = gap_service
        self.candidate_generation = candidate_generation_service
        self.decision_layer = decision_layer
        self.policy_validator = policy_validator
        self.fallback_policy = fallback_policy
        self.success_estimator = success_estimator
        self.dkt_predictor = dkt_predictor

    def next_action(self, user_id: str) -> NextActionResponse:
        """Run the full pipeline and store one audit row per call (docs/api-flow.md §5.4).

        Unknown learners are rejected with NotFoundError (404), exactly like
        LearnerService.getByExternalRef — next-action never auto-provisions.
        """
        learner = self._get_by_external_ref(user_id)
        state = self.state_service.get_or_create_state(learner)
        gap = self.gap_service.detect(state)
        estimate = self.success_estimator.for_learner(learner, state)
        candidates = self.candidate_generation.generate(learner, state, gap, estimate)

        context = DecisionContext(
            user_id, state.ability, state.uncertainty, state.calibrated,
            gap, candidates, self._solved_problem_ids(learner.id))

        # --- Jev decision layer (bounded; never the only line of defense) ---
        outcome: Optional[DecisionOutcome] = None
        fallback_used = False
        provider: Optional[str] = None
        if self.settings.jev.enabled:
            try:
                provider = self.decision_layer.name()
                outcome = self.decision_layer.decide(context)
            except Exception as exc:
                logger.error("Decision layer failed for learner=%s: %s", user_id, exc)
        validation = (self.policy_validator.validate(
            outcome, context, self.settings.jev.confidence_threshold)
            if outcome is not None
            else ValidationResult(False, ["decision layer disabled or failed"]))

        if not validation.valid:
            logger.warning("Decision invalid for learner=%s violations=%s -> fallback",
                           user_id, validation.violations)
            outcome = self.fallback_policy.select(context)
            fallback_used = True

        if outcome is None or outcome.chosen is None:
            raise BusinessRuleError(
                "No recommendation could be produced (empty problem pool?)")

        audit = self._persist_audit(learner, state, context, outcome,
                                    None if fallback_used else provider, fallback_used)
        logger.info(
            "Recommendation generated learner=%s action=%s problem=%s decidedBy=%s provider=%s",
            user_id, outcome.chosen.action.value, outcome.chosen.problem_key,
            "FALLBACK" if fallback_used else "JEV", audit.provider)
        return self._build_response(learner, state, gap, audit, outcome, estimate)

    def history(self, user_id: str, limit: int) -> List[HistoryEntry]:
        """Past recommendations, newest first (docs/api-flow.md §5.5).

        limit is clamped to [1, 200], as in the Java service.
        """
        learner = self._get_by_external_ref(user_id)
        recs = list(self.session.scalars(
            select(Recommendation)
            .where(Recommendation.learner_id == learner.id)
            .order_by(Recommendation.created_at.desc())))
        capped = max(1, min(limit, 200))
        return [HistoryEntry(
                    r.id, r.created_at, r.action.value,
                    r.problem.problem_key if r.problem is not None else None,
                    r.concept.code if r.concept is not None else None,
                    round2(r.difficulty) if r.difficulty is not None else None,
                    r.reason,
                    round2(r.confidence) if r.confidence is not None else None,
                    r.decided_by, r.provider, r.fallback_used,
                    round2(r.reward) if r.reward is not None else None)
                for r in recs[:capped]]

    # --- internals ---------------------------------------------------------

    def _get_by_external_ref(self, external_ref: str) -> Learner:
        """Port of LearnerService.getByExternalRef: 404, never auto-provisioned here."""
        learner = self.session.scalar(
            select(Learner).where(Learner.external_ref == external_ref))
        if learner is None:
            raise NotFoundError("Learner not found: " + external_ref)
        return learner

    def _solved_problem_ids(self, learner_id: uuid.UUID):
        """Port of InteractionEventRepository.findSolvedProblemIds (ACCEPTED only)."""
        return set(self.session.scalars(
            select(InteractionEvent.problem_id)
            .where(InteractionEvent.learner_id == learner_id)
            .where(InteractionEvent.result == InteractionResult.ACCEPTED)
            .distinct()))

    def _persist_audit(self, learner: Learner, state: LearnerState,
                       context: DecisionContext, outcome: DecisionOutcome,
                       provider: Optional[str], fallback_used: bool) -> Recommendation:
        """One recommendations row per decision (port of persistAudit).

        context_json (the bandit feature vector) is stored only when the chosen action
        has a problem; candidates_json always stores the full serialized candidate list.
        """
        chosen = outcome.chosen
        rec = Recommendation(
            learner_id=learner.id,
            action=chosen.action,
            difficulty=chosen.difficulty,
            reason=chosen.reason,
            confidence=outcome.confidence,
            expected_value=chosen.expected_value,
            predicted_success=chosen.predicted_success,
            decided_by="FALLBACK" if fallback_used else "JEV",
            fallback_used=fallback_used,
            provider=provider,
            ability_at_decision=state.ability,
            candidates_json=_serialize_candidates(context.candidates),
        )
        if chosen.problem_key is not None:
            rec.context_json = json.dumps(BanditFeatures.of(chosen, context))
            problem = self.session.scalar(
                select(Problem).where(Problem.problem_key == chosen.problem_key))
            if problem is not None:
                rec.problem_id = problem.id
        if chosen.concept_code is not None:
            concept = self.session.scalar(
                select(Concept).where(Concept.code == chosen.concept_code))
            if concept is not None:
                rec.concept_id = concept.id
        self.session.add(rec)
        self.session.flush()
        return rec

    def _build_response(self, learner: Learner, state: LearnerState, gap: GapReport,
                        audit: Recommendation, outcome: DecisionOutcome,
                        estimate) -> NextActionResponse:
        knowledge = dict(sorted(
            ((to_camel_case(m.concept.code), round2(m.mastery))
             for m in self.state_service.masteries_of(learner)),
            key=lambda kv: kv[0]))
        dkt_knowledge: Dict[str, float] = {}
        snapshot = estimate.dkt()
        if snapshot is not None:
            dkt_knowledge = dict(sorted(
                ((to_camel_case(code), round2(m))
                 for code, m in snapshot.mastery().items()),
                key=lambda kv: kv[0]))

        feedback_required = gap.type not in (GapType.NONE, GapType.UNKNOWN,
                                             GapType.INSUFFICIENT_PRACTICE)

        chosen = outcome.chosen
        # Only cite gap evidence when the chosen action actually addresses that gap.
        addresses_gap = (chosen.concept_code is not None
                         and (chosen.concept_code == gap.concept_code
                              or chosen.concept_code == gap.prerequisite_code))
        evidence = list(gap.evidence) if addresses_gap else [chosen.reason]

        return NextActionResponse(
            learner.external_ref,
            StateView(round2(state.ability), round2(state.uncertainty),
                      state.calibrated, state.total_events),
            knowledge,
            GapView(gap.type.value, gap.concept_code, gap.prerequisite_code,
                    list(gap.evidence), round2(gap.confidence)),
            RecommendationView(
                audit.id,
                chosen.action.value,
                chosen.problem_key,
                chosen.concept_code,
                round2(chosen.difficulty) if chosen.difficulty is not None else None,
                chosen.reason,
                evidence,
                round2(outcome.confidence),
                round2(chosen.expected_value),
                round2(chosen.predicted_success)
                if chosen.predicted_success is not None else None,
                audit.decided_by,
                audit.provider,
                audit.fallback_used),
            FeedbackView(feedback_required,
                         feedback_questions(gap.type) if feedback_required else []),
            self._model_report(estimate),
            dkt_knowledge)

    def _model_report(self, estimate) -> Dict[str, str]:
        """Which model components produced this response (ablation / auditability,
        §36, §45). Port of modelReport; key order matches the Java LinkedHashMap."""
        models: Dict[str, str] = {}
        models["irt"] = ("ENABLED (Rasch 1PL, MAP)" if self.settings.irt.enabled
                         else "DISABLED")
        models["conceptMastery"] = "ENABLED (Beta-Bernoulli)"
        models["prerequisiteGraph"] = ("ENABLED" if self.settings.graph.enabled
                                       else "DISABLED")
        models["adaptiveLocalization"] = ("ENABLED" if self.settings.localization.enabled
                                          else "DISABLED")
        models["dkt"] = self._dkt_status()
        models["successModel"] = estimate.source()
        models["jev"] = ("ENABLED (" + self.settings.jev.provider + ")"
                         if self.settings.jev.enabled
                         else "DISABLED (fallback policy only)")
        return models

    def _dkt_status(self) -> str:
        """Java: dkt.enabled ? dktPredictor.status() : "DISABLED". The duck-typed
        predictor may lack status() (test fakes); then a plain "ENABLED" is reported."""
        if not self.settings.dkt.enabled:
            return "DISABLED"
        status = getattr(self.dkt_predictor, "status", None)
        return status() if callable(status) else "ENABLED"


def feedback_questions(gap_type: GapType) -> List[str]:
    """The fixed self-report questions per gap type (port of feedbackQuestions)."""
    if gap_type is GapType.PREREQUISITE_GAP:
        return ["Did you understand the prerequisite?",
                "What prevented you from solving the problem?"]
    if gap_type is GapType.TIME_PRESSURE:
        return ["Was your approach correct but too slow?",
                "Did you know a more efficient algorithm for this problem?"]
    if gap_type is GapType.LOW_CONFIDENCE:
        return ["Which part of the solution are you unsure about?"]
    return ["What prevented you from solving the problem?",
            "Which concept felt unfamiliar?"]


def to_camel_case(code: str) -> str:
    """ARRAYS -> arrays, LINKED_LIST -> linkedList (response shape per master plan §19)."""
    out: List[str] = []
    upper_next = False
    for ch in code.lower():
        if ch == "_":
            upper_next = True
        elif upper_next:
            out.append(ch.upper())
            upper_next = False
        else:
            out.append(ch)
    return "".join(out)


def _iso_instant(dt: datetime) -> str:
    """Format like Java's Instant.toString(): UTC with a 'Z' suffix, fractional seconds
    omitted when zero, else 3 or 6 digits. Same convention as services.iso_instant."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    micros = dt.microsecond
    if micros == 0:
        return base + "Z"
    if micros % 1000 == 0:
        return "%s.%03dZ" % (base, micros // 1000)
    return "%s.%06dZ" % (base, micros)


def round2(v: float) -> float:
    """Math.round(v * 100) / 100 — half-up rounding, as Java (not Python's banker's)."""
    return math.floor(v * 100.0 + 0.5) / 100.0


def _serialize_candidates(candidates: List[CandidateAction]) -> str:
    """The candidates_json audit column: the candidate list as Jackson would write the
    CandidateAction records (camelCase keys, explicit nulls). Never fails; "[]" on empty."""
    return json.dumps([{
        "action": c.action.value,
        "problemKey": c.problem_key,
        "conceptCode": c.concept_code,
        "difficulty": c.difficulty,
        "reason": c.reason,
        "expectedValue": c.expected_value,
        "predictedSuccess": c.predicted_success,
    } for c in candidates])


# --------------------------------------------------------------------------
# OutcomeRewardService
# --------------------------------------------------------------------------

class OutcomeRewardService:
    """Closes the Jev learning loop (docs/jev.md §reward) — port of
    recommendation.OutcomeRewardService. Implements the ingestion.RewardSink protocol:
    the FIRST outcome event on a recommended problem inside the attribution window
    becomes that recommendation's reward, and the LinUCB arm of its action type is
    updated with the stored context.

        quality = 1.0 accepted unassisted | 0.6 accepted with hint
                | 0.3 accepted after viewing the solution | 0 otherwise
        growth  = (clamp((ability_after - ability_at_decision) / growth_scale, -1, 1) + 1) / 2
        reward  = quality_weight * quality + (1 - quality_weight) * growth    in [0,1]

    Rewards are logged for every provider (offline policy evaluation); the bandit also
    learns from decisions made by other providers, which is off-policy and documented
    as a warm-start bias in docs/jev.md.
    """

    def __init__(self, session: Session, settings: Settings, bandit_store: BanditStore):
        self.session = session
        self.settings = settings
        self.bandit_store = bandit_store

    def on_outcome(self, event: InteractionEvent, ability_after: float) -> Optional[float]:
        """RewardSink protocol: attribute one persisted event to a pending recommendation.

        Returns the reward, or None when no pending recommendation matches (reward is
        written once: the pending lookup only matches rows whose reward IS NULL).
        """
        cfg = self.settings.jev.reward
        since = event.occurred_at - timedelta(hours=cfg.attribution_window_hours)
        rec = self.session.scalar(
            select(Recommendation)
            .where(Recommendation.learner_id == event.learner_id)
            .where(Recommendation.problem_id == event.problem_id)
            .where(Recommendation.reward.is_(None))
            .where(Recommendation.created_at >= since)
            .order_by(Recommendation.created_at.desc())
            .limit(1))
        if rec is None:
            return None
        reward = self.reward(event,
                             rec.ability_at_decision
                             if rec.ability_at_decision is not None else ability_after,
                             ability_after, cfg)
        rec.reward = reward
        rec.rewarded_at = datetime.now(timezone.utc)
        rec.reward_event_id = event.event_id
        self.session.flush()

        x = self._context(rec)
        if x is not None:
            self.bandit_store.update(rec.action.value, x, reward)
        logger.info("Reward %.3f for recommendation %s (action=%s provider=%s)",
                    reward, rec.id, rec.action.value, rec.provider)
        return reward

    @staticmethod
    def reward(event: InteractionEvent, ability_before: float, ability_after: float,
               cfg) -> float:
        """The reward formula (port of OutcomeRewardService.reward). cfg is
        settings.jev.reward (quality_weight, growth_scale)."""
        if not event.result.is_correct:
            quality = 0.0
        elif event.solution_viewed:
            quality = 0.3
        elif event.hint_used:
            quality = 0.6
        else:
            quality = 1.0
        delta = max(-1.0, min(1.0, (ability_after - ability_before) / cfg.growth_scale))
        growth = (delta + 1.0) / 2.0
        w = cfg.quality_weight
        return w * quality + (1.0 - w) * growth

    def _context(self, rec: Recommendation) -> Optional[List[float]]:
        """The stored bandit feature vector, or None when the recommendation has no
        context_json (e.g. a FEEDBACK_REQUEST without a problem) or it is unreadable —
        in both cases the reward is still written but no arm is updated (Java parity)."""
        if rec.context_json is None:
            return None
        try:
            return [float(v) for v in json.loads(rec.context_json)]
        except (ValueError, TypeError):
            logger.warning("Unreadable context for recommendation %s", rec.id)
            return None

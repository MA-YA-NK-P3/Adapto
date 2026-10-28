"""Event ingestion pipeline (master plan §10), port of event.EventIngestionService:
validate -> dedupe -> persist -> update derived state -> attribute the outcome to a
pending recommendation (Jev reward). One transaction: the event, the state transition
and the reward are atomic.

Idempotency: a client-generated event_id that already exists returns the stored event
with duplicate=True and does NOT touch learner state again. Reusing an event_id for a
different learner or problem is a 409 conflict, checked BEFORE any learner is created.
"""
from __future__ import annotations

import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .domain import InteractionEvent, InteractionResult, Learner, Problem
from .errors import BusinessRuleError, ConflictError, ValidationError
from .state import LearnerStateService


class RewardSink(Protocol):
    """Implemented by decision.OutcomeRewardService (wired by the container)."""
    def on_outcome(self, event: InteractionEvent, ability_after: float) -> None:
        ...


@dataclass(frozen=True)
class InteractionEventRequest:
    """Event schema v1 (master plan §8), port of event.api.dto.InteractionEventRequest."""
    user_id: str
    problem_id: str
    timestamp: datetime
    attempt_number: int
    result: InteractionResult
    event_id: Optional[uuidlib.UUID] = None
    time_spent_seconds: Optional[int] = None
    hint_used: bool = False
    solution_viewed: bool = False
    confidence: Optional[int] = None
    perceived_difficulty: Optional[int] = None
    blocker: Optional[str] = None
    session_id: Optional[str] = None


@dataclass(frozen=True)
class IngestionOutcome:
    event: InteractionEvent
    duplicate: bool   # True when the event_id was already stored (no state update happened)


def parse_timestamp(raw) -> datetime:
    """Accept a datetime or an ISO-8601 string (a trailing 'Z' means UTC)."""
    if isinstance(raw, datetime):
        ts = raw
    elif isinstance(raw, str):
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        raise ValidationError("timestamp must be an ISO-8601 string")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def validate_request(req: InteractionEventRequest) -> None:
    """The Bean Validation rules of the Java DTO, enforced at the service boundary."""
    if not req.user_id or not req.user_id.strip() or len(req.user_id) > 64:
        raise ValidationError("userId is required and must be at most 64 characters")
    if not req.problem_id or not req.problem_id.strip():
        raise ValidationError("problemId is required")
    if req.timestamp is None:
        raise ValidationError("timestamp is required")
    if req.attempt_number is None or req.attempt_number < 1:
        raise ValidationError("attemptNumber must be >= 1")
    if not isinstance(req.result, InteractionResult):
        raise ValidationError("result must be one of " + ", ".join(r.name for r in InteractionResult))
    if req.time_spent_seconds is not None and req.time_spent_seconds < 0:
        raise ValidationError("timeSpentSeconds must be >= 0")
    for value, name in ((req.confidence, "confidence"), (req.perceived_difficulty, "perceivedDifficulty")):
        if value is not None and not (1 <= value <= 5):
            raise ValidationError(name + " must be between 1 and 5")
    if req.blocker is not None and len(req.blocker) > 100:
        raise ValidationError("blocker must be at most 100 characters")
    if req.session_id is not None and len(req.session_id) > 64:
        raise ValidationError("sessionId must be at most 64 characters")


class EventIngestionService:
    def __init__(self, session: Session, settings: Settings,
                 state_service: Optional[LearnerStateService] = None,
                 reward_sink: Optional[RewardSink] = None):
        self.session = session
        self.settings = settings
        self.state = state_service or LearnerStateService(session, settings)
        self.reward_sink = reward_sink

    def ingest(self, request: InteractionEventRequest) -> IngestionOutcome:
        """Validate, dedupe, persist, update state, attribute reward. One transaction."""
        validate_request(request)

        problem = self.session.scalar(
            select(Problem).where(
                (Problem.problem_key == request.problem_id) | (Problem.slug == request.problem_id)
            )
        )
        if problem is None:
            raise BusinessRuleError(
                f"Unknown problemId '{request.problem_id}': register the problem first")

        event_id = request.event_id or uuidlib.uuid4()
        prior = self.session.scalar(
            select(InteractionEvent).where(InteractionEvent.event_id == event_id))
        if prior is not None:
            if (prior.learner.external_ref != request.user_id
                    or prior.problem.problem_key != request.problem_id):
                raise ConflictError(
                    f"eventId {event_id} is already recorded for a different learner or problem")
            return IngestionOutcome(prior, True)

        # Auto-provision learners on first sight — the extension only knows an opaque id (§22).
        learner = self.session.scalar(
            select(Learner).where(Learner.external_ref == request.user_id))
        if learner is None:
            learner = Learner(external_ref=request.user_id)
            self.session.add(learner)
            self.session.flush()

        event = InteractionEvent(
            event_id=event_id, learner_id=learner.id, problem_id=problem.id,
            occurred_at=request.timestamp, attempt_number=request.attempt_number,
            result=request.result, time_spent_seconds=request.time_spent_seconds,
            hint_used=request.hint_used, solution_viewed=request.solution_viewed,
            confidence=request.confidence, perceived_difficulty=request.perceived_difficulty,
            blocker=request.blocker, session_id=request.session_id)
        self.session.add(event)
        self.session.flush()

        self.state.apply_event(learner, problem, event)
        if self.reward_sink is not None:
            ability = self.state.get_or_create_state(learner).ability
            self.reward_sink.on_outcome(event, ability)
        return IngestionOutcome(event, False)

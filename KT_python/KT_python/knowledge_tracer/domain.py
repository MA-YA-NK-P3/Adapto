"""Entities — the equivalent of the JPA domain model (docs/domain-model.md).

One SQLAlchemy model per table produced by Flyway migrations V1-V6. Column classes
(raw observation / self-report / reference data / model state / decision) are noted
per table in docs/domain-model.md; the same grouping applies here.

Every column comment names its Java counterpart.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StaticDifficulty(enum.Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class InteractionResult(enum.Enum):
    """Only ACCEPTED counts as correct."""
    ACCEPTED = "ACCEPTED"
    WRONG_ANSWER = "WRONG_ANSWER"
    TIME_LIMIT_EXCEEDED = "TIME_LIMIT_EXCEEDED"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    ABANDONED = "ABANDONED"

    @property
    def is_correct(self) -> bool:
        return self is InteractionResult.ACCEPTED


class ActionType(enum.Enum):
    """The bounded action set a recommendation may choose from."""
    CALIBRATION_PROBLEM = "CALIBRATION_PROBLEM"
    EASIER_PROBLEM = "EASIER_PROBLEM"
    SAME_LEVEL_PROBLEM = "SAME_LEVEL_PROBLEM"
    HARDER_PROBLEM = "HARDER_PROBLEM"
    PREREQUISITE_REVIEW = "PREREQUISITE_REVIEW"
    RETRY_PROBLEM = "RETRY_PROBLEM"
    SPACED_REVIEW = "SPACED_REVIEW"
    FEEDBACK_REQUEST = "FEEDBACK_REQUEST"


class Learner(Base):
    """learners — identity. Pseudonymous: surrogate UUID plus an opaque external_ref."""
    __tablename__ = "learners"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    external_ref: Mapped[str] = mapped_column(sa.String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)


class Problem(Base):
    """problems — reference metadata AND the IRT item calibration (merged entity)."""
    __tablename__ = "problems"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    problem_key: Mapped[str] = mapped_column(sa.String(32), unique=True)   # public id, e.g. "LC21"
    title: Mapped[str] = mapped_column(sa.String(255))
    url: Mapped[Optional[str]] = mapped_column(sa.String(512))
    static_difficulty: Mapped[StaticDifficulty] = mapped_column(
        sa.Enum(StaticDifficulty, native_enum=False))                     # prior only
    source: Mapped[str] = mapped_column(sa.String(32), default="LEETCODE")
    frontend_id: Mapped[Optional[str]] = mapped_column(sa.String(32))
    slug: Mapped[Optional[str]] = mapped_column(sa.String(160))
    acceptance_rate: Mapped[Optional[float]] = mapped_column(sa.Float)
    likes: Mapped[Optional[int]] = mapped_column(sa.Integer)
    paid_only: Mapped[bool] = mapped_column(sa.Boolean, default=False)    # never recommended
    topic_tags: Mapped[Optional[str]] = mapped_column(sa.String(1024))    # comma-separated, as observed
    # IRT prior from label + acceptance rate; fixed after creation.
    prior_difficulty: Mapped[float] = mapped_column(sa.Float)
    # Model state, overwritten in place on every recalibration:
    estimated_difficulty: Mapped[float] = mapped_column(sa.Float)
    difficulty_uncertainty: Mapped[float] = mapped_column(sa.Float, default=1.5)
    sample_size: Mapped[int] = mapped_column(sa.Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    __table_args__ = (sa.Index("ix_problems_paid_difficulty", "paid_only", "estimated_difficulty"),)


class Concept(Base):
    """concepts — knowledge-graph node. `code` (e.g. LINKED_LIST) is the public id."""
    __tablename__ = "concepts"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(sa.String(64), unique=True)
    name: Mapped[str] = mapped_column(sa.String(128))
    description: Mapped[Optional[str]] = mapped_column(sa.String(1024))


class ProblemConcept(Base):
    """problem_concepts — join with one attribute, weight (how strongly the problem
    exercises the concept, 0-1)."""
    __tablename__ = "problem_concepts"

    problem_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True)
    concept_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True)
    weight: Mapped[float] = mapped_column(sa.Float, default=1.0)

    concept: Mapped[Concept] = relationship(lazy="joined")

    __table_args__ = (sa.Index("ix_problem_concepts_concept", "concept_id"),)


class Prerequisite(Base):
    """prerequisites — directed edge "concept_id requires prerequisite_id"."""
    __tablename__ = "prerequisites"

    concept_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True)
    prerequisite_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("concepts.id", ondelete="CASCADE"), primary_key=True)

    __table_args__ = (sa.CheckConstraint("concept_id <> prerequisite_id"),)


class InteractionEvent(Base):
    """interaction_events — raw observations + self-reports (event schema v1, §8).

    Append-only: no update or delete path exists. `event_id` is the client-generated
    idempotency key, globally unique (not per learner).
    """
    __tablename__ = "interaction_events"
    CURRENT_SCHEMA_VERSION = 1

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, unique=True)
    learner_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("learners.id"))
    problem_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("problems.id"))
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))  # client clock, as sent
    attempt_number: Mapped[int] = mapped_column(sa.Integer)                    # client-reported, >= 1
    result: Mapped[InteractionResult] = mapped_column(sa.Enum(InteractionResult, native_enum=False))
    time_spent_seconds: Mapped[Optional[int]] = mapped_column(sa.Integer)
    hint_used: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    solution_viewed: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    confidence: Mapped[Optional[int]] = mapped_column(sa.Integer)              # self-report 1-5
    perceived_difficulty: Mapped[Optional[int]] = mapped_column(sa.Integer)    # self-report 1-5
    blocker: Mapped[Optional[str]] = mapped_column(sa.String(100))             # self-report, free text
    session_id: Mapped[Optional[str]] = mapped_column(sa.String(64))
    schema_version: Mapped[int] = mapped_column(sa.Integer, default=CURRENT_SCHEMA_VERSION)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    learner: Mapped[Learner] = relationship(lazy="joined")
    problem: Mapped[Problem] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint("attempt_number >= 1"),
        sa.Index("ix_events_learner_time", "learner_id", sa.text("occurred_at desc")),
        sa.Index("ix_events_problem_time", "problem_id", sa.text("occurred_at desc")),
    )


class LearnerState(Base):
    """learner_states — current estimate only: no snapshots, no history, no version."""
    __tablename__ = "learner_states"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("learners.id"), primary_key=True)
    ability: Mapped[float] = mapped_column(sa.Float)
    uncertainty: Mapped[float] = mapped_column(sa.Float)
    total_events: Mapped[int] = mapped_column(sa.Integer, default=0)
    calibrated: Mapped[bool] = mapped_column(sa.Boolean, default=False)  # set once, never reset
    version: Mapped[int] = mapped_column(sa.Integer, default=0)          # optimistic-lock counter
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    __mapper_args__ = {"version_id_col": version}


class ConceptMastery(Base):
    """concept_masteries — Beta-Bernoulli state per (learner, concept)."""
    __tablename__ = "concept_masteries"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("learners.id"))
    concept_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("concepts.id"))
    alpha: Mapped[float] = mapped_column(sa.Float)
    beta: Mapped[float] = mapped_column(sa.Float)
    mastery: Mapped[float] = mapped_column(sa.Float)          # alpha / (alpha + beta)
    evidence_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    concept: Mapped[Concept] = relationship(lazy="joined")

    __table_args__ = (
        sa.UniqueConstraint("learner_id", "concept_id"),
        sa.CheckConstraint("mastery >= 0 AND mastery <= 1"),
    )

    def uncertainty(self) -> float:
        """Posterior sd of the Beta distribution; computed, not stored."""
        a, b = self.alpha, self.beta
        return (a * b / ((a + b) ** 2 * (a + b + 1.0))) ** 0.5


class Recommendation(Base):
    """recommendations — one audit row per decision (decision + issued action + outcome)."""
    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("learners.id"))
    # Issued recommendation:
    action: Mapped[ActionType] = mapped_column(sa.Enum(ActionType, native_enum=False))
    problem_id: Mapped[Optional[uuid.UUID]] = mapped_column(sa.Uuid, sa.ForeignKey("problems.id"))
    concept_id: Mapped[Optional[uuid.UUID]] = mapped_column(sa.Uuid, sa.ForeignKey("concepts.id"))
    difficulty: Mapped[Optional[float]] = mapped_column(sa.Float)
    reason: Mapped[Optional[str]] = mapped_column(sa.String(1024))   # the candidate's reason
    confidence: Mapped[Optional[float]] = mapped_column(sa.Float)
    expected_value: Mapped[Optional[float]] = mapped_column(sa.Float)
    predicted_success: Mapped[Optional[float]] = mapped_column(sa.Float)
    # Decision audit:
    decided_by: Mapped[str] = mapped_column(sa.String(16))           # JEV | FALLBACK
    fallback_used: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    provider: Mapped[Optional[str]] = mapped_column(sa.String(32))   # deterministic|bandit|http; null on fallback
    candidates_json: Mapped[str] = mapped_column(sa.Text, default="[]")
    context_json: Mapped[Optional[str]] = mapped_column(sa.Text)     # bandit feature vector
    ability_at_decision: Mapped[Optional[float]] = mapped_column(sa.Float)
    # Observed outcome (written once, by the first matching event):
    reward: Mapped[Optional[float]] = mapped_column(sa.Float)
    rewarded_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True))
    reward_event_id: Mapped[Optional[uuid.UUID]] = mapped_column(sa.Uuid)  # no FK, by design
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    problem: Mapped[Optional[Problem]] = relationship(lazy="joined")
    concept: Mapped[Optional[Concept]] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint("reward >= 0 AND reward <= 1"),
        sa.Index("ix_recs_learner_time", "learner_id", sa.text("created_at desc")),
        sa.Index("ix_recs_pending", "learner_id", "problem_id", sa.text("created_at desc"),
                 sqlite_where=sa.text("reward is null")),
    )


class Feedback(Base):
    """feedback — self-report, write-only for now (no model or export reads it)."""
    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    learner_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, sa.ForeignKey("learners.id"))
    problem_id: Mapped[Optional[uuid.UUID]] = mapped_column(sa.Uuid, sa.ForeignKey("problems.id"))
    recommendation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.Uuid, sa.ForeignKey("recommendations.id"))
    answers_json: Mapped[str] = mapped_column(sa.Text)   # JSON array of {question, answer}
    free_text: Mapped[Optional[str]] = mapped_column(sa.String(2048))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    learner: Mapped[Learner] = relationship(lazy="joined")
    problem: Mapped[Optional[Problem]] = relationship(lazy="joined")
    recommendation: Mapped[Optional[Recommendation]] = relationship(lazy="joined")


class BanditArm(Base):
    """jev_bandit_arms — LinUCB sufficient statistics per action type (global, not per learner)."""
    __tablename__ = "jev_bandit_arms"

    arm: Mapped[str] = mapped_column(sa.String(64), primary_key=True)  # an ActionType name
    dim: Mapped[int] = mapped_column(sa.Integer)
    feature_version: Mapped[int] = mapped_column(sa.Integer)
    a_matrix: Mapped[str] = mapped_column(sa.Text)   # JSON: dim x dim
    b_vector: Mapped[str] = mapped_column(sa.Text)   # JSON: dim
    pulls: Mapped[int] = mapped_column(sa.Integer, default=0)
    reward_sum: Mapped[float] = mapped_column(sa.Float, default=0.0)
    version: Mapped[int] = mapped_column(sa.Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

    __mapper_args__ = {"version_id_col": version}

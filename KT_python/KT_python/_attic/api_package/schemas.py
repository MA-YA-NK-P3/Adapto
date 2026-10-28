"""Request schemas for the HTTP API — ports of the *.api.dto records.

Pydantic v2 models carry the Bean Validation rules of the Java DTOs:
@NotBlank -> min_length=1, @Size -> max_length, @Min/@Max -> ge/le,
@Pattern -> pattern, @NotEmpty -> min_length=1 on lists. Field aliases are the
camelCase JSON names; populate_by_name also accepts the snake_case attribute names.

Responses are built as plain dicts in the route modules (NextActionResponse and
HistoryEntry already provide to_dict() with the exact camelCase shape from
docs/api-flow.md); the only shared response body is the ApiError document built in
errors.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..domain import InteractionResult, StaticDifficulty


def iso_instant(dt: datetime) -> str:
    """Format like Java's Instant.toString(): UTC with a 'Z' suffix, fractional
    seconds omitted when zero else 3 or 6 digits (our precision is microseconds).
    Naive datetimes are taken as UTC (SQLite drops the offset on read)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    micros = dt.microsecond
    if micros == 0:
        return base + "Z"
    if micros % 1000 == 0:
        return f"{base}.{micros // 1000:03d}Z"
    return f"{base}.{micros:06d}Z"


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class InteractionEventIn(_Model):
    """event.api.dto.InteractionEventRequest (event schema v1, master plan §8)."""
    user_id: str = Field(alias="userId", min_length=1, max_length=64)
    problem_id: str = Field(alias="problemId", min_length=1, max_length=32)
    event_id: Optional[uuid.UUID] = Field(default=None, alias="eventId")
    timestamp: datetime
    attempt_number: int = Field(alias="attemptNumber", ge=1)
    result: InteractionResult
    time_spent_seconds: Optional[int] = Field(default=None, alias="timeSpentSeconds", ge=0)
    hint_used: bool = Field(default=False, alias="hintUsed")
    solution_viewed: bool = Field(default=False, alias="solutionViewed")
    confidence: Optional[int] = Field(default=None, ge=1, le=5)
    perceived_difficulty: Optional[int] = Field(default=None, alias="perceivedDifficulty", ge=1, le=5)
    blocker: Optional[str] = Field(default=None, max_length=100)
    session_id: Optional[str] = Field(default=None, alias="sessionId", max_length=64)


class CreateLearnerIn(_Model):
    """learner.api.dto.CreateLearnerRequest."""
    external_ref: str = Field(alias="externalRef", min_length=1, max_length=64)


class ConceptTagIn(_Model):
    """CreateProblemRequest.ConceptTag."""
    concept_code: str = Field(alias="conceptCode", min_length=1)
    weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class CreateProblemIn(_Model):
    """problem.api.dto.CreateProblemRequest."""
    problem_key: str = Field(alias="problemKey", min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=255)
    url: Optional[str] = Field(default=None, max_length=512)
    static_difficulty: StaticDifficulty = Field(alias="staticDifficulty")
    concepts: Optional[List[ConceptTagIn]] = None
    acceptance_rate: Optional[float] = Field(default=None, alias="acceptanceRate", ge=0.0, le=1.0)
    paid_only: Optional[bool] = Field(default=None, alias="paidOnly")
    topic_tags: Optional[List[str]] = Field(default=None, alias="topicTags", max_length=30)


class CreateConceptIn(_Model):
    """concept.api.dto.CreateConceptRequest."""
    code: str = Field(min_length=1, max_length=64, pattern="^[A-Z0-9_]+$")
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=1024)


class AddPrerequisiteIn(_Model):
    """concept.api.dto.AddPrerequisiteRequest."""
    prerequisite_code: str = Field(alias="prerequisiteCode", min_length=1)


class AnswerIn(_Model):
    """FeedbackRequest.Answer."""
    question: str = Field(min_length=1, max_length=512)
    answer: str = Field(min_length=1, max_length=1024)


class FeedbackIn(_Model):
    """feedback.api.dto.FeedbackRequest."""
    user_id: str = Field(alias="userId", min_length=1, max_length=64)
    problem_id: Optional[str] = Field(default=None, alias="problemId", max_length=32)
    recommendation_id: Optional[uuid.UUID] = Field(default=None, alias="recommendationId")
    answers: List[AnswerIn] = Field(min_length=1)
    free_text: Optional[str] = Field(default=None, alias="freeText", max_length=2048)

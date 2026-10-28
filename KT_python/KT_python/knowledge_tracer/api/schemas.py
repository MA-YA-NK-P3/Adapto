"""Request DTOs — pydantic ports of the *.api.dto request records, Bean Validation
included (@NotBlank/@Size/@Min/@Max/@Pattern), so 400s match the Java service.

@NotBlank is at least one non-whitespace character: min_length plus the \\S pattern
(pydantic patterns use re.search). Field aliases are the camelCase JSON names; error
locations report the alias, matching Spring's field errors.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

from ..domain import InteractionResult, StaticDifficulty

#: @NotBlank + @Size columns, mirroring the Java DTO records.
NotBlank64 = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"\S")]
NotBlank32 = Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=r"\S")]
NotBlank128 = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"\S")]
NotBlank255 = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"\S")]
NotBlank512 = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"\S")]
NotBlank1024 = Annotated[str, StringConstraints(min_length=1, max_length=1024, pattern=r"\S")]


class InteractionEventRequestIn(BaseModel):
    """event.api.dto.InteractionEventRequest (event schema v1, master plan §8)."""
    user_id: NotBlank64 = Field(alias="userId")
    problem_id: NotBlank32 = Field(alias="problemId")
    event_id: Optional[uuid.UUID] = Field(default=None, alias="eventId")
    timestamp: datetime
    attempt_number: int = Field(alias="attemptNumber", ge=1)
    result: InteractionResult
    time_spent_seconds: Optional[int] = Field(default=None, alias="timeSpentSeconds", ge=0)
    hint_used: bool = Field(default=False, alias="hintUsed")
    solution_viewed: bool = Field(default=False, alias="solutionViewed")
    confidence: Optional[int] = Field(default=None, ge=1, le=5)
    perceived_difficulty: Optional[int] = Field(default=None, alias="perceivedDifficulty",
                                                ge=1, le=5)
    blocker: Optional[str] = Field(default=None, max_length=100)
    session_id: Optional[str] = Field(default=None, alias="sessionId", max_length=64)


class CreateLearnerRequestIn(BaseModel):
    """learner.api.dto.CreateLearnerRequest."""
    external_ref: NotBlank64 = Field(alias="externalRef")


class ConceptTagIn(BaseModel):
    """CreateProblemRequest.ConceptTag (weight defaults to 1.0 in the service)."""
    concept_code: NotBlank64 = Field(alias="conceptCode")
    weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class CreateProblemRequestIn(BaseModel):
    """problem.api.dto.CreateProblemRequest."""
    problem_key: NotBlank32 = Field(alias="problemKey")
    title: NotBlank255
    url: Optional[str] = Field(default=None, max_length=512)
    static_difficulty: StaticDifficulty = Field(alias="staticDifficulty")
    concepts: Optional[List[ConceptTagIn]] = None
    acceptance_rate: Optional[float] = Field(default=None, alias="acceptanceRate",
                                             ge=0.0, le=1.0)
    paid_only: Optional[bool] = Field(default=None, alias="paidOnly")
    topic_tags: Optional[List[Annotated[str, StringConstraints(max_length=64)]]] = Field(
        default=None, alias="topicTags", max_length=30)


class CreateConceptRequestIn(BaseModel):
    """concept.api.dto.CreateConceptRequest (code must be UPPER_SNAKE_CASE)."""
    code: Annotated[str, StringConstraints(min_length=1, max_length=64,
                                           pattern=r"^[A-Z0-9_]+$")]
    name: NotBlank128
    description: Optional[str] = Field(default=None, max_length=1024)


class AddPrerequisiteRequestIn(BaseModel):
    """concept.api.dto.AddPrerequisiteRequest."""
    prerequisite_code: NotBlank64 = Field(alias="prerequisiteCode")


class AnswerIn(BaseModel):
    """FeedbackRequest.Answer."""
    question: NotBlank512
    answer: NotBlank1024


class FeedbackRequestIn(BaseModel):
    """feedback.api.dto.FeedbackRequest."""
    user_id: NotBlank64 = Field(alias="userId")
    problem_id: Optional[str] = Field(default=None, alias="problemId", max_length=32)
    recommendation_id: Optional[uuid.UUID] = Field(default=None, alias="recommendationId")
    answers: List[AnswerIn] = Field(min_length=1)
    free_text: Optional[str] = Field(default=None, alias="freeText", max_length=2048)

"""CRUD-style application services and their DTOs (docs/api-flow.md §5.2-5.3, §5.6-5.14).

Ports of the four Spring application services that back the catalog/learner/feedback
controllers:

  * learner.LearnerService   -> LearnerService   (createIfAbsent, stateView, history)
  * problem.ProblemService   -> ProblemService   (create, list, getByKey)
  * concept.ConceptService   -> ConceptService   (create, list, getByCode, addPrerequisite)
  * feedback.FeedbackService -> FeedbackService  (submit)

The api.dto request/response records they consume and produce (CreateLearnerRequest,
LearnerResponse, LearnerStateResponse, LearnerHistoryEntry, CreateProblemRequest,
ProblemResponse, CreateConceptRequest, ConceptResponse, AddPrerequisiteRequest,
FeedbackRequest/Answer, FeedbackResponse) are plain dataclasses here; to_dict() renders
the exact camelCase JSON shape the Java records serialize to (docs/api-flow.md).

The Java services are @Transactional; here the container's session_scope() provides the
transaction (commit on success, rollback on error), as the foundation services do. The
Bean Validation constraints of the Java DTO records are mirrored at the service boundary
(the style of event.EventIngestionService's validate_request), so the services are safe
without the HTTP layer; the API layer's pydantic models enforce the same rules first and
produce the 400 responses.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import feedback as feedback_foundation
from .config import Settings
from .domain import (Concept, InteractionEvent, Learner, Prerequisite,
                     Problem, ProblemConcept, StaticDifficulty)
from .errors import (BusinessRuleError, ConflictError, NotFoundError, ValidationError)
from .irt import IrtService
from .knowledge_graph import KnowledgeGraphService
from .recommendation import round2
from .state import LearnerStateService

logger = logging.getLogger(__name__)

#: CreateConceptRequest's @Pattern(regexp = "^[A-Z0-9_]+$", message = "must be UPPER_SNAKE_CASE").
UPPER_SNAKE_CASE = re.compile(r"^[A-Z0-9_]+$")

#: Java's ProblemService.list orders by frontendId with nulls mapped to Integer.MAX_VALUE.
_NULL_FRONTEND_ID = 2 ** 31 - 1


def iso_instant(dt: datetime) -> str:
    """Format like Java's Instant.toString() (Jackson's ISO-8601 output): UTC with a 'Z'
    suffix, fractional seconds omitted when zero, else 3 or 6 digits (our precision is
    microseconds). Same convention as research._iso_instant."""
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


# --------------------------------------------------------------------------
# Request DTOs (the api.dto request records, at the service boundary)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ConceptTag:
    """Port of CreateProblemRequest.ConceptTag; weight defaults to 1.0 when absent."""
    concept_code: str
    weight: Optional[float] = None


@dataclass(frozen=True)
class CreateProblemRequest:
    """Port of problem.api.dto.CreateProblemRequest."""
    problem_key: str
    title: str
    url: Optional[str]
    static_difficulty: StaticDifficulty
    concepts: Optional[List[ConceptTag]] = None
    acceptance_rate: Optional[float] = None
    paid_only: Optional[bool] = None
    topic_tags: Optional[List[str]] = None


@dataclass(frozen=True)
class CreateConceptRequest:
    """Port of concept.api.dto.CreateConceptRequest."""
    code: str
    name: str
    description: Optional[str] = None


@dataclass(frozen=True)
class FeedbackAnswer:
    """Port of FeedbackRequest.Answer."""
    question: str
    answer: str


@dataclass(frozen=True)
class FeedbackRequest:
    """Port of feedback.api.dto.FeedbackRequest."""
    user_id: str
    answers: List[FeedbackAnswer]
    problem_id: Optional[str] = None
    recommendation_id: Optional[uuid.UUID] = None
    free_text: Optional[str] = None


# --------------------------------------------------------------------------
# Response DTOs (the api.dto response records; to_dict() = Jackson's output)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LearnerCreateResult:
    """Port of the LearnerService.LearnerCreateResult record."""
    learner: Learner
    created: bool


@dataclass(frozen=True)
class ConceptMasteryView:
    """Port of LearnerStateResponse.ConceptMasteryView (rounded estimates)."""
    mastery: float
    uncertainty: float
    evidence_count: int


@dataclass(frozen=True)
class LearnerStateResponse:
    """Port of learner.api.dto.LearnerStateResponse (docs/api-flow.md §5.3).

    Concept keys are the machine codes (e.g. LINKED_LIST), sorted by code like the
    Java TreeMap — unlike next-action, which uses camelCase keys.
    """
    user_id: str
    ability: float
    uncertainty: float
    calibrated: bool
    total_events: int
    concepts: Dict[str, ConceptMasteryView]

    def to_dict(self) -> Dict[str, object]:
        return {
            "userId": self.user_id,
            "ability": self.ability,
            "uncertainty": self.uncertainty,
            "calibrated": self.calibrated,
            "totalEvents": self.total_events,
            "concepts": {code: {"mastery": v.mastery, "uncertainty": v.uncertainty,
                                "evidenceCount": v.evidence_count}
                         for code, v in self.concepts.items()},
        }


@dataclass(frozen=True)
class LearnerHistoryEntry:
    """One raw event as stored (port of learner.api.dto.LearnerHistoryEntry, §5.6)."""
    event_id: uuid.UUID
    problem_id: str
    timestamp: datetime
    attempt_number: int
    result: str
    time_spent_seconds: Optional[int]
    hint_used: bool
    solution_viewed: bool
    confidence: Optional[int]
    perceived_difficulty: Optional[int]
    blocker: Optional[str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "eventId": str(self.event_id),
            "problemId": self.problem_id,
            "timestamp": iso_instant(self.timestamp),
            "attemptNumber": self.attempt_number,
            "result": self.result,
            "timeSpentSeconds": self.time_spent_seconds,
            "hintUsed": self.hint_used,
            "solutionViewed": self.solution_viewed,
            "confidence": self.confidence,
            "perceivedDifficulty": self.perceived_difficulty,
            "blocker": self.blocker,
        }


@dataclass(frozen=True)
class ConceptWeight:
    """Port of ProblemResponse.ConceptWeight (tags ordered primary-first)."""
    code: str
    weight: float


@dataclass(frozen=True)
class ProblemResponse:
    """Port of problem.api.dto.ProblemResponse (docs/api-flow.md §5.7).

    estimated_difficulty/difficulty_uncertainty are model estimates (calibrated),
    static_difficulty is the platform's prior label — both are exposed so callers can
    see the difference (master plan §2).
    """
    problem_id: str
    title: str
    url: Optional[str]
    static_difficulty: str
    estimated_difficulty: float
    difficulty_uncertainty: float
    sample_size: int
    source: str
    concepts: List[str]
    concept_tags: List[ConceptWeight]
    prior_difficulty: float
    frontend_id: Optional[int]
    slug: Optional[str]
    acceptance_rate: Optional[float]
    likes: Optional[int]
    paid_only: bool
    topic_tags: List[str]

    def to_dict(self) -> Dict[str, object]:
        return {
            "problemId": self.problem_id,
            "title": self.title,
            "url": self.url,
            "staticDifficulty": self.static_difficulty,
            "estimatedDifficulty": self.estimated_difficulty,
            "difficultyUncertainty": self.difficulty_uncertainty,
            "sampleSize": self.sample_size,
            "source": self.source,
            "concepts": list(self.concepts),
            "conceptTags": [{"code": t.code, "weight": t.weight} for t in self.concept_tags],
            "priorDifficulty": self.prior_difficulty,
            "frontendId": self.frontend_id,
            "slug": self.slug,
            "acceptanceRate": self.acceptance_rate,
            "likes": self.likes,
            "paidOnly": self.paid_only,
            "topicTags": list(self.topic_tags),
        }


@dataclass(frozen=True)
class ConceptResponse:
    """Port of concept.api.dto.ConceptResponse (docs/api-flow.md §5.10-5.13)."""
    code: str
    name: str
    description: Optional[str]
    direct_prerequisites: List[str] = field(default_factory=list)
    all_prerequisites: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "directPrerequisites": list(self.direct_prerequisites),
            "allPrerequisites": list(self.all_prerequisites),
        }


# --------------------------------------------------------------------------
# LearnerService
# --------------------------------------------------------------------------

class LearnerService:
    """Port of learner.LearnerService (docs/api-flow.md §5.2, §5.3, §5.6).

    The Java class injects LearnerRepository, LearnerStateService and
    InteractionEventRepository; the replica collapses the repositories into the
    SQLAlchemy session, as the other foundation services do.
    """

    def __init__(self, session: Session, settings: Settings,
                 state_service: LearnerStateService):
        self.session = session
        self.settings = settings
        self.state = state_service

    def create_if_absent(self, external_ref: str) -> LearnerCreateResult:
        """Idempotent create: returns the existing learner when the ref is registered."""
        if not external_ref or not external_ref.strip() or len(external_ref) > 64:
            raise ValidationError(
                "externalRef is required and must be at most 64 characters")
        learner = self.session.scalar(
            select(Learner).where(Learner.external_ref == external_ref))
        if learner is not None:
            return LearnerCreateResult(learner, False)
        learner = Learner(external_ref=external_ref)
        self.session.add(learner)
        self.session.flush()
        return LearnerCreateResult(learner, True)

    def get_by_external_ref(self, external_ref: str) -> Learner:
        """A learner by external reference, or NotFoundError (404) — never
        auto-provisioned here (Java getByExternalRef)."""
        learner = self.session.scalar(
            select(Learner).where(Learner.external_ref == external_ref))
        if learner is None:
            raise NotFoundError("Learner not found: " + external_ref)
        return learner

    def state_view(self, external_ref: str) -> LearnerStateResponse:
        """Current ESTIMATED state (never a permanent label). Inserts learner_states
        with the cold-start prior on first read (getOrCreateState)."""
        learner = self.get_by_external_ref(external_ref)
        state = self.state.get_or_create_state(learner)
        concepts = dict(sorted(
            (m.concept.code,
             ConceptMasteryView(round2(m.mastery), round2(m.uncertainty()), m.evidence_count))
            for m in self.state.masteries_of(learner)))
        return LearnerStateResponse(
            external_ref, round2(state.ability), round2(state.uncertainty),
            state.calibrated, state.total_events, concepts)

    def history(self, external_ref: str, limit: int) -> List[LearnerHistoryEntry]:
        """Stored events, newest first (observations, not estimates).

        Java: findByLearnerIdOrderByOccurredAtDesc with PageRequest.of(0,
        Math.min(limit, 200)) — the controller's @Min(1) guarantees limit >= 1.
        """
        learner = self.get_by_external_ref(external_ref)
        if limit < 1:
            raise ValidationError("limit must be >= 1")
        capped = min(limit, 200)
        events = self.session.scalars(
            select(InteractionEvent)
            .where(InteractionEvent.learner_id == learner.id)
            .order_by(InteractionEvent.occurred_at.desc())
            .limit(capped))
        return [LearnerHistoryEntry(
                    e.event_id, e.problem.problem_key, e.occurred_at, e.attempt_number,
                    e.result.name, e.time_spent_seconds, e.hint_used, e.solution_viewed,
                    e.confidence, e.perceived_difficulty, e.blocker)
                for e in events]


# --------------------------------------------------------------------------
# ProblemService
# --------------------------------------------------------------------------

class ProblemService:
    """Port of problem.ProblemService (docs/api-flow.md §5.7-5.9).

    The Java class injects ProblemRepository, ConceptRepository,
    ProblemConceptRepository, IrtService and KnowledgeTracerProperties; the replica
    collapses the repositories into the SQLAlchemy session.
    """

    def __init__(self, session: Session, settings: Settings, irt_service: IrtService):
        self.session = session
        self.settings = settings
        self.irt = irt_service

    def create(self, request: CreateProblemRequest) -> ProblemResponse:
        """Register a problem. The estimated difficulty starts at the prior (static
        label, shifted by acceptance rate when supplied — docs/irt.md) and is
        re-calibrated by evidence. One transaction: an unknown concept rolls the
        problem insert back, exactly like the Java service."""
        self._validate(request)
        if self.session.scalar(
                select(Problem.id).where(Problem.problem_key == request.problem_key)) is not None:
            raise ConflictError("Problem already exists: " + request.problem_key)
        prior = self.irt.prior(request.static_difficulty, request.acceptance_rate)
        problem = Problem(
            problem_key=request.problem_key, title=request.title, url=request.url,
            static_difficulty=request.static_difficulty,
            prior_difficulty=prior, estimated_difficulty=prior,
            difficulty_uncertainty=self.settings.irt.difficulty_prior_sd, sample_size=0,
            acceptance_rate=request.acceptance_rate,
            paid_only=request.paid_only is True,  # Java: Boolean.TRUE.equals(paidOnly)
            topic_tags=(", ".join(request.topic_tags)
                        if request.topic_tags else None))
        self.session.add(problem)
        self.session.flush()

        tags: List[Tuple[str, float]] = []
        if request.concepts is not None:
            for tag in request.concepts:
                concept = self.session.scalar(
                    select(Concept).where(Concept.code == tag.concept_code))
                if concept is None:
                    raise NotFoundError("Concept not found: " + tag.concept_code)
                weight = tag.weight if tag.weight is not None else 1.0
                self.session.add(ProblemConcept(
                    problem_id=problem.id, concept_id=concept.id, weight=weight))
                tags.append((concept.code, weight))
        self.session.flush()
        return self.to_response(problem, tags)

    def list(self) -> List[ProblemResponse]:
        """All problems, ordered by frontendId (nulls last) then problemKey — the Java
        comparator over ProblemRepository.findAll(). No paging, as in Java."""
        problems = list(self.session.scalars(select(Problem)))
        tags: Dict[uuid.UUID, List[Tuple[str, float]]] = {}
        for pc in self.session.scalars(select(ProblemConcept)):  # findAllWithConcept
            tags.setdefault(pc.problem_id, []).append((pc.concept.code, pc.weight))
        problems.sort(key=lambda p: (_frontend_id_order(p.frontend_id), p.problem_key))
        return [self.to_response(p, tags.get(p.id, [])) for p in problems]

    def get_by_key(self, problem_key: str) -> ProblemResponse:
        problem = self.session.scalar(
            select(Problem).where((Problem.problem_key == problem_key) | (Problem.slug == problem_key)))
        if problem is None:
            raise NotFoundError("Problem not found: " + problem_key)
        links = self.session.scalars(
            select(ProblemConcept).where(ProblemConcept.problem_id == problem.id))
        return self.to_response(problem, [(pc.concept.code, pc.weight) for pc in links])

    @staticmethod
    def to_response(p: Problem, tags: List[Tuple[str, float]]) -> ProblemResponse:
        """Port of ProblemService.toResponse: conceptTags ordered by weight (primary
        first), then code; concepts the sorted distinct codes; topicTags split back
        from the stored comma-joined column."""
        weighted = sorted(tags, key=lambda t: (-t[1], t[0]))
        concepts = sorted({code for code, _ in tags})
        if p.topic_tags is None or not p.topic_tags.strip():
            topic_tags: List[str] = []
        else:
            topic_tags = [t.strip() for t in p.topic_tags.split(",") if t.strip()]
        return ProblemResponse(
            problem_id=p.problem_key, title=p.title, url=p.url,
            static_difficulty=p.static_difficulty.name,
            estimated_difficulty=round2(p.estimated_difficulty),
            difficulty_uncertainty=round2(p.difficulty_uncertainty),
            sample_size=p.sample_size, source=p.source, concepts=concepts,
            concept_tags=[ConceptWeight(code, w) for code, w in weighted],
            prior_difficulty=round2(p.prior_difficulty),
            frontend_id=_frontend_id_as_int(p.frontend_id), slug=p.slug,
            acceptance_rate=p.acceptance_rate, likes=p.likes, paid_only=p.paid_only,
            topic_tags=topic_tags)

    @staticmethod
    def _validate(request: CreateProblemRequest) -> None:
        """The Bean Validation rules of CreateProblemRequest, enforced at the service
        boundary (the API layer's pydantic model checks them first)."""
        if not request.problem_key or not request.problem_key.strip() \
                or len(request.problem_key) > 32:
            raise ValidationError("problemKey is required and must be at most 32 characters")
        if not request.title or not request.title.strip() or len(request.title) > 255:
            raise ValidationError("title is required and must be at most 255 characters")
        if request.url is not None and len(request.url) > 512:
            raise ValidationError("url must be at most 512 characters")
        if not isinstance(request.static_difficulty, StaticDifficulty):
            raise ValidationError("staticDifficulty is required (EASY, MEDIUM or HARD)")
        if request.acceptance_rate is not None \
                and not (0.0 <= request.acceptance_rate <= 1.0):
            raise ValidationError("acceptanceRate must be between 0.0 and 1.0")
        if request.topic_tags is not None:
            if len(request.topic_tags) > 30:
                raise ValidationError("topicTags must have at most 30 entries")
            if any(len(t) > 64 for t in request.topic_tags):
                raise ValidationError("topicTags entries must be at most 64 characters")
        if request.concepts is not None:
            for tag in request.concepts:
                if not tag.concept_code or not tag.concept_code.strip():
                    raise ValidationError("conceptCode is required")
                if tag.weight is not None and not (0.0 <= tag.weight <= 1.0):
                    raise ValidationError("weight must be between 0.0 and 1.0")


def _frontend_id_order(frontend_id: Optional[str]) -> int:
    """Java's comparator key: frontendId == null ? Integer.MAX_VALUE : frontendId."""
    if frontend_id is None:
        return _NULL_FRONTEND_ID
    try:
        return int(str(frontend_id))
    except ValueError:
        return _NULL_FRONTEND_ID


def _frontend_id_as_int(frontend_id: Optional[str]) -> Optional[int]:
    """The Java entity declares frontendId an Integer; the replica's column is a string
    (the catalog stores numbers), so the API renders it as a JSON number again."""
    if frontend_id is None:
        return None
    try:
        return int(str(frontend_id))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# ConceptService
# --------------------------------------------------------------------------

class ConceptService:
    """Port of concept.ConceptService (docs/api-flow.md §5.10-5.13)."""

    def __init__(self, session: Session, settings: Settings,
                 graph_service: KnowledgeGraphService):
        self.session = session
        self.settings = settings
        self.graph = graph_service

    def create(self, request: CreateConceptRequest) -> ConceptResponse:
        self._validate(request)
        if self.session.scalar(
                select(Concept.id).where(Concept.code == request.code)) is not None:
            raise ConflictError("Concept already exists: " + request.code)
        concept = Concept(code=request.code, name=request.name,
                          description=request.description)
        self.session.add(concept)
        self.session.flush()
        return self.to_response(concept)

    def list(self) -> List[ConceptResponse]:
        concepts = list(self.session.scalars(select(Concept)))
        concepts.sort(key=lambda c: c.code)
        return [self.to_response(c) for c in concepts]

    def get_by_code(self, code: str) -> ConceptResponse:
        return self.to_response(self.graph.get_by_code(code))

    def add_prerequisite(self, concept_code: str, prerequisite_code: str) -> ConceptResponse:
        """Add the edge "concept requires prerequisite". Rejects self-loops and cycles
        with 422 — traversals are cycle-safe, but the graph should stay acyclic by
        contract. Checks run in the Java order: self-loop (before any existence
        check), unknown codes (404), cycle (422), duplicate edge (409)."""
        if not concept_code or not concept_code.strip():
            raise ValidationError("code is required")
        if not prerequisite_code or not prerequisite_code.strip():
            raise ValidationError("prerequisiteCode is required")
        if concept_code == prerequisite_code:
            raise BusinessRuleError("A concept cannot be its own prerequisite")
        concept = self.graph.get_by_code(concept_code)  # 404 Concept not found: <code>
        prerequisite = self.session.scalar(
            select(Concept).where(Concept.code == prerequisite_code))
        if prerequisite is None:
            raise NotFoundError("Concept not found: " + prerequisite_code)
        if self.graph.would_create_cycle(concept.id, prerequisite.id):
            raise BusinessRuleError(
                "Adding " + concept_code + " -> " + prerequisite_code
                + " would create a cycle")
        if self.session.get(Prerequisite, (concept.id, prerequisite.id)) is not None:
            raise ConflictError("Prerequisite edge already exists")
        self.session.add(Prerequisite(concept_id=concept.id,
                                      prerequisite_id=prerequisite.id))
        self.session.flush()
        return self.to_response(concept)

    def to_response(self, concept: Concept) -> ConceptResponse:
        """Port of ConceptService.toResponse: direct and transitive prerequisite codes,
        both sorted (KnowledgeGraphService, breadth-first, cycle-safe)."""
        direct = sorted(c.code for c in self.graph.direct_prerequisites(concept.id))
        transitive = sorted(c.code for c in self.graph.all_prerequisites(concept.id))
        return ConceptResponse(concept.code, concept.name, concept.description,
                               direct, transitive)

    @staticmethod
    def _validate(request: CreateConceptRequest) -> None:
        """The Bean Validation rules of CreateConceptRequest at the service boundary."""
        if not request.code or not request.code.strip() or len(request.code) > 64:
            raise ValidationError("code is required and must be at most 64 characters")
        if not UPPER_SNAKE_CASE.match(request.code):
            raise ValidationError("code must be UPPER_SNAKE_CASE")
        if not request.name or not request.name.strip() or len(request.name) > 128:
            raise ValidationError("name is required and must be at most 128 characters")
        if request.description is not None and len(request.description) > 1024:
            raise ValidationError("description must be at most 1024 characters")


# --------------------------------------------------------------------------
# FeedbackService
# --------------------------------------------------------------------------

class FeedbackService:
    """Port of feedback.FeedbackService (docs/api-flow.md §5.14).

    Thin adapter over the foundation knowledge_tracer.feedback.FeedbackService (the
    actual port, covered by tests/test_feedback.py): translates the services-layer
    FeedbackRequest DTO into the foundation FeedbackSubmission and delegates. Unlike
    events, feedback does NOT auto-provision learners: an unknown userId is a 404.
    The service does not check that recommendationId belongs to userId (Java parity,
    documented in docs/security.md).
    """

    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings
        self._delegate = feedback_foundation.FeedbackService(session, settings)

    def submit(self, request: FeedbackRequest) -> uuid.UUID:
        """Store one self-report; returns the new feedback id. Write-only for now:
        no model or export reads the feedback table."""
        return self._delegate.submit(feedback_foundation.FeedbackSubmission(
            user_id=request.user_id, problem_id=request.problem_id,
            recommendation_id=request.recommendation_id,
            answers=[feedback_foundation.Answer(question=a.question, answer=a.answer)
                     for a in request.answers],
            free_text=request.free_text))

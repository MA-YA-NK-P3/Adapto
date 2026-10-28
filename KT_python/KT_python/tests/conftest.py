"""Shared fixtures: in-memory SQLite, a mini catalog, and wired services."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from knowledge_tracer.config import Settings
from knowledge_tracer.db import create_schema, make_engine, make_session_factory
from knowledge_tracer.domain import (Concept, Prerequisite, Problem, ProblemConcept,
                                     StaticDifficulty)
from knowledge_tracer.ingestion import (EventIngestionService,
                                        InteractionEventRequest)
from knowledge_tracer.irt import IrtService
from knowledge_tracer.localization import AdaptiveLocalizationService
from knowledge_tracer.mastery import ConceptMasteryService
from knowledge_tracer.state import LearnerStateService


@pytest.fixture()
def settings() -> Settings:
    return Settings()


@pytest.fixture()
def session(settings):
    engine = make_engine("sqlite://")  # in-memory
    create_schema(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s
        s.rollback()


# --- Mini catalog: 5 concepts, 3 edges, 6 problems -----------------------------

CONCEPTS = {
    "ARRAYS": [],
    "HASHING": ["ARRAYS"],
    "TWO_POINTERS": ["ARRAYS"],
    "STRINGS": [],
    "DYNAMIC_PROGRAMMING": ["ARRAYS", "HASHING"],
}

PROBLEMS = [
    # key, difficulty label, prior, acceptance, tags {code: weight}
    ("LC_E1", StaticDifficulty.EASY, 3.0, 0.55, {"ARRAYS": 1.0}),
    ("LC_E2", StaticDifficulty.EASY, 3.2, 0.50, {"HASHING": 0.9, "ARRAYS": 0.6}),
    ("LC_E3", StaticDifficulty.EASY, 3.4, 0.52, {"STRINGS": 1.0}),
    ("LC_E4", StaticDifficulty.EASY, 3.1, 0.58, {"ARRAYS": 0.7, "STRINGS": 0.7}),
    ("LC_M1", StaticDifficulty.MEDIUM, 5.0, 0.40, {"TWO_POINTERS": 1.0}),
    ("LC_M2", StaticDifficulty.MEDIUM, 5.2, 0.38, {"DYNAMIC_PROGRAMMING": 1.0, "ARRAYS": 0.5}),
    ("LC_M3", StaticDifficulty.MEDIUM, 5.4, 0.35, {"HASHING": 1.0}),
    ("LC_M4", StaticDifficulty.MEDIUM, 5.1, 0.42, {"TWO_POINTERS": 0.8, "STRINGS": 0.6}),
    ("LC_H1", StaticDifficulty.HARD, 7.0, 0.30, {"DYNAMIC_PROGRAMMING": 1.0}),
    ("LC_H2", StaticDifficulty.HARD, 7.2, 0.28, {"DYNAMIC_PROGRAMMING": 0.9, "TWO_POINTERS": 0.5}),
    ("LC_PAID", StaticDifficulty.EASY, 3.1, 0.60, {"ARRAYS": 1.0}),  # paid_only=True
]


@pytest.fixture()
def mini_catalog(session):
    ids = {}
    for code, _ in CONCEPTS.items():
        c = Concept(code=code, name=code.replace("_", " ").title())
        session.add(c)
        session.flush()
        ids[code] = c.id
    for code, prereqs in CONCEPTS.items():
        for p in prereqs:
            session.add(Prerequisite(concept_id=ids[code], prerequisite_id=ids[p]))
    for key, label, prior, acceptance, tags in PROBLEMS:
        problem = Problem(problem_key=key, title=key, static_difficulty=label,
                          acceptance_rate=acceptance, paid_only=(key == "LC_PAID"),
                          prior_difficulty=prior, estimated_difficulty=prior,
                          difficulty_uncertainty=1.5, sample_size=0)
        session.add(problem)
        session.flush()
        for code, weight in tags.items():
            session.add(ProblemConcept(problem_id=problem.id,
                                       concept_id=ids[code], weight=weight))
    session.commit()
    return session


class RecordingRewardSink:
    """Test double for decision.OutcomeRewardService."""
    def __init__(self):
        self.calls = []

    def on_outcome(self, event, ability_after):
        self.calls.append((event.event_id, ability_after))


@pytest.fixture()
def services(session, settings):
    """All foundation services wired like the container wires them."""
    irt = IrtService(session, settings)
    mastery = ConceptMasteryService(session, settings)
    localization = AdaptiveLocalizationService(settings)
    state = LearnerStateService(session, settings, irt, mastery, localization)
    reward_sink = RecordingRewardSink()
    ingestion = EventIngestionService(session, settings, state, reward_sink)
    return type("Services", (), {
        "irt": irt, "mastery": mastery, "localization": localization,
        "state": state, "ingestion": ingestion, "reward_sink": reward_sink,
    })()


def event_request(user="U1", problem="LC_E1", result="ACCEPTED", attempt=1,
                  minutes_ago=0, **kwargs):
    from knowledge_tracer.domain import InteractionResult
    return InteractionEventRequest(
        user_id=user, problem_id=problem,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        attempt_number=attempt, result=InteractionResult[result],
        event_id=kwargs.pop("event_id", uuid.uuid4()), **kwargs)

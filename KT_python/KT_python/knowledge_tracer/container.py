"""Application container — the replica's Spring application context.

Builds the engine from settings.database_url, creates the schema (the entities in
domain.py ARE the schema; the Java side uses Flyway V1-V6), optionally seeds the
problem catalog, and wires the whole service graph exactly like the Java @Configuration
classes do:

  * ONE DKT predictor per container (DktConfiguration.dktPredictor is a singleton;
    the predictor is stateless across sessions — LstmDktPredictor caches problem
    features, not learner state).
  * ONE decision-layer router per container (deterministic / bandit / http are
    stateless singletons in Java) and ONE BanditStore (the Java singleton whose
    repository joins the caller's transaction).
  * Per-session services (repositories collapsed into the SQLAlchemy session) are
    built per session_scope(), mirroring tests.test_recommendation.build_stack.

Threading: the container holds a scoped_session (thread-local), so the singletons
above resolve to the calling thread's session on every use — FastAPI runs sync
endpoints in a worker threadpool, and each request gets its own session from
session_scope(). For a purely in-memory SQLite URL ("sqlite://") the engine uses a
StaticPool so the container, the TestClient and every worker thread share ONE
in-memory database (otherwise each connection would see its own empty database).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from . import dkt, seed
from .candidates import CandidateGenerationService
from .config import Settings
from .db import create_schema, make_engine, make_session_factory
from .decision import (BanditDecisionLayer, BanditStore, DecisionLayerRouter,
                       DeterministicDecisionLayer, FallbackPolicy,
                       HttpDecisionLayer, PolicyValidator)
from .dkt import DktPredictor
from .feedback import FeedbackService
from .ingestion import EventIngestionService
from .irt import IrtService
from .knowledge_gap import KnowledgeGapService
from .knowledge_graph import KnowledgeGraphService
from .localization import AdaptiveLocalizationService
from .mastery import ConceptMasteryService
from .recommendation import OutcomeRewardService, RecommendationService
from .research import ResearchExportService
from .services import ConceptService, LearnerService, ProblemService
from .state import LearnerStateService
from .success import SuccessEstimator

logger = logging.getLogger(__name__)

#: database_url values that mean "one shared in-memory SQLite database".
_IN_MEMORY_URLS = ("sqlite://", "sqlite:///:memory:")


class Container:
    """Owns the engine, the schema, the optional catalog seed and the singletons.

    Java parity: this is the @SpringBootApplication context — DataSource, Flyway,
    the CatalogSeeder ApplicationRunner and every singleton @Bean.
    """

    def __init__(self, settings: Settings, seed_catalog: bool = True):
        self.settings = settings
        self.engine = _make_engine(settings.database_url)
        create_schema(self.engine)
        self.session_factory = make_session_factory(self.engine)
        # Thread-local session registry: the singletons below hold this proxy and
        # resolve to the current request's session, like Spring's request-scoped
        # EntityManager proxy behind the singleton services.
        self._scoped = scoped_session(self.session_factory)

        if seed_catalog:
            self.seed_catalog()

        # --- Singletons (one per container, stateless across sessions) ----------
        self.dkt_predictor: DktPredictor = dkt.build_predictor(self._scoped, settings)
        deterministic = DeterministicDecisionLayer()
        self.bandit_store = BanditStore(self._scoped, settings)
        bandit = BanditDecisionLayer(settings, self.bandit_store, deterministic)
        http = HttpDecisionLayer(settings)
        self.router = DecisionLayerRouter(settings, deterministic, bandit, http)

    def seed_catalog(self, path: Optional[str] = None) -> Optional[seed.SeedReport]:
        """Load the problem catalog once (CatalogSeeder). Idempotent: skipped when
        concepts exist. A missing/invalid file never prevents startup — the API
        still serves an empty catalog, exactly like the Java seeder's guard."""
        session = self.session_factory()
        try:
            report = seed.load_catalog(session, self.settings, path)
            session.commit()
            if report.skipped:
                logger.info("Catalog already present — seeding skipped")
            else:
                logger.info("Catalog seeded: %d concepts, %d problems, %d tags",
                            report.concepts, report.problems, report.problem_concepts)
            return report
        except Exception as exc:
            session.rollback()
            logger.warning("Catalog seed skipped (%s): %s",
                           path or self.settings.catalog_path, exc)
            return None
        finally:
            session.close()

    @contextmanager
    def session_scope(self) -> Iterator[SimpleNamespace]:
        """One request-scoped unit of work: yields a namespace with the session and
        ALL services wired (the build_stack graph plus the services.py CRUD services
        and the research export service). Commits on success, rolls back on error —
        the @Transactional boundary every Java controller method runs in."""
        session: Session = self._scoped()
        try:
            yield self._wire(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self._scoped.remove()

    def _wire(self, session: Session) -> SimpleNamespace:
        """Per-session services, in the same order as build_stack (Java: the
        request-scoped repositories inside the singleton services)."""
        settings = self.settings
        irt = IrtService(session, settings)
        mastery = ConceptMasteryService(session, settings)
        localization = AdaptiveLocalizationService(settings)
        state = LearnerStateService(session, settings, irt, mastery, localization)
        graph = KnowledgeGraphService(session)
        gap = KnowledgeGapService(session, settings, graph)
        success = SuccessEstimator(session, settings, self.dkt_predictor)
        candidates = CandidateGenerationService(session, settings, localization)
        validator = PolicyValidator(session)
        fallback = FallbackPolicy(session, validator)
        reward = OutcomeRewardService(session, settings, self.bandit_store)
        recommendation = RecommendationService(
            session, settings, state, gap, candidates, self.router,
            validator, fallback, success, self.dkt_predictor)
        ingestion = EventIngestionService(session, settings, state, reward)
        return SimpleNamespace(
            session=session,
            irt=irt,
            mastery=mastery,
            localization=localization,
            state=state,
            graph=graph,
            gap=gap,
            success=success,
            candidates=candidates,
            validator=validator,
            fallback=fallback,
            bandit_store=self.bandit_store,
            reward=reward,
            recommendation=recommendation,
            ingestion=ingestion,
            learners=LearnerService(session, settings, state),
            problems=ProblemService(session, settings, irt),
            concepts=ConceptService(session, settings, graph),
            feedback=FeedbackService(session, settings),
            research=ResearchExportService(session, settings),
            # Container singletons, re-exposed for the models endpoints:
            router=self.router,
            dkt_predictor=self.dkt_predictor,
        )


def _make_engine(url: str) -> Engine:
    """Engine for the configured URL. Pure in-memory SQLite gets a StaticPool so
    every session (container, TestClient, worker threads) shares one connection —
    and therefore one database."""
    if url in _IN_MEMORY_URLS:
        return create_engine(url, connect_args={"check_same_thread": False},
                             poolclass=StaticPool, future=True)
    return make_engine(url)

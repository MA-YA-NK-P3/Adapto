"""FastAPI application factory — the Python port's replacement for the Spring Boot
container (KnowledgeTracerApplication + @Configuration wiring).

create_app(settings, database_url) builds the engine, creates the schema (the
entities ARE the schema — db.create_schema replaces Flyway), seeds the reference
catalog when the catalog file exists (seed.load_catalog is idempotent, like the
V3-V5 migrations), and wires every service per request exactly like
tests/conftest.py does.

Default database: a sqlite file kt_python.db in the KT_python directory. Tests pass
database_url="sqlite://" (in-memory); for in-memory SQLite the engine uses a
StaticPool so the single connection — and with it the schema — is shared across the
TestClient's worker threads (db.make_engine's check_same_thread handling alone keeps
SingletonThreadPool connections per-thread, which would hide the schema).

The DKT model file is parsed at most once per process (app.state.dkt_model); the
per-request LstmDktPredictor wraps the shared immutable DktModel, mirroring
dkt.build_predictor's degradation rules without re-reading the file on every call.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Tuple

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..candidates import CandidateGenerationService
from ..config import Settings
from ..db import create_schema, make_engine, make_session_factory
from ..decision import (BanditStore, DecisionLayerRouter, DeterministicDecisionLayer,
                        BanditDecisionLayer, FallbackPolicy, HttpDecisionLayer,
                        PolicyValidator)
from ..dkt import DktModel, DktPredictor, LstmDktPredictor, UnavailableDktPredictor
from ..ingestion import EventIngestionService
from ..irt import IrtService
from ..knowledge_gap import KnowledgeGapService
from ..knowledge_graph import KnowledgeGraphService
from ..localization import AdaptiveLocalizationService
from ..mastery import ConceptMasteryService
from ..recommendation import OutcomeRewardService, RecommendationService
from ..research import ResearchExportService
from ..seed import load_catalog
from ..state import LearnerStateService
from ..success import SuccessEstimator
from .auth import ApiKeyMiddleware, UnsupportedMediaTypeMiddleware
from .errors import install_exception_handlers

logger = logging.getLogger(__name__)

#: KT_python/ — the directory config.py's relative paths (catalog_path, dkt.model_path)
#: are documented against.
KT_PYTHON_DIR = Path(__file__).resolve().parents[2]

_MEMORY_SQLITE = ("sqlite://", "sqlite:///:memory:")


def _default_database_url() -> str:
    return "sqlite:///" + str(KT_PYTHON_DIR / "kt_python.db")


def _make_engine(url: str) -> Engine:
    if url in _MEMORY_SQLITE:
        # One shared in-memory connection for all threads (see module docstring).
        return create_engine(url, connect_args={"check_same_thread": False},
                             poolclass=StaticPool, future=True)
    return make_engine(url)


def _load_dkt_model(settings: Settings) -> Tuple[Optional[DktModel], str]:
    """The loading half of dkt.build_predictor, run once at startup.

    Returns (model, source_name) when a model loaded, else (None, unavailable_reason).
    A missing or invalid model file never prevents startup (docs/dkt.md).
    """
    if not settings.dkt.enabled:
        return None, "disabled by knowledge-tracer.dkt.enabled=false"
    path = Path(os.path.abspath(settings.dkt.model_path))
    if not os.access(path, os.R_OK):
        logger.warning("DKT enabled but model file not readable: %s", path)
        return None, (f"model file not found: {path}"
                      " — train with model/dkt/train_dkt.py or the Colab notebook")
    try:
        model = DktModel.load(path)
        logger.info("DKT model loaded from %s (K=%d, H=%d, trainedOn=%s)",
                    path, len(model.concepts), model.hidden_size, model.trained_on)
        return model, path.name
    except Exception as exc:
        logger.error("DKT model at %s could not be loaded: %s", path, exc)
        return None, f"invalid model file {path.name}: {exc}"


def _maybe_seed(factory: sessionmaker, settings: Settings) -> None:
    """Load the reference catalog when it exists and the DB is empty (Flyway V3-V5)."""
    path = Path(settings.catalog_path)
    if not path.is_absolute():
        path = KT_PYTHON_DIR / path
    if not path.is_file():
        return
    with factory() as session:
        report = load_catalog(session, settings, str(path))
        session.commit()
    if report.skipped:
        logger.info("Catalog already present; seed skipped")
    else:
        logger.info("Seeded catalog: %d concepts, %d prerequisites, %d problems",
                    report.concepts, report.prerequisites, report.problems)


@dataclass
class Services:
    """All request-scoped services, wired like tests/conftest.py wires them."""
    session: Session
    settings: Settings
    irt: IrtService
    mastery: ConceptMasteryService
    localization: AdaptiveLocalizationService
    state: LearnerStateService
    graph: KnowledgeGraphService
    gap: KnowledgeGapService
    candidates: CandidateGenerationService
    validator: PolicyValidator
    decision: DecisionLayerRouter
    fallback: FallbackPolicy
    bandit_store: BanditStore
    reward: OutcomeRewardService
    ingestion: EventIngestionService
    success: SuccessEstimator
    recommendation: RecommendationService
    research: ResearchExportService
    dkt_predictor: DktPredictor


# --- FastAPI dependencies ------------------------------------------------------

def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """One session per request. Routes commit through db.transaction(); closing an
    uncommitted session rolls back, so failed requests never leak partial writes."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_services(request: Request, session: Session = Depends(get_session),
                 settings: Settings = Depends(get_settings)) -> Services:
    """The whole service graph for one request (plain objects — cheap to build)."""
    irt = IrtService(session, settings)
    mastery = ConceptMasteryService(session, settings)
    localization = AdaptiveLocalizationService(settings)
    state = LearnerStateService(session, settings, irt, mastery, localization)
    graph = KnowledgeGraphService(session)
    gap = KnowledgeGapService(session, settings, graph)
    candidates = CandidateGenerationService(session, settings, localization)
    validator = PolicyValidator(session)
    bandit_store = BanditStore(session, settings)
    deterministic = DeterministicDecisionLayer()
    bandit = BanditDecisionLayer(settings, bandit_store, deterministic)
    http = HttpDecisionLayer(settings)
    decision = DecisionLayerRouter(settings, deterministic, bandit, http)
    fallback = FallbackPolicy(session, validator)
    reward = OutcomeRewardService(session, settings, bandit_store)
    ingestion = EventIngestionService(session, settings, state, reward)
    dkt_predictor = _dkt_predictor(request.app.state, session, settings)
    success = SuccessEstimator(session, settings, dkt_predictor)
    recommendation = RecommendationService(session, settings, state, gap, candidates,
                                           decision, validator, fallback, success,
                                           dkt_predictor)
    research = ResearchExportService(session, settings)
    return Services(session=session, settings=settings, irt=irt, mastery=mastery,
                    localization=localization, state=state, graph=graph, gap=gap,
                    candidates=candidates, validator=validator, decision=decision,
                    fallback=fallback, bandit_store=bandit_store, reward=reward,
                    ingestion=ingestion, success=success, recommendation=recommendation,
                    research=research, dkt_predictor=dkt_predictor)


def _dkt_predictor(app_state, session: Session, settings: Settings) -> DktPredictor:
    """dkt.build_predictor with the model parsed once (app.state.dkt_model)."""
    model, source_or_reason = app_state.dkt_model
    if model is None:
        return UnavailableDktPredictor(source_or_reason)
    return LstmDktPredictor(session, settings, model, source=source_or_reason)


def create_app(settings: Optional[Settings] = None,
               database_url: Optional[str] = None) -> FastAPI:
    """Build the FastAPI app: schema, seed, middleware, error handlers, routers.

    Database URL precedence: the explicit database_url argument, then an explicitly
    provided settings.database_url, then KT_DATABASE_URL from the environment, then
    the default sqlite file kt_python.db in the KT_python directory.
    """
    settings_provided = settings is not None
    settings = settings if settings_provided else Settings.from_env()
    if database_url is not None:
        url = database_url
    elif settings_provided:
        url = settings.database_url
    else:
        url = os.environ.get("KT_DATABASE_URL") or _default_database_url()
    engine = _make_engine(url)
    create_schema(engine)
    factory = make_session_factory(engine)

    app = FastAPI(title="Knowledge Tracer API", version="0.1.0")
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.dkt_model = _load_dkt_model(settings)

    _maybe_seed(factory, settings)

    install_exception_handlers(app)

    # Last added runs first: CORS (preflight) -> API key -> media type -> routes.
    app.add_middleware(UnsupportedMediaTypeMiddleware)
    app.add_middleware(ApiKeyMiddleware, settings=settings)
    if settings.security.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.security.allowed_origins),
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Key", "X-Request-Id"],
            expose_headers=["X-Request-Id"],
            max_age=3600,
        )

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict:
        """Liveness probe — unauthenticated (the ApiKeyMiddleware skips it)."""
        return {"status": "UP"}

    # The static research dashboard (Java serves it from resources/static). Served as
    # one explicit route — no catch-all mount, so unknown /api/ paths still answer
    # with the ApiError 404 "No endpoint at this path".
    static_dir = Path(__file__).resolve().parent.parent / "static"
    dashboard = static_dir / "dashboard.html"
    if dashboard.is_file():
        from fastapi.responses import FileResponse

        @app.get("/dashboard.html", include_in_schema=False)
        def dashboard_page():
            return FileResponse(dashboard)

    # Imported here so routes_*.py can import the dependencies above without a cycle.
    from . import (routes_concepts, routes_events, routes_feedback, routes_learners,
                   routes_models, routes_problems, routes_research)

    app.include_router(routes_events.router)
    app.include_router(routes_learners.router)
    app.include_router(routes_problems.router)
    app.include_router(routes_concepts.router)
    app.include_router(routes_feedback.router)
    app.include_router(routes_models.router)
    app.include_router(routes_research.router)
    return app

"""Application factory — the Spring Boot application equivalent for the Python port.

    create_app(settings=None, database_url=None) -> FastAPI

Accepted shapes:
  * create_app()                       — Settings.from_env(); the database defaults to
                                         KT_DATABASE_URL, else a sqlite file
                                         kt_python.db in the KT_python directory.
  * create_app(settings)               — a config.Settings; the database comes from
                                         settings.database_url unless overridden.
  * create_app(settings, database_url) — explicit URL wins ("sqlite://" is one shared
                                         in-memory database, StaticPool-backed).
  * create_app(container)              — a knowledge_tracer.container.Container (the
                                         integration test harness shape): the app
                                         reuses the container's settings, engine and
                                         session factory; schema creation and catalog
                                         seeding stay the container's job.

Without a container the factory creates the schema (db.create_schema — the entities
ARE the schema, mirroring Flyway V1-V6) and seeds the problem catalog once when the
catalog file exists (seed.load_catalog is idempotent; a missing/invalid file never
prevents startup, like the Java CatalogSeeder guard).

Per request, one transactional SQLAlchemy session (commit on success, rollback on
error — the @Transactional boundary every Java controller method runs in) and the
full service graph wired exactly like the container/conftest do. The DKT model file
is parsed at most once per app; each request gets an LstmDktPredictor wrapping the
shared immutable model (build_predictor's degradation rules apply at load time).

Uvicorn factory entry point: "knowledge_tracer.api.app:create_app" (factory=True).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, List, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.cors import CORSMiddleware

from .. import dkt as dkt_module
from .. import seed as seed_module
from ..candidates import CandidateGenerationService
from ..config import Settings
from ..db import create_schema, make_engine, make_session_factory
from ..decision import (BanditDecisionLayer, BanditStore, DecisionLayerRouter,
                        DeterministicDecisionLayer, FallbackPolicy, HttpDecisionLayer,
                        PolicyValidator)
from ..ingestion import EventIngestionService
from ..irt import IrtService
from ..knowledge_gap import KnowledgeGapService
from ..knowledge_graph import KnowledgeGraphService
from ..localization import AdaptiveLocalizationService
from ..mastery import ConceptMasteryService
from ..recommendation import OutcomeRewardService, RecommendationService
from ..research import ResearchExportService
from ..services import ConceptService, LearnerService, ProblemService
from ..state import LearnerStateService
from ..success import SuccessEstimator
from .auth import (ApiKeyMiddleware, CorrelationIdMiddleware,
                   JsonContentTypeMiddleware)
from .errors import register_exception_handlers

logger = logging.getLogger(__name__)

#: KT_python/ — the package lives two levels down (KT_python/knowledge_tracer/api/).
KT_PYTHON_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = "sqlite:///" + str(KT_PYTHON_DIR / "kt_python.db")

#: The dashboard ships inside the package (an adapted copy of
#: backend/src/main/resources/static/dashboard.html).
_DASHBOARD_HTML = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"

#: database_url values that mean "one shared in-memory SQLite database".
_IN_MEMORY_URLS = ("sqlite://", "sqlite:///:memory:")


def _is_container(obj) -> bool:
    """Duck-typed knowledge_tracer.container.Container (imported lazily nowhere:
    any object owning settings + engine + session_factory qualifies)."""
    return (obj is not None
            and not isinstance(obj, Settings)
            and hasattr(obj, "settings")
            and hasattr(obj, "engine")
            and hasattr(obj, "session_factory"))


def _make_engine(url: str):
    """Engine for the configured URL. Pure in-memory SQLite gets a StaticPool so the
    app, the TestClient and every worker thread share ONE in-memory database
    (otherwise each connection would see its own empty database)."""
    if url in _IN_MEMORY_URLS:
        return create_engine(url, connect_args={"check_same_thread": False},
                             poolclass=StaticPool, future=True)
    return make_engine(url)


def _load_dkt(settings: Settings):
    """Parse the DKT model once per app (build_predictor's degradation rules):
    returns (model, source) when an LSTM model loads, else (None, reason)."""
    if not settings.dkt.enabled:
        return None, "disabled by knowledge-tracer.dkt.enabled=false"
    path = Path(os.path.abspath(settings.dkt.model_path))
    if not os.access(path, os.R_OK):
        logger.warning("DKT enabled but model file not readable: %s", path)
        return None, (f"model file not found: {path}"
                      " — train with model/dkt/train_dkt.py or the Colab notebook")
    try:
        model = dkt_module.DktModel.load(path)
        logger.info("DKT model loaded from %s (K=%d, H=%d, trainedOn=%s)", path,
                    len(model.concepts), model.hidden_size, model.trained_on)
        return model, path.name
    except Exception as exc:  # an invalid model never prevents startup (Java parity)
        logger.error("DKT model at %s could not be loaded: %s", path, exc)
        return None, f"invalid model file {path.name}: {exc}"


def _request_dkt_predictor(session: Session, settings: Settings, app: FastAPI):
    """One predictor per request, wrapping the app-cached model (LstmDktPredictor
    caches problem features per predictor, not learner state — a fresh instance per
    request is correct and keeps all reads on the request's transaction)."""
    model = app.state.dkt_model
    if model is not None:
        return dkt_module.LstmDktPredictor(session, settings, model,
                                           source=app.state.dkt_detail)
    return dkt_module.UnavailableDktPredictor(app.state.dkt_detail)


def wire_services(session: Session, settings: Settings, dkt_predictor) -> SimpleNamespace:
    """The per-request service graph — the same wiring as container._wire /
    conftest.build_stack (Java: the request-scoped repositories inside the singleton
    services). The decision-layer providers are stateless; the BanditStore joins the
    caller's transaction, so per-request instances behave like the Java singletons."""
    irt = IrtService(session, settings)
    mastery = ConceptMasteryService(session, settings)
    localization = AdaptiveLocalizationService(settings)
    state = LearnerStateService(session, settings, irt, mastery, localization)
    graph = KnowledgeGraphService(session)
    gap = KnowledgeGapService(session, settings, graph)
    success = SuccessEstimator(session, settings, dkt_predictor)
    candidates = CandidateGenerationService(session, settings, localization)
    validator = PolicyValidator(session)
    fallback = FallbackPolicy(session, validator)
    bandit_store = BanditStore(session, settings)
    deterministic = DeterministicDecisionLayer()
    bandit = BanditDecisionLayer(settings, bandit_store, deterministic)
    http = HttpDecisionLayer(settings)
    router = DecisionLayerRouter(settings, deterministic, bandit, http)
    reward = OutcomeRewardService(session, settings, bandit_store)
    recommendation = RecommendationService(
        session, settings, state, gap, candidates, router, validator, fallback,
        success, dkt_predictor)
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
        bandit_store=bandit_store,
        reward=reward,
        recommendation=recommendation,
        ingestion=ingestion,
        learners=LearnerService(session, settings, state),
        problems=ProblemService(session, settings, irt),
        concepts=ConceptService(session, settings, graph),
        research=ResearchExportService(session, settings),
        dkt_predictor=dkt_predictor,
    )


# --------------------------------------------------------------------------
# FastAPI dependencies (imported by the routes_* modules)
# --------------------------------------------------------------------------

def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """One transactional session per request: commit on success, rollback on error —
    the @Transactional boundary (container.session_scope's twin)."""
    session: Session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_services(request: Request,
                 session: Session = Depends(get_session)) -> SimpleNamespace:
    settings = request.app.state.settings
    predictor = _request_dkt_predictor(session, settings, request.app)
    return wire_services(session, settings, predictor)


# --------------------------------------------------------------------------
# The factory
# --------------------------------------------------------------------------

def create_app(settings: Optional[Settings] = None,
               database_url: Optional[str] = None) -> FastAPI:
    """Build the FastAPI app. See the module docstring for the accepted shapes."""
    container = None
    if _is_container(settings):
        container = settings
        settings = container.settings
    if settings is None:
        settings = Settings.from_env()
        if database_url is None:
            database_url = os.environ.get("KT_DATABASE_URL") or DEFAULT_DATABASE_URL
    elif database_url is None:
        database_url = settings.database_url

    if container is not None:
        # The container owns the engine, the schema and the (optional) catalog seed.
        engine = container.engine
        session_factory = container.session_factory
    else:
        engine = _make_engine(database_url)
        create_schema(engine)
        session_factory = make_session_factory(engine)
        _seed_catalog(session_factory, settings)

    app = FastAPI(title="Knowledge Tracer (Python replica)",
                  redirect_slashes=False)  # Spring does not match trailing slashes
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.dkt_model, app.state.dkt_detail = _load_dkt(settings)

    register_exception_handlers(app)

    # --- Middleware (last added = outermost; Java order: correlation -> key -> web) ---
    if settings.security.allowed_origins:
        _add_cors(app, settings.security.allowed_origins)
    app.add_middleware(JsonContentTypeMiddleware)
    app.add_middleware(ApiKeyMiddleware, settings=settings)
    app.add_middleware(CorrelationIdMiddleware)

    # --- Controllers (one module per resource, mirroring the Java api/ tree) -------
    from . import (routes_concepts, routes_events, routes_feedback, routes_learners,
                   routes_models, routes_problems, routes_research)
    for module in (routes_events, routes_learners, routes_problems, routes_concepts,
                   routes_feedback, routes_models, routes_research):
        app.include_router(module.router)

    # --- Health (unauthenticated) and the learner dashboard -------------------------

    @app.get("/api/health", include_in_schema=False)
    def api_health():
        """Liveness probe under the API namespace (never API-key filtered)."""
        return {"status": "UP"}

    @app.get("/actuator/health")
    def actuator_health():
        """Minimal Spring Actuator equivalent (postman collection folder "0. Health")."""
        return {"status": "UP"}

    if _DASHBOARD_HTML.is_file():
        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/dashboard")

        @app.get("/dashboard", include_in_schema=False)
        @app.get("/dashboard.html", include_in_schema=False)
        def dashboard():
            """The learner dashboard (Spring serves static/dashboard.html at
            /dashboard.html; the replica also answers /dashboard)."""
            return FileResponse(_DASHBOARD_HTML)

    return app


def _seed_catalog(session_factory: sessionmaker, settings: Settings) -> None:
    """Load the problem catalog once (CatalogSeeder). Idempotent: skipped when
    concepts exist. A missing/invalid file never prevents startup — the API still
    serves an empty catalog, exactly like the Java seeder's guard."""
    catalog_path = Path(settings.catalog_path)
    if not catalog_path.is_file():
        logger.info("Catalog file %s not found — starting with an empty catalog",
                    catalog_path)
        return
    session = session_factory()
    try:
        report = seed_module.load_catalog(session, settings)
        session.commit()
        if report.skipped:
            logger.info("Catalog already present — seeding skipped")
        else:
            logger.info("Catalog seeded: %d concepts, %d problems, %d tags",
                        report.concepts, report.problems, report.problem_concepts)
    except Exception as exc:
        session.rollback()
        logger.warning("Catalog seed skipped (%s): %s", catalog_path, exc)
    finally:
        session.close()


def _add_cors(app: FastAPI, origins: List[str]) -> None:
    """config.WebConfig.addCorsMappings: /api/**, GET/POST/OPTIONS, the three headers,
    X-Request-Id exposed, maxAge 3600. Java uses allowedOriginPatterns, so entries
    with '*' become a regex (starlette has no pattern list)."""
    exact = [o for o in origins if "*" not in o]
    patterns = [o for o in origins if "*" in o]
    regex = None
    if patterns:
        regex = "^(" + "|".join(
            re.escape(p).replace(r"\*", ".*") for p in patterns) + ")$"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=exact,
        allow_origin_regex=regex,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-Id"],
        expose_headers=["X-Request-Id"],
        max_age=3600,
    )

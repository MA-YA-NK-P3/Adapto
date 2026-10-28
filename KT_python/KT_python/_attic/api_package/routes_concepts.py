"""/api/concepts — port of concept.api.ConceptController and concept.ConceptService
(the service is a thin repository + graph wrapper, inlined here).

POST /api/concepts                       create (201; 409 when the code exists)
GET  /api/concepts                       list, sorted by code
GET  /api/concepts/{code}                one concept (404 when unknown)
POST /api/concepts/{code}/prerequisites  add edge "{code} requires prerequisiteCode"
                                         (201). Checks, in Java's order: self-loop
                                         422 -> unknown concept 404 -> unknown
                                         prerequisite 404 -> cycle 422 -> duplicate
                                         edge 409.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ..db import transaction
from ..domain import Concept, Prerequisite
from ..errors import BusinessRuleError, ConflictError, NotFoundError
from ..knowledge_graph import KnowledgeGraphService
from .app import Services, get_services
from .schemas import AddPrerequisiteIn, CreateConceptIn

router = APIRouter()


def _concept_response(graph: KnowledgeGraphService, concept: Concept) -> dict:
    """ConceptResponse: direct and transitive prerequisite codes, both sorted."""
    return {
        "code": concept.code,
        "name": concept.name,
        "description": concept.description,
        "directPrerequisites": sorted(
            c.code for c in graph.direct_prerequisites(concept.id)),
        "allPrerequisites": sorted(
            c.code for c in graph.all_prerequisites(concept.id)),
    }


@router.post("/api/concepts")
def create(body: CreateConceptIn,
           services: Services = Depends(get_services)) -> JSONResponse:
    session = services.session
    with transaction(session):
        if session.scalar(select(Concept).where(Concept.code == body.code)) is not None:
            raise ConflictError("Concept already exists: " + body.code)
        concept = Concept(code=body.code, name=body.name, description=body.description)
        session.add(concept)
        session.flush()
        return JSONResponse(status_code=201,
                            content=_concept_response(services.graph, concept))


@router.get("/api/concepts")
def list_concepts(services: Services = Depends(get_services)) -> list:
    session = services.session
    concepts = sorted(session.scalars(select(Concept)), key=lambda c: c.code)
    return [_concept_response(services.graph, c) for c in concepts]


@router.get("/api/concepts/{code}")
def get_concept(code: str, services: Services = Depends(get_services)) -> dict:
    return _concept_response(services.graph, services.graph.get_by_code(code))


@router.post("/api/concepts/{code}/prerequisites")
def add_prerequisite(code: str, body: AddPrerequisiteIn,
                     services: Services = Depends(get_services)) -> JSONResponse:
    session = services.session
    graph = services.graph
    with transaction(session):
        if code == body.prerequisite_code:
            raise BusinessRuleError("A concept cannot be its own prerequisite")
        concept = graph.get_by_code(code)
        prerequisite = session.scalar(
            select(Concept).where(Concept.code == body.prerequisite_code))
        if prerequisite is None:
            raise NotFoundError("Concept not found: " + body.prerequisite_code)
        if graph.would_create_cycle(concept.id, prerequisite.id):
            raise BusinessRuleError(
                f"Adding {code} -> {body.prerequisite_code} would create a cycle")
        existing = session.scalar(
            select(Prerequisite)
            .where(Prerequisite.concept_id == concept.id)
            .where(Prerequisite.prerequisite_id == prerequisite.id))
        if existing is not None:
            raise ConflictError("Prerequisite edge already exists")
        session.add(Prerequisite(concept_id=concept.id,
                                 prerequisite_id=prerequisite.id))
        session.flush()
        return JSONResponse(status_code=201,
                            content=_concept_response(graph, concept))

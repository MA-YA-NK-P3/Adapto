"""Concept graph endpoints — port of concept/api/ConceptController.java
(docs/api-flow.md §5.10-5.13).

  POST /api/concepts                          register a concept (201; 409 duplicate code)
  GET  /api/concepts                          all concepts, sorted by code
  GET  /api/concepts/{code}                   one concept with direct + transitive prerequisites
  POST /api/concepts/{code}/prerequisites     declare an edge (201; 422 self-loop/cycle,
                                              404 unknown code, 409 duplicate edge —
                                              checked in that Java order)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..services import CreateConceptRequest
from .app import get_services
from .schemas import AddPrerequisiteRequestIn, CreateConceptRequestIn

router = APIRouter()


@router.post("/api/concepts", status_code=201)
def create_concept(body: CreateConceptRequestIn, services=Depends(get_services)):
    return services.concepts.create(CreateConceptRequest(
        code=body.code, name=body.name, description=body.description)).to_dict()


@router.get("/api/concepts")
def list_concepts(services=Depends(get_services)):
    return [c.to_dict() for c in services.concepts.list()]


@router.get("/api/concepts/{code}")
def get_concept(code: str, services=Depends(get_services)):
    return services.concepts.get_by_code(code).to_dict()


@router.post("/api/concepts/{code}/prerequisites", status_code=201)
def add_prerequisite(code: str, body: AddPrerequisiteRequestIn,
                     services=Depends(get_services)):
    return services.concepts.add_prerequisite(code, body.prerequisite_code).to_dict()

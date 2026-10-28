"""Problem catalog endpoints — port of problem/api/ProblemController.java
(docs/api-flow.md §5.7-5.9).

  POST /api/problems                 register a problem (201; 409 duplicate key;
                                     404 unknown conceptCode — one transaction, the
                                     problem insert rolls back with it)
  GET  /api/problems                 all problems, frontendId order (nulls last) then key
  GET  /api/problems/{problemKey}    one problem (404 unknown key)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..services import ConceptTag, CreateProblemRequest
from .app import get_services
from .schemas import CreateProblemRequestIn

router = APIRouter()


@router.post("/api/problems", status_code=201)
def create_problem(body: CreateProblemRequestIn, services=Depends(get_services)):
    return services.problems.create(CreateProblemRequest(
        problem_key=body.problem_key, title=body.title, url=body.url,
        static_difficulty=body.static_difficulty,
        concepts=([ConceptTag(t.concept_code, t.weight) for t in body.concepts]
                  if body.concepts is not None else None),
        acceptance_rate=body.acceptance_rate, paid_only=body.paid_only,
        topic_tags=body.topic_tags)).to_dict()


@router.get("/api/problems")
def list_problems(services=Depends(get_services)):
    return [p.to_dict() for p in services.problems.list()]


@router.get("/api/problems/{problem_key}")
def get_problem(problem_key: str, services=Depends(get_services)):
    return services.problems.get_by_key(problem_key).to_dict()

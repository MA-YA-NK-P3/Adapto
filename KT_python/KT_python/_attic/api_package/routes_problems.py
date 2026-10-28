"""/api/problems — port of problem.api.ProblemController and problem.ProblemService
(the service is a thin repository + IRT-prior wrapper, inlined here as in the other
replica modules).

POST /api/problems                  register a problem (201; 409 when the key exists,
                                    404 when a tagged concept is unknown — the whole
                                    transaction rolls back, no partial problem)
GET  /api/problems                  the catalog, frontendId (nulls last) then problemKey
GET  /api/problems/{problemKey}     one problem (404 when unknown)
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import transaction
from ..domain import Concept, Problem, ProblemConcept
from ..errors import ConflictError, NotFoundError
from ..recommendation import round2
from .app import Services, get_services
from .schemas import CreateProblemIn

router = APIRouter()


def _frontend_id(value: Optional[str]):
    """Java's Integer frontendId: numeric strings go out as JSON numbers."""
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return value


def _problem_response(p: Problem, links: List[ProblemConcept]) -> dict:
    """ProblemResponse — conceptTags ordered primary-first (weight desc, then code)."""
    tags = sorted(({"code": link.concept.code, "weight": link.weight} for link in links),
                  key=lambda t: (-t["weight"], t["code"]))
    topic_tags = ([t.strip() for t in p.topic_tags.split(",") if t.strip()]
                  if p.topic_tags else [])
    return {
        "problemId": p.problem_key,
        "title": p.title,
        "url": p.url,
        "staticDifficulty": p.static_difficulty.name,
        "estimatedDifficulty": round2(p.estimated_difficulty),
        "difficultyUncertainty": round2(p.difficulty_uncertainty),
        "sampleSize": p.sample_size,
        "source": p.source,
        "concepts": sorted(t["code"] for t in tags),
        "conceptTags": tags,
        "priorDifficulty": round2(p.prior_difficulty),
        "frontendId": _frontend_id(p.frontend_id),
        "slug": p.slug,
        "acceptanceRate": p.acceptance_rate,
        "likes": p.likes,
        "paidOnly": p.paid_only,
        "topicTags": topic_tags,
    }


def _list_sort_key(p: Problem):
    """Comparator.comparing(frontendId == null ? MAX_VALUE : frontendId).thenComparing(key)."""
    try:
        fid = int(str(p.frontend_id)) if p.frontend_id is not None else 2**31 - 1
    except ValueError:
        fid = 2**31 - 1
    return (fid, p.problem_key)


@router.post("/api/problems")
def create(body: CreateProblemIn,
           services: Services = Depends(get_services)) -> JSONResponse:
    session = services.session
    with transaction(session):
        if session.scalar(
                select(Problem).where(Problem.problem_key == body.problem_key)) is not None:
            raise ConflictError("Problem already exists: " + body.problem_key)
        prior = services.irt.prior(body.static_difficulty, body.acceptance_rate)
        problem = Problem(
            problem_key=body.problem_key,
            title=body.title,
            url=body.url,
            static_difficulty=body.static_difficulty,
            prior_difficulty=prior,
            estimated_difficulty=prior,
            difficulty_uncertainty=services.settings.irt.difficulty_prior_sd,
            acceptance_rate=body.acceptance_rate,
            paid_only=bool(body.paid_only),
            topic_tags=", ".join(body.topic_tags) if body.topic_tags else None)
        session.add(problem)
        session.flush()
        links: List[ProblemConcept] = []
        for tag in body.concepts or []:
            concept = session.scalar(
                select(Concept).where(Concept.code == tag.concept_code))
            if concept is None:
                raise NotFoundError("Concept not found: " + tag.concept_code)
            link = ProblemConcept(problem_id=problem.id, concept_id=concept.id,
                                  weight=tag.weight if tag.weight is not None else 1.0)
            session.add(link)
            links.append(link)
        session.flush()
        return JSONResponse(status_code=201, content=_problem_response(problem, links))


@router.get("/api/problems")
def list_problems(services: Services = Depends(get_services)) -> list:
    session = services.session
    tags = {}
    for link in session.scalars(select(ProblemConcept)):
        tags.setdefault(link.problem_id, []).append(link)
    problems = sorted(session.scalars(select(Problem)), key=_list_sort_key)
    return [_problem_response(p, tags.get(p.id, [])) for p in problems]


@router.get("/api/problems/{problem_key}")
def get_problem(problem_key: str, services: Services = Depends(get_services)) -> dict:
    session = services.session
    problem = session.scalar(select(Problem).where(Problem.problem_key == problem_key))
    if problem is None:
        raise NotFoundError("Problem not found: " + problem_key)
    links = list(session.scalars(
        select(ProblemConcept).where(ProblemConcept.problem_id == problem.id)))
    return _problem_response(problem, links)

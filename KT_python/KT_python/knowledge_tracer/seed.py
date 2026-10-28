"""Seed the reference data from data/seed/catalog_1000.json.

The Java side seeds via Flyway migrations V3-V5; the replica loads the same generated
catalog file (1000 LeetCode problems, 25 concepts, 21 prerequisite edges). Idempotent:
if the concepts table is non-empty the load is skipped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .domain import Concept, Prerequisite, Problem, ProblemConcept, StaticDifficulty


@dataclass(frozen=True)
class SeedReport:
    concepts: int
    prerequisites: int
    problems: int
    problem_concepts: int
    skipped: bool


def load_catalog(session: Session, settings: Settings, path: str | None = None) -> SeedReport:
    catalog_path = Path(path or settings.catalog_path)
    if not catalog_path.is_file():
        candidates = [
            Path(__file__).resolve().parents[2] / "data" / "seed" / "catalog_1000.json",
            Path(__file__).resolve().parents[1] / "data" / "seed" / "catalog_1000.json",
            Path(__file__).resolve().parents[3] / "Knowledge_Tracing" / "Knowledge_Tracing" / "data" / "seed" / "catalog_1000.json",
            Path(__file__).resolve().parents[2] / "Knowledge_Tracing" / "data" / "seed" / "catalog_1000.json",
        ]
        for c in candidates:
            if c.is_file():
                catalog_path = c
                break

    if session.scalar(select(func.count()).select_from(Concept)) > 0:
        return SeedReport(0, 0, 0, 0, skipped=True)

    data = json.loads(catalog_path.read_text(encoding="utf-8"))

    concept_ids: dict[str, object] = {}
    for c in data["concepts"]:
        concept = Concept(code=c["code"], name=c.get("name", c["code"].title()),
                          description=c.get("description"))
        session.add(concept)
        session.flush()
        concept_ids[c["code"]] = concept.id

    edges = 0
    for c in data["concepts"]:
        for prereq_code in c.get("prerequisites", []):
            session.add(Prerequisite(concept_id=concept_ids[c["code"]],
                                     prerequisite_id=concept_ids[prereq_code]))
            edges += 1

    problems = tags = 0
    difficulty_prior_sd = settings.irt.difficulty_prior_sd
    import re
    def _to_slug(t: str) -> str:
        return re.sub(r'[\s_]+', '-', re.sub(r'[^\w\s-]', '', t.lower())).strip('-')

    for p in data["problems"]:
        slug = p.get("slug") or _to_slug(p["title"])
        url = p.get("url") or f"https://leetcode.com/problems/{slug}/"
        problem = Problem(
            problem_key=p["problemId"], title=p["title"], url=url,
            static_difficulty=StaticDifficulty[p["staticDifficulty"]],
            frontend_id=p.get("frontendId"), slug=slug,
            acceptance_rate=p.get("acceptanceRate"), likes=p.get("likes"),
            paid_only=p.get("paidOnly", False),
            prior_difficulty=p["priorDifficulty"],
            estimated_difficulty=p["priorDifficulty"],
            difficulty_uncertainty=difficulty_prior_sd, sample_size=0)
        session.add(problem)
        session.flush()
        problems += 1
        for code, weight in p.get("conceptWeights", {}).items():
            session.add(ProblemConcept(problem_id=problem.id,
                                       concept_id=concept_ids[code], weight=weight))
            tags += 1

    session.flush()
    return SeedReport(len(concept_ids), edges, problems, tags, skipped=False)

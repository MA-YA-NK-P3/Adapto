"""Read operations over the prerequisite graph (docs/knowledge-graph.md).

Port of concept.KnowledgeGraphService, plus the cycle check from
concept.ConceptService.addPrerequisite (ported as would_create_cycle so the API
layer can reuse it when adding edges).

The graph structure is static domain knowledge and is kept strictly separate from
learner state (master plan §14): this service has no mastery repository;
weakest_prerequisite receives the learner's masteries as an argument and writes
nothing. All traversals are breadth-first with a visited set, so they terminate
even if a cycle were inserted directly into the database.

Note on the feature toggle: settings.graph.enabled is NOT read here. Exactly as in
the Java code, the toggle only changes KnowledgeGapService (no PREREQUISITE_GAP when
disabled); the graph itself, these traversals and the cycle check stay available.
"""
from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain import Concept, ConceptMastery, Prerequisite
from .errors import NotFoundError

#: Mastery assumed for a prerequisite the learner has never practised
#: (uninformative prior; it loses to any prerequisite with measured weakness).
UNOBSERVED_MASTERY = 0.5


@dataclass(frozen=True)
class PrerequisiteWeakness:
    """A prerequisite concept together with the learner's current mastery of it.

    Port of the KnowledgeGraphService.PrerequisiteWeakness record.
    """
    concept: Concept
    mastery: float
    evidence_count: int


class KnowledgeGraphService:
    """Port of concept.KnowledgeGraphService (docs/knowledge-graph.md).

    The Java class injects ConceptRepository and PrerequisiteRepository; the
    replica's repository equivalent is the SQLAlchemy session, as in the other
    foundation services. Each BFS step is one query — fine at catalog scale.
    """

    def __init__(self, session: Session):
        self.session = session

    def direct_prerequisites(self, concept_id: uuid.UUID) -> List[Concept]:
        """Direct prerequisites of a concept."""
        edges = self.session.scalars(
            select(Prerequisite).where(Prerequisite.concept_id == concept_id))
        return [self.session.get(Concept, edge.prerequisite_id) for edge in edges]

    def all_prerequisites(self, concept_id: uuid.UUID) -> List[Concept]:
        """Transitive prerequisites, breadth-first, closest first. Cycle-safe."""
        visited = {concept_id}
        ordered: List[Concept] = []
        queue = deque([concept_id])
        while queue:
            current = queue.popleft()
            edges = self.session.scalars(
                select(Prerequisite).where(Prerequisite.concept_id == current))
            for edge in edges:
                if edge.prerequisite_id not in visited:
                    visited.add(edge.prerequisite_id)
                    ordered.append(self.session.get(Concept, edge.prerequisite_id))
                    queue.append(edge.prerequisite_id)
        return ordered

    def downstream_concepts(self, concept_id: uuid.UUID) -> Set[Concept]:
        """Concepts that (transitively) depend on the given concept. Cycle-safe."""
        visited = {concept_id}
        result: Set[Concept] = set()
        queue = deque([concept_id])
        while queue:
            current = queue.popleft()
            edges = self.session.scalars(
                select(Prerequisite).where(Prerequisite.prerequisite_id == current))
            for edge in edges:
                if edge.concept_id not in visited:
                    visited.add(edge.concept_id)
                    result.add(self.session.get(Concept, edge.concept_id))
                    queue.append(edge.concept_id)
        return result

    def weakest_prerequisite(self, concept_id: uuid.UUID,
                             masteries: List[ConceptMastery]) -> Optional[PrerequisiteWeakness]:
        """The weakest direct prerequisite of a concept, given the learner's masteries.

        Prerequisites with no evidence are treated as mastery 0.5 (uninformative
        prior) but lose to any prerequisite with measured weakness. Ties keep the
        first candidate encountered, as Java's Stream.min does. Returns None when
        the concept has no prerequisites.
        """
        by_concept = {m.concept_id: m for m in masteries}
        candidates: List[PrerequisiteWeakness] = []
        for prereq in self.direct_prerequisites(concept_id):
            mastery = by_concept.get(prereq.id)
            candidates.append(PrerequisiteWeakness(
                concept=prereq,
                mastery=mastery.mastery if mastery is not None else UNOBSERVED_MASTERY,
                evidence_count=mastery.evidence_count if mastery is not None else 0))
        if not candidates:
            return None
        return min(candidates, key=lambda c: c.mastery)

    def get_by_code(self, code: str) -> Concept:
        """A concept by its public code, or NotFoundError (404), as the Java service."""
        concept = self.session.scalar(select(Concept).where(Concept.code == code))
        if concept is None:
            raise NotFoundError("Concept not found: " + code)
        return concept

    def would_create_cycle(self, concept_id: uuid.UUID,
                           prerequisite_id: uuid.UUID) -> bool:
        """True iff adding the edge "concept requires prerequisite" would create a cycle.

        Port of the check in concept.ConceptService.addPrerequisite: the edge closes a
        cycle exactly when concept is already a transitive prerequisite of prerequisite.
        A self-loop is a degenerate cycle; ConceptService rejects self-loops beforehand
        with a dedicated message (and the table enforces
        CHECK (concept_id <> prerequisite_id)), so callers should check that case first
        if they want the same error message as the Java API.
        """
        if concept_id == prerequisite_id:
            return True
        return any(c.id == concept_id for c in self.all_prerequisites(prerequisite_id))

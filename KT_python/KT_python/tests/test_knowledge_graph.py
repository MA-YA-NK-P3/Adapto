"""Knowledge-graph tests (docs/knowledge-graph.md).

Port of the graph parts of ApiContractIT (seededGraphExposesTransitivePrerequisites,
prerequisiteGraphRejectsSelfLoopsAndCycles) plus unit coverage of the
KnowledgeGraphService traversals on the mini catalog:

    HASHING             requires ARRAYS
    TWO_POINTERS        requires ARRAYS
    DYNAMIC_PROGRAMMING requires ARRAYS, HASHING
    STRINGS             (no edges)
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from knowledge_tracer.domain import Concept, ConceptMastery, Prerequisite
from knowledge_tracer.errors import NotFoundError
from knowledge_tracer.knowledge_graph import KnowledgeGraphService


@pytest.fixture()
def graph(session) -> KnowledgeGraphService:
    return KnowledgeGraphService(session)


def _concept_id(session, code: str):
    return session.scalar(select(Concept).where(Concept.code == code)).id


def _codes(concepts):
    return {c.code for c in concepts}


@pytest.mark.usefixtures("mini_catalog")
class TestPrerequisites:
    def test_direct_prerequisites(self, session, graph):
        dp = _concept_id(session, "DYNAMIC_PROGRAMMING")
        assert _codes(graph.direct_prerequisites(dp)) == {"ARRAYS", "HASHING"}

    def test_direct_prerequisites_of_root_is_empty(self, session, graph):
        arrays = _concept_id(session, "ARRAYS")
        assert graph.direct_prerequisites(arrays) == []

    def test_all_prerequisites_breadth_first_closest_first(self, session, graph):
        # Extend the mini catalog with one deeper edge: ARRAYS requires STRINGS.
        arrays = _concept_id(session, "ARRAYS")
        strings = _concept_id(session, "STRINGS")
        session.add(Prerequisite(concept_id=arrays, prerequisite_id=strings))
        session.flush()

        dp = _concept_id(session, "DYNAMIC_PROGRAMMING")
        ordered = [c.code for c in graph.all_prerequisites(dp)]
        assert set(ordered) == {"ARRAYS", "HASHING", "STRINGS"}
        assert len(ordered) == len(set(ordered))  # each concept reported once
        assert ordered[-1] == "STRINGS"           # the deepest node comes last

    def test_all_prerequisites_of_root_is_empty(self, session, graph):
        arrays = _concept_id(session, "ARRAYS")
        assert graph.all_prerequisites(arrays) == []

    def test_downstream_concepts(self, session, graph):
        arrays = _concept_id(session, "ARRAYS")
        assert _codes(graph.downstream_concepts(arrays)) == {
            "HASHING", "TWO_POINTERS", "DYNAMIC_PROGRAMMING"}
        hashing = _concept_id(session, "HASHING")
        assert _codes(graph.downstream_concepts(hashing)) == {"DYNAMIC_PROGRAMMING"}
        dp = _concept_id(session, "DYNAMIC_PROGRAMMING")
        assert graph.downstream_concepts(dp) == set()

    def test_traversals_are_cycle_safe(self, session, graph):
        # Insert a cycle directly into the database (the service-level check in
        # ConceptService is bypassed; the DB only forbids self-loops).
        arrays = _concept_id(session, "ARRAYS")
        dp = _concept_id(session, "DYNAMIC_PROGRAMMING")
        session.add(Prerequisite(concept_id=arrays, prerequisite_id=dp))  # ARRAYS -> DP -> ARRAYS
        session.flush()

        ordered = [c.code for c in graph.all_prerequisites(arrays)]
        assert set(ordered) == {"DYNAMIC_PROGRAMMING", "HASHING"}
        assert len(ordered) == 2  # terminates, each concept exactly once

        downstream = _codes(graph.downstream_concepts(_concept_id(session, "HASHING")))
        assert downstream == {"DYNAMIC_PROGRAMMING", "ARRAYS", "TWO_POINTERS"}


@pytest.mark.usefixtures("mini_catalog")
class TestCycleCheck:
    """The would-create-cycle logic of ConceptService.addPrerequisite (ApiContractIT)."""

    def test_direct_cycle_is_detected(self, session, graph):
        # HASHING requires ARRAYS already; adding "ARRAYS requires HASHING" closes a cycle.
        arrays = _concept_id(session, "ARRAYS")
        hashing = _concept_id(session, "HASHING")
        assert graph.would_create_cycle(arrays, hashing)

    def test_transitive_cycle_is_detected(self, session, graph):
        # DYNAMIC_PROGRAMMING transitively requires ARRAYS, so "ARRAYS requires
        # DYNAMIC_PROGRAMMING" would close a cycle through HASHING/ARRAYS.
        arrays = _concept_id(session, "ARRAYS")
        dp = _concept_id(session, "DYNAMIC_PROGRAMMING")
        assert graph.would_create_cycle(arrays, dp)

    def test_valid_edge_does_not_create_cycle(self, session, graph):
        two_pointers = _concept_id(session, "TWO_POINTERS")
        hashing = _concept_id(session, "HASHING")
        assert not graph.would_create_cycle(two_pointers, hashing)

    def test_self_loop_is_a_degenerate_cycle(self, session, graph):
        arrays = _concept_id(session, "ARRAYS")
        assert graph.would_create_cycle(arrays, arrays)


@pytest.mark.usefixtures("mini_catalog")
class TestGetByCode:
    def test_found(self, session, graph):
        assert graph.get_by_code("ARRAYS").code == "ARRAYS"

    def test_missing_raises_not_found(self, graph):
        with pytest.raises(NotFoundError, match="Concept not found: NOPE"):
            graph.get_by_code("NOPE")


def _mastery(concept_id, mastery: float, evidence: int) -> ConceptMastery:
    """A transient mastery row; weakest_prerequisite never reads it from the DB."""
    return ConceptMastery(concept_id=concept_id, alpha=mastery, beta=1.0 - mastery,
                          mastery=mastery, evidence_count=evidence)


@pytest.mark.usefixtures("mini_catalog")
class TestWeakestPrerequisite:
    def test_no_prerequisites_returns_none(self, session, graph):
        arrays = _concept_id(session, "ARRAYS")
        assert graph.weakest_prerequisite(arrays, []) is None

    def test_unobserved_prerequisites_count_as_half(self, session, graph):
        dp = _concept_id(session, "DYNAMIC_PROGRAMMING")
        weakness = graph.weakest_prerequisite(dp, [])
        assert weakness is not None
        assert weakness.mastery == 0.5        # uninformative prior
        assert weakness.evidence_count == 0

    def test_measured_weakness_beats_uninformative_prior(self, session, graph):
        dp = _concept_id(session, "DYNAMIC_PROGRAMMING")
        hashing = _concept_id(session, "HASHING")
        weakness = graph.weakest_prerequisite(dp, [_mastery(hashing, 0.4, 2)])
        assert weakness.concept.code == "HASHING"   # 0.4 < 0.5 (unobserved ARRAYS)
        assert weakness.mastery == 0.4
        assert weakness.evidence_count == 2

    def test_unobserved_beats_measured_strength(self, session, graph):
        dp = _concept_id(session, "DYNAMIC_PROGRAMMING")
        hashing = _concept_id(session, "HASHING")
        weakness = graph.weakest_prerequisite(dp, [_mastery(hashing, 0.6, 2)])
        assert weakness.concept.code == "ARRAYS"    # unobserved: 0.5 < 0.6
        assert weakness.mastery == 0.5
        assert weakness.evidence_count == 0

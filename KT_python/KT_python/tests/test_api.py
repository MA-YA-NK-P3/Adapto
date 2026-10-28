"""HTTP contract and end-to-end learning-loop tests for the FastAPI replica.

Ports of the Java integration suites, driven over HTTP through the TestClient
(httpx) against an app built on a shared in-memory SQLite container seeded with the
conftest mini catalog:

  * api.ApiContractIT        (19 tests) — status codes, response/error structure
  * pipeline.LearningLoopIT  (12 tests) — the full knowledge-tracing loop over HTTP
  * api.ApiKeyIT             (2 tests)  — X-API-Key enabled/disabled (docs/security.md)
  * research.ResearchExportIT (3 tests) — NDJSON export gating and pseudonymisation
  * plus DKT model status with the real artifact, CORS, and the dashboard routes

Java tests that asserted catalog-seeded LeetCode metadata (frontendId, slug,
topicTags on LC1, the 1000-problem list) are adapted to the mini catalog: the
CreateProblemRequest DTO has no frontendId/slug fields, so those come only from the
seed file, which the container deliberately does not load here. The
acceptance-adjusted prior assertion (docs/irt.md: MEDIUM + acceptanceRate 0.58 ->
priorDifficulty 4.94) is kept verbatim via the API.

Mirroring AbstractIntegrationTest: unique learner ids isolate tests; a shared fake
clock (strictly increasing timestamps) makes "most recent" ordering deterministic.
"""
from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from knowledge_tracer.api import create_app
from knowledge_tracer.config import Settings
from knowledge_tracer.container import Container
from knowledge_tracer.domain import (Concept, Prerequisite, Problem,
                                     ProblemConcept)
from tests.conftest import CONCEPTS, PROBLEMS

ROOT = Path(__file__).resolve().parents[1]          # KT_python/
REPO = ROOT.parent                                  # Knowledge_Tracing/
DKT_MODEL = REPO / "model" / "artifacts" / "dkt" / "dkt-model.json"

#: Shared fake clock (AbstractIntegrationTest.CLOCK): strictly increasing
#: timestamps so "most recent" ordering is deterministic.
_CLOCK = itertools.count()
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _next_timestamp() -> str:
    dt = _EPOCH + timedelta(seconds=60 * next(_CLOCK))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def unique_user(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex[:8]


def event_payload(user: str, problem: str, result: str, attempt: int = 1,
                  **extra) -> dict:
    """AbstractIntegrationTest.event(): the v1 event schema with a client eventId."""
    payload = {
        "userId": user,
        "problemId": problem,
        "eventId": str(uuid.uuid4()),
        "timestamp": _next_timestamp(),
        "attemptNumber": attempt,
        "result": result,
        "timeSpentSeconds": 900,
    }
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------
# App / client fixtures
# --------------------------------------------------------------------------

def _seed_mini_catalog(container: Container) -> None:
    """The conftest mini catalog, loaded through the container's own session so the
    TestClient's worker threads see it (identical rows to conftest.mini_catalog)."""
    with container.session_scope() as scope:
        session = scope.session
        ids = {}
        for code in CONCEPTS:
            concept = Concept(code=code, name=code.replace("_", " ").title())
            session.add(concept)
            session.flush()
            ids[code] = concept.id
        for code, prereqs in CONCEPTS.items():
            for p in prereqs:
                session.add(Prerequisite(concept_id=ids[code],
                                         prerequisite_id=ids[p]))
        for key, label, prior, acceptance, tags in PROBLEMS:
            problem = Problem(problem_key=key, title=key, static_difficulty=label,
                              acceptance_rate=acceptance, paid_only=(key == "LC_PAID"),
                              prior_difficulty=prior, estimated_difficulty=prior,
                              difficulty_uncertainty=1.5, sample_size=0)
            session.add(problem)
            session.flush()
            for code, weight in tags.items():
                session.add(ProblemConcept(problem_id=problem.id,
                                           concept_id=ids[code], weight=weight))


def make_client(*, mini: bool = True, api_key: str = "", export: bool = False,
                salt: Optional[str] = None, dkt: bool = False,
                origins: Optional[list] = None) -> Tuple[TestClient, Container]:
    """An app + container on one shared in-memory SQLite database (StaticPool in the
    container keeps the single connection shared with the TestClient's threads)."""
    settings = Settings()
    settings.database_url = "sqlite://"
    settings.security.api_key = api_key
    settings.research.export_enabled = export
    if salt is not None:
        settings.research.pseudonym_salt = salt
    if dkt:
        settings.dkt.enabled = True
        settings.dkt.model_path = str(DKT_MODEL)
    if origins is not None:
        settings.security.allowed_origins = origins
    container = Container(settings, seed_catalog=False)
    if mini:
        _seed_mini_catalog(container)
    return TestClient(create_app(container)), container


@pytest.fixture()
def client() -> TestClient:
    return make_client()[0]


# --- AbstractIntegrationTest helpers -----------------------------------------

def send_event(client: TestClient, user: str, problem: str, result: str,
               attempt: int = 1) -> dict:
    r = client.post("/api/events", json=event_payload(user, problem, result, attempt))
    assert r.status_code == 201, "event rejected: " + r.text
    return r.json()


def state(client: TestClient, user: str) -> dict:
    r = client.get("/api/learners/%s/state" % user)
    assert r.status_code == 200, r.text
    return r.json()


def next_action(client: TestClient, user: str) -> dict:
    r = client.get("/api/learners/%s/next-action" % user)
    assert r.status_code == 200, "next-action failed: " + r.text
    return r.json()


def assert_error(response, status: int, error: str) -> dict:
    """ApiContractIT.assertError: the ApiError record shape (master plan §43)."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["status"] == status
    assert body["error"] == error
    assert body["timestamp"] is not None
    assert body["message"] is not None
    assert body["path"] is not None
    assert isinstance(body["details"], list)
    return body


def assert_references_existing_problem(client: TestClient, next_body: dict) -> dict:
    """LearningLoopIT.assertRecommendationReferencesExistingProblem."""
    problem_id = next_body["recommendation"]["problemId"]
    assert problem_id
    assert 0.0 <= next_body["recommendation"]["difficulty"] <= 10.0
    r = client.get("/api/problems/" + problem_id)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# ApiContractIT — HTTP status codes, response structure, error structure
# --------------------------------------------------------------------------

class TestApiContract:
    def test_health_endpoint_is_up(self, client):
        assert client.get("/actuator/health").json()["status"] == "UP"

    def test_post_event_returns_201_with_stored_event(self, client):
        user = unique_user("api")
        r = client.post("/api/events",
                        json=event_payload(user, "LC_E1", "ACCEPTED"))
        assert r.status_code == 201
        body = r.json()
        assert body["userId"] == user
        assert body["problemId"] == "LC_E1"
        assert body["duplicate"] is False
        assert body["eventId"] is not None
        assert body["id"] is not None
        assert body["result"] == "ACCEPTED"
        assert body["timestamp"] is not None

    def test_invalid_event_returns_400_with_field_details(self, client):
        r = client.post("/api/events",
                        json={"userId": "", "problemId": "LC_E1", "attemptNumber": 0})
        body = assert_error(r, 400, "VALIDATION_ERROR")
        details = str(body["details"])
        for field in ("userId", "attemptNumber", "timestamp", "result"):
            assert field in details

    def test_malformed_json_and_unknown_enum_return_400(self, client):
        r = client.post("/api/events", content="{not json",
                        headers={"Content-Type": "application/json"})
        assert_error(r, 400, "MALFORMED_REQUEST")
        bad_enum = event_payload(unique_user("enum"), "LC_E1", "MAYBE")
        assert_error(client.post("/api/events", json=bad_enum),
                     400, "MALFORMED_REQUEST")

    def test_unknown_problem_returns_422(self, client):
        r = client.post("/api/events",
                        json=event_payload(unique_user("u"), "LC_NOPE", "ACCEPTED"))
        assert_error(r, 422, "BUSINESS_RULE_VIOLATION")

    def test_unknown_learner_returns_404_on_read_endpoints(self, client):
        for path in ("state", "next-action", "history", "recommendations"):
            assert_error(client.get("/api/learners/GHOST-%s/%s" % (path, path)),
                         404, "NOT_FOUND")
        assert_error(client.get("/api/problems/LC_NOPE"), 404, "NOT_FOUND")
        assert_error(client.get("/api/does-not-exist"), 404, "NOT_FOUND")

    def test_state_response_has_documented_structure(self, client):
        user = unique_user("shape")
        send_event(client, user, "LC_E1", "ACCEPTED")
        s = state(client, user)
        for field in ("userId", "ability", "uncertainty", "calibrated",
                      "totalEvents", "concepts"):
            assert field in s, field
        arrays = s["concepts"]["ARRAYS"]
        assert "mastery" in arrays
        assert "uncertainty" in arrays
        assert "evidenceCount" in arrays

    def test_next_action_response_has_documented_structure(self, client):
        user = unique_user("shape2")
        send_event(client, user, "LC_M3", "WRONG_ANSWER")
        n = next_action(client, user)
        for field in ("userId", "state", "knowledge", "gap", "recommendation",
                      "feedback", "models"):
            assert field in n, field
        for field in ("type", "problemId", "difficulty", "reason", "evidence",
                      "confidence", "decidedBy", "fallbackUsed"):
            assert field in n["recommendation"], "recommendation." + field
        assert isinstance(n["knowledge"]["hashing"], float)
        assert n["models"]["dkt"].startswith("DISABLED")
        assert 0.0 <= n["recommendation"]["confidence"] <= 1.0

    def test_duplicate_problem_and_concept_return_409(self, client):
        assert_error(client.post("/api/problems", json={
            "problemKey": "LC_E1", "title": "dup", "staticDifficulty": "EASY"}),
            409, "CONFLICT")
        assert_error(client.post("/api/concepts",
                                 json={"code": "ARRAYS", "name": "dup"}),
                     409, "CONFLICT")

    def test_problem_with_unknown_concept_returns_404(self, client):
        # Java parity: ProblemService looks the concept up and 404s (docs/api-flow.md
        # §5.7) — it is NOT a 422.
        key = "LCU" + uuid.uuid4().hex[:8].upper()
        assert_error(client.post("/api/problems", json={
            "problemKey": key, "title": "x", "staticDifficulty": "EASY",
            "concepts": [{"conceptCode": "NOT_A_CONCEPT"}]}), 404, "NOT_FOUND")

    def test_prerequisite_graph_rejects_self_loops_and_cycles(self, client):
        a = "CA_" + uuid.uuid4().hex[:8].upper()
        b = "CB_" + uuid.uuid4().hex[:8].upper()
        assert client.post("/api/concepts", json={"code": a, "name": "A"}).status_code == 201
        assert client.post("/api/concepts", json={"code": b, "name": "B"}).status_code == 201

        edge = client.post("/api/concepts/%s/prerequisites" % b,
                           json={"prerequisiteCode": a})
        assert edge.status_code == 201
        assert a in edge.json()["directPrerequisites"]

        assert_error(client.post("/api/concepts/%s/prerequisites" % a,
                                 json={"prerequisiteCode": b}),
                     422, "BUSINESS_RULE_VIOLATION")
        assert_error(client.post("/api/concepts/%s/prerequisites" % a,
                                 json={"prerequisiteCode": a}),
                     422, "BUSINESS_RULE_VIOLATION")
        assert_error(client.post("/api/concepts/%s/prerequisites" % b,
                                 json={"prerequisiteCode": a}),
                     409, "CONFLICT")
        assert_error(client.post("/api/concepts",
                                 json={"code": "lower case", "name": "x"}),
                     400, "VALIDATION_ERROR")

    def test_problem_exposes_weighted_tags_primary_first(self, client):
        # Mini-catalog analogue of the Java LC104 assertion (TREES 1.0, RECURSION 0.8).
        lc_e2 = client.get("/api/problems/LC_E2").json()
        assert lc_e2["conceptTags"][0]["code"] == "HASHING"
        assert lc_e2["conceptTags"][0]["weight"] == 0.9
        assert lc_e2["conceptTags"][1]["code"] == "ARRAYS"
        assert lc_e2["conceptTags"][1]["weight"] == 0.6
        assert lc_e2["concepts"] == ["ARRAYS", "HASHING"]

    def test_seeded_graph_exposes_transitive_prerequisites(self, client):
        # Mini-catalog analogue of the Java GRAPHS assertion: HASHING -> ARRAYS is
        # direct; adding STRINGS -> DYNAMIC_PROGRAMMING exposes the transitive chain
        # STRINGS -> DP -> HASHING -> ARRAYS through the API.
        hashing = client.get("/api/concepts/HASHING").json()
        assert hashing["directPrerequisites"] == ["ARRAYS"]
        assert hashing["allPrerequisites"] == ["ARRAYS"]

        r = client.post("/api/concepts/STRINGS/prerequisites",
                        json={"prerequisiteCode": "DYNAMIC_PROGRAMMING"})
        assert r.status_code == 201
        strings = client.get("/api/concepts/STRINGS").json()
        assert strings["directPrerequisites"] == ["DYNAMIC_PROGRAMMING"]
        assert "HASHING" in strings["allPrerequisites"]
        assert "ARRAYS" in strings["allPrerequisites"]

    def test_feedback_is_recorded_and_validated(self, client):
        user = unique_user("fb")
        send_event(client, user, "LC_M3", "WRONG_ANSWER")
        rec_id = next_action(client, user)["recommendation"]["recommendationId"]

        ok = client.post("/api/feedback", json={
            "userId": user, "problemId": "LC_M3", "recommendationId": rec_id,
            "answers": [{"question": "What prevented you?",
                         "answer": "pointer updates"}]})
        assert ok.status_code == 201, ok.text
        assert ok.json()["status"] == "RECORDED"
        assert ok.json()["feedbackId"] is not None

        assert_error(client.post("/api/feedback",
                                 json={"userId": user, "answers": []}),
                     400, "VALIDATION_ERROR")
        assert_error(client.post("/api/feedback", json={
            "userId": "GHOST-fb",
            "answers": [{"question": "q", "answer": "a"}]}), 404, "NOT_FOUND")

    def test_wrong_method_returns_405(self, client):
        assert_error(client.get("/api/events"), 405, "METHOD_NOT_ALLOWED")

    def test_non_json_content_type_returns_415(self, client):
        r = client.post("/api/events", content="hello",
                        headers={"Content-Type": "text/plain"})
        assert_error(r, 415, "UNSUPPORTED_MEDIA_TYPE")

    def test_non_positive_limit_returns_400(self, client):
        user = unique_user("limit")
        send_event(client, user, "LC_E1", "ACCEPTED")
        for path in ("history?limit=0", "history?limit=-5", "recommendations?limit=0"):
            r = client.get("/api/learners/%s/%s" % (user, path))
            body = assert_error(r, 400, "VALIDATION_ERROR")
            assert "limit" in str(body["details"]), path
        # Parameter validation runs before the export guards, as in Java.
        assert_error(client.get("/api/research/decisions?limit=0"),
                     400, "VALIDATION_ERROR")
        assert client.get("/api/learners/%s/history?limit=1" % user).status_code == 200

    def test_acceptance_adjusted_prior_and_leetcode_metadata(self, client):
        """Adapted from catalogCarriesLeetCodeMetadataAndAcceptanceAdjustedPrior: the
        mini catalog carries no frontendId/slug (the CreateProblemRequest DTO has no
        such fields — they only come from the seed file), so this keeps the portable
        parts: the acceptance-adjusted prior (docs/irt.md: MEDIUM + 0.58 -> 4.94),
        paidOnly, topicTags round-trip and the list endpoint."""
        key = "LCM" + uuid.uuid4().hex[:8].upper()
        created = client.post("/api/problems", json={
            "problemKey": key, "title": "Meta probe", "staticDifficulty": "MEDIUM",
            "url": "https://leetcode.com/problems/meta-probe/",
            "acceptanceRate": 0.58, "paidOnly": True,
            "topicTags": ["Hash Table", "Two Pointers"],
            "concepts": [{"conceptCode": "ARRAYS"}]})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["priorDifficulty"] == 4.94
        assert body["estimatedDifficulty"] == 4.94
        assert body["staticDifficulty"] == "MEDIUM"
        assert body["paidOnly"] is True
        assert body["topicTags"] == ["Hash Table", "Two Pointers"]
        assert body["frontendId"] is None
        assert body["slug"] is None
        assert body["concepts"] == ["ARRAYS"]

        fetched = client.get("/api/problems/" + key)
        assert fetched.status_code == 200
        assert fetched.json()["priorDifficulty"] == 4.94
        assert len(client.get("/api/problems").json()) >= len(PROBLEMS)

    def test_research_export_off_by_default_and_model_status_public(self, client):
        assert_error(client.get("/api/research/events"), 404, "NOT_FOUND")
        assert_error(client.get("/api/research/catalog"), 404, "NOT_FOUND")
        dkt = client.get("/api/models/dkt").json()
        assert dkt["status"] == "DISABLED"
        assert dkt["enabled"] is False
        assert dkt["available"] is False
        jev = client.get("/api/models/jev").json()
        assert jev["provider"] == "deterministic"
        assert len(jev["bandit"]["arms"]) == 8
        assert jev["bandit"]["arms"][0]["arm"] == "CALIBRATION_PROBLEM"
        assert jev["bandit"]["arms"][0]["meanReward"] is None
        assert len(jev["bandit"]["arms"][0]["theta"]) == 8
        assert jev["bandit"]["features"][0] == "bias"


# --------------------------------------------------------------------------
# LearningLoopIT — the end-to-end knowledge-tracing loop over HTTP
# --------------------------------------------------------------------------

class TestLearningLoop:
    def test_new_learner_with_zero_history_gets_calibration_item(self, client):
        user = unique_user("new")
        assert client.get("/api/learners/%s/state" % user).status_code == 404

        assert client.post("/api/learners",
                           json={"externalRef": user}).status_code == 201
        assert client.post("/api/learners",
                           json={"externalRef": user}).status_code == 200

        s = state(client, user)
        assert s["ability"] == 5.0
        assert s["uncertainty"] == 2.0
        assert s["totalEvents"] == 0
        assert s["calibrated"] is False
        assert len(s["concepts"]) == 0

        nxt = next_action(client, user)
        assert nxt["gap"]["type"] == "UNKNOWN"
        assert nxt["recommendation"]["type"] == "CALIBRATION_PROBLEM"
        assert_references_existing_problem(client, nxt)

    def test_sparse_learner_is_flagged_as_insufficient_practice(self, client):
        user = unique_user("sparse")
        send_event(client, user, "LC_E1", "ACCEPTED")
        s = state(client, user)
        assert s["totalEvents"] == 1
        assert s["ability"] > 5.0
        assert s["uncertainty"] < 2.0

        nxt = next_action(client, user)
        assert nxt["gap"]["type"] == "INSUFFICIENT_PRACTICE"
        assert nxt["feedback"]["required"] is False

    def test_repeated_failure_is_detected_as_concept_gap_and_remediated(self, client):
        """Mini-catalog port of repeatedFailureOnLinkedListIsDetectedAsConceptGapAndRemediated
        (HASHING plays the LINKED_LIST role; the proven sequence of
        test_recommendation.test_concept_gap_is_remediated)."""
        user = unique_user("ll")
        send_event(client, user, "LC_E1", "ACCEPTED")
        send_event(client, user, "LC_M3", "WRONG_ANSWER")
        send_event(client, user, "LC_M3", "WRONG_ANSWER", attempt=2)

        s = state(client, user)
        assert s["concepts"]["HASHING"]["mastery"] < 0.45
        assert "TWO_POINTERS" not in s["concepts"], "no evidence = unknown, not weak"

        nxt = next_action(client, user)
        assert nxt["gap"]["type"] == "CONCEPT_GAP"
        assert nxt["gap"]["concept"] == "HASHING"
        assert "HASHING mastery estimate" in str(nxt["gap"]["evidence"])
        assert nxt["recommendation"]["type"] == "EASIER_PROBLEM"
        assert nxt["recommendation"]["topic"] == "HASHING"
        assert nxt["recommendation"]["decidedBy"] == "JEV"
        assert nxt["feedback"]["required"] is True

        problem = assert_references_existing_problem(client, nxt)
        assert "HASHING" in problem["concepts"]
        assert problem["problemId"] != "LC_E1"

    def test_concept_specific_failure_does_not_destroy_global_ability(self, client):
        user = unique_user("strong")
        for p in ("LC_E1", "LC_E3", "LC_E4", "LC_M1", "LC_M2", "LC_M4"):
            send_event(client, user, p, "ACCEPTED")
        ability_before = state(client, user)["ability"]
        for attempt in (1, 2, 3):
            send_event(client, user, "LC_M3", "WRONG_ANSWER", attempt=attempt)

        s = state(client, user)
        assert ability_before > 5.5
        # Rasch is unidimensional, so concept failures do lower global ability
        # somewhat (docs/limitations.md) — but not into the LOW_GLOBAL_ABILITY
        # region (< 3.0, the gap-detection rule).
        assert s["ability"] < ability_before
        assert s["ability"] > 3.0
        assert s["concepts"]["ARRAYS"]["mastery"] > 0.7
        assert s["concepts"]["HASHING"]["mastery"] < 0.45

        nxt = next_action(client, user)
        assert nxt["gap"]["concept"] == "HASHING"
        assert nxt["gap"]["type"] != "LOW_GLOBAL_ABILITY"

    def test_weak_prerequisite_is_detected_and_reviewed_first(self, client):
        """Mini-catalog port (DYNAMIC_PROGRAMMING plays TREES, HASHING its weak
        prerequisite; the proven sequence of test_knowledge_gap.test_prerequisite_gap)."""
        user = unique_user("prereq")
        send_event(client, user, "LC_H1", "WRONG_ANSWER")
        send_event(client, user, "LC_H1", "WRONG_ANSWER", attempt=2)
        send_event(client, user, "LC_M3", "WRONG_ANSWER")

        nxt = next_action(client, user)
        assert nxt["gap"]["type"] == "PREREQUISITE_GAP"
        assert nxt["gap"]["concept"] == "DYNAMIC_PROGRAMMING"
        prerequisite = nxt["gap"]["prerequisite"]
        assert prerequisite == "HASHING"
        assert nxt["recommendation"]["type"] == "PREREQUISITE_REVIEW"
        assert nxt["recommendation"]["topic"] == prerequisite
        problem = assert_references_existing_problem(client, nxt)
        assert prerequisite in problem["concepts"]

    def test_repeated_success_on_same_concept_raises_mastery_monotonically(self, client):
        user = unique_user("rep")
        previous = 0.5
        for p in ("LC_E1", "LC_E2", "LC_E4", "LC_M2"):
            send_event(client, user, p, "ACCEPTED")
            mastery = state(client, user)["concepts"]["ARRAYS"]["mastery"]
            assert mastery > previous
            previous = mastery
        assert next_action(client, user)["gap"]["type"] != "CONCEPT_GAP"

    def test_duplicate_event_does_not_corrupt_learner_state(self, client):
        user = unique_user("dup")
        payload = event_payload(user, "LC_M3", "WRONG_ANSWER")
        assert client.post("/api/events", json=payload).status_code == 201
        before = state(client, user)

        for _ in range(3):
            replay = client.post("/api/events", json=payload)
            assert replay.status_code == 200
            assert replay.json()["duplicate"] is True
        assert state(client, user) == before
        assert len(client.get("/api/learners/%s/history" % user).json()) == 1

    def test_event_id_reused_for_another_learner_or_problem_is_a_conflict(self, client):
        user = unique_user("reuse")
        payload = event_payload(user, "LC_M3", "WRONG_ANSWER")
        assert client.post("/api/events", json=payload).status_code == 201

        stranger = unique_user("stranger")
        other_learner = dict(payload, userId=stranger)
        clash = client.post("/api/events", json=other_learner)
        assert clash.status_code == 409
        assert clash.json()["status"] == 409
        # The conflict is checked BEFORE any learner is created.
        assert client.get("/api/learners/%s/state" % stranger).status_code == 404

        other_problem = dict(payload, problemId="LC_E1")
        assert client.post("/api/events", json=other_problem).status_code == 409
        assert len(client.get("/api/learners/%s/history" % user).json()) == 1

    def test_problem_without_concept_metadata_is_accepted(self, client):
        key = "LCX" + uuid.uuid4().hex[:8].upper()
        created = client.post("/api/problems", json={
            "problemKey": key, "title": "Untagged problem",
            "staticDifficulty": "MEDIUM"})
        assert created.status_code == 201
        assert created.json()["estimatedDifficulty"] == 5.0
        assert len(created.json()["concepts"]) == 0

        user = unique_user("nometa")
        send_event(client, user, key, "WRONG_ANSWER")
        s = state(client, user)
        assert s["totalEvents"] == 1
        assert len(s["concepts"]) == 0
        assert_references_existing_problem(client, next_action(client, user))

    def test_timeouts_are_reported_as_time_pressure(self, client):
        """The proven sequence of test_knowledge_gap.test_time_pressure: timeouts on
        problems whose concepts have no other weak evidence."""
        user = unique_user("tle")
        for p in ("LC_E1", "LC_E3", "LC_M1", "LC_M3"):
            send_event(client, user, p, "ACCEPTED")
        send_event(client, user, "LC_H1", "TIME_LIMIT_EXCEEDED")
        send_event(client, user, "LC_M3", "TIME_LIMIT_EXCEEDED", attempt=2)
        assert next_action(client, user)["gap"]["type"] == "TIME_PRESSURE"

    def test_next_interaction_becomes_new_evidence(self, client):
        user = unique_user("loop")
        send_event(client, user, "LC_M3", "WRONG_ANSWER")
        send_event(client, user, "LC_M3", "WRONG_ANSWER", attempt=2)
        first = next_action(client, user)
        recommended = first["recommendation"]["problemId"]
        topic = first["recommendation"]["topic"]
        assert topic, "gap-directed recommendation carries its concept"
        mastery_before = state(client, user)["concepts"][topic]["mastery"]

        send_event(client, user, recommended, "ACCEPTED")

        s = state(client, user)
        assert s["totalEvents"] == 3
        assert s["concepts"][topic]["mastery"] > mastery_before
        history = client.get("/api/learners/%s/recommendations" % user).json()
        assert len(history) == 1
        assert history[0]["problemId"] == recommended

    def test_item_difficulty_is_recalibrated_from_evidence(self, client):
        key = "LCC" + uuid.uuid4().hex[:8].upper()
        client.post("/api/problems", json={
            "problemKey": key, "title": "Calibration probe",
            "staticDifficulty": "EASY",
            "concepts": [{"conceptCode": "ARRAYS"}]})
        for _ in range(4):
            send_event(client, unique_user("cal"), key, "WRONG_ANSWER")
        p = client.get("/api/problems/" + key).json()
        assert p["staticDifficulty"] == "EASY"
        assert p["estimatedDifficulty"] > 3.0
        assert p["difficultyUncertainty"] < 1.5
        assert p["sampleSize"] == 4


# --------------------------------------------------------------------------
# ApiKeyIT — optional shared API key and correlation ids (docs/security.md)
# --------------------------------------------------------------------------

class TestApiKey:
    def test_api_requires_key_but_health_does_not(self):
        client, _ = make_client(api_key="test-secret")
        denied = client.post("/api/learners",
                             json={"externalRef": unique_user("key")})
        assert denied.status_code == 401
        assert denied.json()["error"] == "UNAUTHORIZED"

        wrong = client.post("/api/learners",
                            json={"externalRef": unique_user("key")},
                            headers={"X-API-Key": "nope"})
        assert wrong.status_code == 401

        allowed = client.post("/api/learners",
                              json={"externalRef": unique_user("key")},
                              headers={"X-API-Key": "test-secret"})
        assert allowed.status_code == 201
        assert allowed.headers.get("X-Request-Id")

        assert client.get("/actuator/health").status_code == 200

    def test_api_is_open_when_key_blank(self):
        client, _ = make_client()  # api_key="" disables the filter (local dev)
        r = client.post("/api/learners", json={"externalRef": unique_user("open")})
        assert r.status_code == 201
        assert r.headers.get("X-Request-Id")

    def test_client_supplied_safe_request_id_is_echoed(self):
        client, _ = make_client()
        r = client.get("/actuator/health", headers={"X-Request-Id": "req-123.abc"})
        assert r.headers["X-Request-Id"] == "req-123.abc"
        # An unsafe id is replaced with a generated UUID (log-injection guard).
        r = client.get("/actuator/health", headers={"X-Request-Id": "bad id\r\n"})
        assert r.headers["X-Request-Id"] != "bad id\r\n"
        uuid.UUID(r.headers["X-Request-Id"])


# --------------------------------------------------------------------------
# ResearchExportIT — NDJSON export, pseudonymised (docs/research.md)
# --------------------------------------------------------------------------

def _ndjson_lines(response) -> list:
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


class TestResearchExport:
    def test_events_exported_as_pseudonymised_kt_events_v1(self):
        client, _ = make_client(export=True, salt="test-salt")
        user = unique_user("export")
        send_event(client, user, "LC_E1", "ACCEPTED")
        send_event(client, user, "LC_M3", "WRONG_ANSWER")
        recommended = next_action(client, user)["recommendation"]["problemId"]
        assert recommended

        r = client.get("/api/research/events")
        assert r.headers["content-type"].startswith("application/x-ndjson")
        assert r.headers["content-disposition"] == 'attachment; filename="events.jsonl"'
        lines = _ndjson_lines(r)
        learner_id = None
        for e in lines:
            assert e["schema"] == "kt-events-v1"
            assert "export-" not in json.dumps(e)
            if (e["problemId"] == "LC_M3" and e["result"] == "WRONG_ANSWER"
                    and e["seq"] == 1):
                learner_id = e["learnerId"]
        assert learner_id is not None and learner_id.startswith("L-")

        mine = [e for e in _ndjson_lines(client.get("/api/research/events"))
                if e["learnerId"] == learner_id]
        assert len(mine) == 2
        assert mine[0]["problemId"] == "LC_E1"
        assert mine[0]["correct"] == 1
        assert mine[1]["correct"] == 0

        # Only rewarded decisions are exported; the reward is attributed on the next
        # attempt at the recommended problem.
        send_event(client, user, recommended, "ACCEPTED")
        r = client.get("/api/research/decisions?limit=500")
        assert r.headers["content-disposition"] == \
            'attachment; filename="decisions.jsonl"'
        decisions = _ndjson_lines(r)
        assert any(d["schema"] == "kt-decisions-v1"
                   and d["learnerId"] == learner_id for d in decisions)

    def test_catalog_export_lists_the_mini_catalog(self):
        client, _ = make_client(export=True, salt="test-salt")
        catalog = client.get("/api/research/catalog").json()
        assert len(catalog["problems"]) == len(PROBLEMS)
        assert len(catalog["concepts"]) == len(CONCEPTS)
        lc_e2 = next(p for p in catalog["problems"] if p["problemId"] == "LC_E2")
        assert lc_e2["conceptWeights"]["HASHING"] == 0.9
        assert lc_e2["priorDifficulty"] == 3.2

    def test_insecure_salt_returns_422(self):
        client, _ = make_client(export=True)  # default salt "change-me" is insecure
        assert_error(client.get("/api/research/events"), 422,
                     "BUSINESS_RULE_VIOLATION")
        assert_error(client.get("/api/research/decisions"), 422,
                     "BUSINESS_RULE_VIOLATION")
        assert_error(client.get("/api/research/catalog"), 422,
                     "BUSINESS_RULE_VIOLATION")


# --------------------------------------------------------------------------
# DKT model status with the real artifact (DktEnabledIT's status assertions)
# --------------------------------------------------------------------------

@pytest.mark.skipif(not DKT_MODEL.is_file(), reason="DKT model artifact not present")
class TestDktModelStatus:
    def test_dkt_status_reports_the_loaded_model(self):
        client, _ = make_client(dkt=True)
        dkt = client.get("/api/models/dkt").json()
        assert dkt["enabled"] is True
        assert dkt["available"] is True
        assert dkt["status"].startswith("ENABLED")
        assert dkt["hiddenSize"] == 64
        assert dkt["maxSequenceLength"] == 200
        assert len(dkt["concepts"]) == 25
        assert dkt["trainedOn"] == "synthetic"
        assert dkt["testAuc"] is not None

        user = unique_user("dkt")
        for p in ("LC_E1", "LC_E3", "LC_M1"):
            send_event(client, user, p, "ACCEPTED")
        nxt = next_action(client, user)
        assert nxt["models"]["dkt"].startswith("ENABLED")
        assert len(nxt["dktKnowledge"]) > 0


# --------------------------------------------------------------------------
# CORS (config.WebConfig) and the dashboard
# --------------------------------------------------------------------------

class TestCorsAndDashboard:
    def test_cors_preflight_only_when_origins_configured(self):
        client, _ = make_client(origins=["http://example.com"])
        preflight = client.options(
            "/api/learners",
            headers={"Origin": "http://example.com",
                     "Access-Control-Request-Method": "POST"})
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == \
            "http://example.com"

        open_client, _ = make_client()  # no origins -> no CORS mapping, as in Java
        preflight = open_client.options(
            "/api/learners",
            headers={"Origin": "http://example.com",
                     "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in preflight.headers

    def test_dashboard_is_served_and_root_redirects(self, client):
        for path in ("/dashboard", "/dashboard.html"):
            r = client.get(path)
            assert r.status_code == 200
            assert "text/html" in r.headers["content-type"]
            assert "Knowledge Tracer" in r.text
        root = client.get("/", follow_redirects=False)
        assert root.status_code in (302, 307)
        assert root.headers["location"] == "/dashboard"

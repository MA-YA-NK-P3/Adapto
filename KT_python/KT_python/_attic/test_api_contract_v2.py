"""HTTP API contract tests for knowledge_tracer.api (the FastAPI layer).

Driven through fastapi.testclient.TestClient against apps built by create_app in all
three supported shapes:

  * create_app(container)                 — a Container on shared in-memory SQLite,
                                            seeded with the conftest mini catalog
  * create_app(settings, database_url)    — explicit Settings + "sqlite://"
  * create_app()                          — zero-arg factory (KT_DATABASE_URL pinned
                                            to a tmp file by monkeypatch)

Covers the task contract: learner creation, event ingestion with idempotent replay
and eventId conflicts, ApiError-shaped 400/401/404/409/415/422, the next-action
explanation fields, problem/concept CRUD round-trips, model status, NDJSON research
exports, and feedback via a dependency-overridden fake FeedbackService (plus the real
one end to end).
"""
from __future__ import annotations

import itertools
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from knowledge_tracer.api import create_app
from knowledge_tracer.api.routes_feedback import get_feedback_service
from knowledge_tracer.config import Settings
from knowledge_tracer.container import Container
from knowledge_tracer.domain import Concept, Prerequisite, Problem, ProblemConcept
from tests.conftest import CONCEPTS, PROBLEMS

_CLOCK = itertools.count()
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ts() -> str:
    return (_EPOCH + timedelta(seconds=30 * next(_CLOCK))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def unique_user(prefix: str) -> str:
    return "ct-%s-%s" % (prefix, uuid.uuid4().hex[:8])


def event_payload(user: str, problem: str, result: str, attempt: int = 1,
                  **extra) -> dict:
    payload = {
        "userId": user,
        "problemId": problem,
        "eventId": str(uuid.uuid4()),
        "timestamp": _ts(),
        "attemptNumber": attempt,
        "result": result,
        "timeSpentSeconds": 600,
    }
    payload.update(extra)
    return payload


def _seed_mini_catalog(container: Container) -> None:
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


def make_client(*, api_key: str = "", export: bool = False,
                salt: str | None = None, mini: bool = True):
    """An app on a shared in-memory container (the integration-harness shape)."""
    settings = Settings()
    settings.database_url = "sqlite://"
    settings.security.api_key = api_key
    settings.research.export_enabled = export
    if salt is not None:
        settings.research.pseudonym_salt = salt
    container = Container(settings, seed_catalog=False)
    if mini:
        _seed_mini_catalog(container)
    app = create_app(container)
    return TestClient(app), app, container


@pytest.fixture()
def client() -> TestClient:
    return make_client()[0]


def send_event(client: TestClient, user: str, problem: str, result: str,
               attempt: int = 1) -> dict:
    r = client.post("/api/events", json=event_payload(user, problem, result, attempt))
    assert r.status_code == 201, r.text
    return r.json()


def assert_error(response, status: int, error: str) -> dict:
    """The ApiError record shape (common/error/ApiError.java)."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["status"] == status
    assert body["error"] == error
    assert body["timestamp"] is not None
    assert body["message"] is not None
    assert body["path"] is not None
    assert isinstance(body["details"], list)
    return body


# --------------------------------------------------------------------------
# Factory shapes
# --------------------------------------------------------------------------

class TestFactoryShapes:
    def test_settings_and_database_url_shape(self, tmp_path):
        settings = Settings()
        # A missing catalog file means no seeding (the Java CatalogSeeder guard):
        # the app starts with an empty catalog but is fully usable.
        settings.catalog_path = str(tmp_path / "no-catalog.json")
        client = TestClient(create_app(settings, database_url="sqlite://"))
        assert client.get("/api/health").json() == {"status": "UP"}
        assert client.post("/api/concepts", json={
            "code": "ARRAYS", "name": "Arrays"}).status_code == 201
        assert client.post("/api/problems", json={
            "problemKey": "P1", "title": "One", "staticDifficulty": "EASY",
            "concepts": [{"conceptCode": "ARRAYS"}]}).status_code == 201
        assert [p["problemId"] for p in client.get("/api/problems").json()] == ["P1"]

    def test_zero_arg_factory_uses_env_database_url(self, tmp_path, monkeypatch):
        db_file = tmp_path / "factory.db"
        monkeypatch.setenv("KT_DATABASE_URL", "sqlite:///" + str(db_file))
        client = TestClient(create_app())
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/learners", json={
            "externalRef": unique_user("factory")}).status_code == 201

    def test_container_shape_skips_seeding(self):
        # The container owns schema + seed; create_app(container) must not seed the
        # real catalog over the harness's mini catalog.
        client, _, _ = make_client()
        catalog_size = len(PROBLEMS)
        assert len(client.get("/api/problems").json()) == catalog_size


# --------------------------------------------------------------------------
# Health, dashboard, correlation ids
# --------------------------------------------------------------------------

class TestHealthAndDashboard:
    def test_health_endpoints_are_up(self, client):
        assert client.get("/api/health").json() == {"status": "UP"}
        assert client.get("/actuator/health").json() == {"status": "UP"}

    def test_dashboard_and_root_redirect(self, client):
        for path in ("/dashboard", "/dashboard.html"):
            r = client.get(path)
            assert r.status_code == 200
            assert "text/html" in r.headers["content-type"]
        root = client.get("/", follow_redirects=False)
        assert root.status_code in (302, 307)
        assert root.headers["location"] == "/dashboard"

    def test_request_id_echoed_or_generated(self, client):
        r = client.get("/api/health", headers={"X-Request-Id": "req-42.x"})
        assert r.headers["X-Request-Id"] == "req-42.x"
        generated = client.get("/api/health")
        uuid.UUID(generated.headers["X-Request-Id"])  # a valid UUID when absent
        unsafe = client.get("/api/health", headers={"X-Request-Id": "bad id"})
        uuid.UUID(unsafe.headers["X-Request-Id"])  # replaced, not echoed


# --------------------------------------------------------------------------
# Learners
# --------------------------------------------------------------------------

class TestLearners:
    def test_create_learner_is_idempotent(self, client):
        user = unique_user("create")
        created = client.post("/api/learners", json={"externalRef": user})
        assert created.status_code == 201
        assert created.json()["externalRef"] == user
        assert created.json()["created"] is True
        assert created.json()["createdAt"] is not None
        again = client.post("/api/learners", json={"externalRef": user})
        assert again.status_code == 200
        assert again.json()["created"] is False

    def test_create_learner_validates_body(self, client):
        assert_error(client.post("/api/learners", json={"externalRef": ""}),
                     400, "VALIDATION_ERROR")
        assert_error(client.post("/api/learners", json={}), 400, "VALIDATION_ERROR")

    def test_unknown_learner_is_404_on_all_reads(self, client):
        user = unique_user("ghost")
        for path in ("state", "next-action", "history", "recommendations"):
            assert_error(client.get("/api/learners/%s/%s" % (user, path)),
                         404, "NOT_FOUND")

    def test_state_shape_and_cold_start_prior(self, client):
        user = unique_user("state")
        client.post("/api/learners", json={"externalRef": user})
        s = client.get("/api/learners/%s/state" % user).json()
        assert s["userId"] == user
        assert s["ability"] == 5.0
        assert s["uncertainty"] == 2.0
        assert s["calibrated"] is False
        assert s["totalEvents"] == 0
        assert s["concepts"] == {}

    def test_state_after_event_reports_concept_mastery(self, client):
        user = unique_user("mastery")
        send_event(client, user, "LC_E1", "ACCEPTED")
        s = client.get("/api/learners/%s/state" % user).json()
        assert s["totalEvents"] == 1
        arrays = s["concepts"]["ARRAYS"]
        assert 0.0 <= arrays["mastery"] <= 1.0
        assert arrays["evidenceCount"] == 1
        assert "uncertainty" in arrays

    def test_next_action_returns_recommendation_with_explanation(self, client):
        user = unique_user("next")
        send_event(client, user, "LC_M3", "WRONG_ANSWER")
        r = client.get("/api/learners/%s/next-action" % user)
        assert r.status_code == 200, r.text
        body = r.json()
        for field in ("userId", "state", "knowledge", "gap", "recommendation",
                      "feedback", "models", "dktKnowledge"):
            assert field in body, field
        rec = body["recommendation"]
        for field in ("recommendationId", "type", "problemId", "difficulty",
                      "reason", "evidence", "confidence", "decidedBy",
                      "fallbackUsed"):
            assert field in rec, field
        assert rec["reason"]
        assert isinstance(rec["evidence"], list) and rec["evidence"]
        assert 0.0 <= rec["confidence"] <= 1.0
        assert rec["decidedBy"] in ("JEV", "FALLBACK")
        # The recommended problem must exist.
        assert client.get("/api/problems/" + rec["problemId"]).status_code == 200

    def test_recommendations_and_history_lists(self, client):
        user = unique_user("lists")
        send_event(client, user, "LC_E1", "ACCEPTED")
        client.get("/api/learners/%s/next-action" % user)
        recs = client.get("/api/learners/%s/recommendations" % user).json()
        assert len(recs) == 1
        assert recs[0]["problemId"]
        assert recs[0]["decidedBy"] in ("JEV", "FALLBACK")
        history = client.get("/api/learners/%s/history" % user).json()
        assert len(history) == 1
        assert history[0]["problemId"] == "LC_E1"
        assert history[0]["result"] == "ACCEPTED"
        assert history[0]["attemptNumber"] == 1

    def test_limit_must_be_positive(self, client):
        user = unique_user("limit")
        send_event(client, user, "LC_E1", "ACCEPTED")
        for path in ("history?limit=0", "recommendations?limit=-1"):
            body = assert_error(client.get("/api/learners/%s/%s" % (user, path)),
                                400, "VALIDATION_ERROR")
            assert "limit" in str(body["details"])


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

class TestEvents:
    def test_post_event_returns_201_with_stored_event(self, client):
        user = unique_user("event")
        payload = event_payload(user, "LC_E1", "ACCEPTED")
        r = client.post("/api/events", json=payload)
        assert r.status_code == 201
        body = r.json()
        assert body["userId"] == user
        assert body["problemId"] == "LC_E1"
        assert body["eventId"] == payload["eventId"]
        assert body["id"]
        assert body["result"] == "ACCEPTED"
        assert body["attemptNumber"] == 1
        assert body["timeSpentSeconds"] == 600
        assert body["hintUsed"] is False
        assert body["solutionViewed"] is False
        assert body["timestamp"] is not None
        assert body["duplicate"] is False

    def test_duplicate_event_id_replay_is_idempotent(self, client):
        user = unique_user("dup")
        payload = event_payload(user, "LC_M3", "WRONG_ANSWER")
        assert client.post("/api/events", json=payload).status_code == 201
        before = client.get("/api/learners/%s/state" % user).json()
        for _ in range(2):
            replay = client.post("/api/events", json=payload)
            assert replay.status_code == 200
            assert replay.json()["duplicate"] is True
        assert client.get("/api/learners/%s/state" % user).json() == before
        assert len(client.get("/api/learners/%s/history" % user).json()) == 1

    def test_event_id_reused_for_another_learner_or_problem_is_409(self, client):
        user = unique_user("reuse")
        payload = event_payload(user, "LC_M3", "WRONG_ANSWER")
        assert client.post("/api/events", json=payload).status_code == 201
        stranger = unique_user("stranger")
        clash = client.post("/api/events", json=dict(payload, userId=stranger))
        assert_error(clash, 409, "CONFLICT")
        # The conflict is checked BEFORE any learner is created.
        assert client.get("/api/learners/%s/state" % stranger).status_code == 404
        assert_error(client.post("/api/events",
                                 json=dict(payload, problemId="LC_E1")),
                     409, "CONFLICT")

    def test_unknown_problem_is_422(self, client):
        assert_error(client.post("/api/events", json=event_payload(
            unique_user("u"), "LC_NOPE", "ACCEPTED")), 422, "BUSINESS_RULE_VIOLATION")

    def test_invalid_body_is_400_with_field_details(self, client):
        r = client.post("/api/events",
                        json={"userId": "", "problemId": "LC_E1", "attemptNumber": 0})
        body = assert_error(r, 400, "VALIDATION_ERROR")
        details = str(body["details"])
        for field in ("userId", "attemptNumber", "timestamp", "result"):
            assert field in details

    def test_malformed_json_and_bad_enum_are_400_malformed(self, client):
        r = client.post("/api/events", content="{not json",
                        headers={"Content-Type": "application/json"})
        assert_error(r, 400, "MALFORMED_REQUEST")
        bad_enum = event_payload(unique_user("enum"), "LC_E1", "MAYBE")
        assert_error(client.post("/api/events", json=bad_enum),
                     400, "MALFORMED_REQUEST")

    def test_non_json_content_type_is_415(self, client):
        r = client.post("/api/events", content="hello",
                        headers={"Content-Type": "text/plain"})
        assert_error(r, 415, "UNSUPPORTED_MEDIA_TYPE")

    def test_wrong_method_is_405_and_unknown_path_404(self, client):
        assert_error(client.get("/api/events"), 405, "METHOD_NOT_ALLOWED")
        assert_error(client.get("/api/does-not-exist"), 404, "NOT_FOUND")


# --------------------------------------------------------------------------
# Problems and concepts
# --------------------------------------------------------------------------

class TestProblemsAndConcepts:
    def test_problem_crud_round_trip(self, client):
        key = "LCX" + uuid.uuid4().hex[:8].upper()
        created = client.post("/api/problems", json={
            "problemKey": key, "title": "Round trip", "staticDifficulty": "MEDIUM",
            "url": "https://leetcode.com/problems/round-trip/",
            "acceptanceRate": 0.58, "paidOnly": False,
            "topicTags": ["Hash Table"],
            "concepts": [{"conceptCode": "HASHING", "weight": 0.9},
                         {"conceptCode": "ARRAYS", "weight": 0.6}]})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["problemId"] == key
        assert body["staticDifficulty"] == "MEDIUM"
        assert body["priorDifficulty"] == body["estimatedDifficulty"]
        assert body["topicTags"] == ["Hash Table"]
        assert body["concepts"] == ["ARRAYS", "HASHING"]
        # conceptTags are ordered primary-first (weight desc, then code).
        assert [t["code"] for t in body["conceptTags"]] == ["HASHING", "ARRAYS"]
        fetched = client.get("/api/problems/" + key)
        assert fetched.status_code == 200
        assert fetched.json() == body
        assert key in [p["problemId"] for p in client.get("/api/problems").json()]

    def test_problem_duplicate_409_unknown_concept_404(self, client):
        assert_error(client.post("/api/problems", json={
            "problemKey": "LC_E1", "title": "dup", "staticDifficulty": "EASY"}),
            409, "CONFLICT")
        assert_error(client.post("/api/problems", json={
            "problemKey": "LCU" + uuid.uuid4().hex[:8].upper(), "title": "x",
            "staticDifficulty": "EASY",
            "concepts": [{"conceptCode": "NOT_A_CONCEPT"}]}), 404, "NOT_FOUND")
        assert_error(client.get("/api/problems/LC_NOPE"), 404, "NOT_FOUND")

    def test_concept_crud_round_trip(self, client):
        code = "CT_" + uuid.uuid4().hex[:8].upper()
        created = client.post("/api/concepts", json={
            "code": code, "name": "Contract", "description": "round trip"})
        assert created.status_code == 201, created.text
        assert created.json()["code"] == code
        assert created.json()["directPrerequisites"] == []
        assert created.json()["allPrerequisites"] == []
        assert client.get("/api/concepts/" + code).json()["name"] == "Contract"
        assert code in [c["code"] for c in client.get("/api/concepts").json()]

    def test_concept_duplicate_409_and_code_pattern_400(self, client):
        assert_error(client.post("/api/concepts",
                                 json={"code": "ARRAYS", "name": "dup"}),
                     409, "CONFLICT")
        assert_error(client.post("/api/concepts",
                                 json={"code": "lower case", "name": "x"}),
                     400, "VALIDATION_ERROR")
        assert_error(client.get("/api/concepts/NOPE"), 404, "NOT_FOUND")

    def test_prerequisite_edge_lifecycle(self, client):
        a = "CA_" + uuid.uuid4().hex[:8].upper()
        b = "CB_" + uuid.uuid4().hex[:8].upper()
        assert client.post("/api/concepts", json={"code": a, "name": "A"}).status_code == 201
        assert client.post("/api/concepts", json={"code": b, "name": "B"}).status_code == 201
        edge = client.post("/api/concepts/%s/prerequisites" % b,
                           json={"prerequisiteCode": a})
        assert edge.status_code == 201
        assert edge.json()["directPrerequisites"] == [a]
        # Java check order: self-loop 422, unknown 404, cycle 422, duplicate 409.
        assert_error(client.post("/api/concepts/%s/prerequisites" % a,
                                 json={"prerequisiteCode": a}),
                     422, "BUSINESS_RULE_VIOLATION")
        assert_error(client.post("/api/concepts/%s/prerequisites" % a,
                                 json={"prerequisiteCode": b}),
                     422, "BUSINESS_RULE_VIOLATION")
        assert_error(client.post("/api/concepts/%s/prerequisites" % b,
                                 json={"prerequisiteCode": a}),
                     409, "CONFLICT")
        assert_error(client.post("/api/concepts/%s/prerequisites" % a,
                                 json={"prerequisiteCode": "NOPE"}),
                     404, "NOT_FOUND")


# --------------------------------------------------------------------------
# Model status
# --------------------------------------------------------------------------

class TestModelStatus:
    def test_dkt_status_disabled_by_default(self, client):
        dkt = client.get("/api/models/dkt").json()
        assert dkt["enabled"] is False
        assert dkt["available"] is False
        assert dkt["status"] == "DISABLED"
        assert dkt["blendWeight"] == 0.5
        assert dkt["minHistory"] == 3

    def test_jev_status_reports_bandit_arms(self, client):
        jev = client.get("/api/models/jev").json()
        assert jev["enabled"] is True
        assert jev["provider"] == "deterministic"
        assert jev["providers"] == ["deterministic", "bandit", "http"]
        assert jev["bandit"]["features"][0] == "bias"
        arms = jev["bandit"]["arms"]
        assert len(arms) == 8
        assert arms[0]["arm"] == "CALIBRATION_PROBLEM"
        assert arms[0]["updates"] == 0
        assert arms[0]["meanReward"] is None
        assert len(arms[0]["theta"]) == 8


# --------------------------------------------------------------------------
# Research exports
# --------------------------------------------------------------------------

class TestResearchExport:
    def test_exports_are_404_when_disabled(self, client):
        for path in ("/api/research/events", "/api/research/decisions",
                     "/api/research/catalog"):
            assert_error(client.get(path), 404, "NOT_FOUND")

    def test_events_and_decisions_are_ndjson(self):
        client, _, _ = make_client(export=True, salt="contract-salt")
        user = unique_user("export")
        send_event(client, user, "LC_E1", "ACCEPTED")
        recommended = client.get(
            "/api/learners/%s/next-action" % user).json()["recommendation"]["problemId"]
        send_event(client, user, recommended, "ACCEPTED")

        events = client.get("/api/research/events")
        assert events.status_code == 200
        assert events.headers["content-type"].startswith("application/x-ndjson")
        assert events.headers["content-disposition"] == \
            'attachment; filename="events.jsonl"'
        lines = [json.loads(l) for l in events.text.splitlines() if l.strip()]
        assert lines and all(l["schema"] == "kt-events-v1" for l in lines)
        assert all(l["learnerId"].startswith("L-") for l in lines)
        assert user not in events.text  # pseudonymised

        decisions = client.get("/api/research/decisions")
        assert decisions.headers["content-type"].startswith("application/x-ndjson")
        assert decisions.headers["content-disposition"] == \
            'attachment; filename="decisions.jsonl"'
        rows = [json.loads(l) for l in decisions.text.splitlines() if l.strip()]
        assert any(r["schema"] == "kt-decisions-v1" for r in rows)

    def test_decisions_limit_validated_before_export_guard(self):
        client, _, _ = make_client(export=True, salt="contract-salt")
        assert_error(client.get("/api/research/decisions?limit=0"),
                     400, "VALIDATION_ERROR")

    def test_insecure_salt_is_422(self):
        client, _, _ = make_client(export=True)  # default salt "change-me"
        assert_error(client.get("/api/research/events"), 422,
                     "BUSINESS_RULE_VIOLATION")


# --------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------

class _FakeFeedbackService:
    """Duck-typed stand-in for knowledge_tracer.feedback.FeedbackService."""

    def __init__(self):
        self.submissions = []
        self.feedback_id = uuid.uuid4()

    def submit(self, submission):
        self.submissions.append(submission)
        return self.feedback_id


class TestFeedback:
    def test_feedback_returns_201_via_dependency_override(self):
        client, app, _ = make_client()
        fake = _FakeFeedbackService()
        app.dependency_overrides[get_feedback_service] = lambda: fake
        try:
            r = client.post("/api/feedback", json={
                "userId": "anyone",
                "problemId": "LC_M3",
                "recommendationId": str(uuid.uuid4()),
                "answers": [{"question": "What prevented you?",
                             "answer": "pointer updates"}],
                "freeText": "hard"})
            assert r.status_code == 201, r.text
            assert r.json() == {"feedbackId": str(fake.feedback_id),
                                "status": "RECORDED"}
            submission = fake.submissions[0]
            assert submission.user_id == "anyone"
            assert submission.problem_id == "LC_M3"
            assert submission.free_text == "hard"
            assert [(a.question, a.answer) for a in submission.answers] == \
                [("What prevented you?", "pointer updates")]
        finally:
            app.dependency_overrides.clear()

    def test_feedback_recorded_end_to_end(self, client):
        user = unique_user("fb")
        send_event(client, user, "LC_M3", "WRONG_ANSWER")
        rec_id = client.get("/api/learners/%s/next-action" % user).json()[
            "recommendation"]["recommendationId"]
        r = client.post("/api/feedback", json={
            "userId": user, "problemId": "LC_M3", "recommendationId": rec_id,
            "answers": [{"question": "q", "answer": "a"}]})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "RECORDED"
        uuid.UUID(r.json()["feedbackId"])

    def test_feedback_unknown_learner_is_404(self, client):
        assert_error(client.post("/api/feedback", json={
            "userId": unique_user("ghost"),
            "answers": [{"question": "q", "answer": "a"}]}), 404, "NOT_FOUND")

    def test_feedback_validation_is_400(self, client):
        assert_error(client.post("/api/feedback",
                                 json={"userId": unique_user("v"), "answers": []}),
                     400, "VALIDATION_ERROR")
        assert_error(client.post("/api/feedback", json={
            "userId": unique_user("v"),
            "answers": [{"question": "", "answer": "a"}]}), 400, "VALIDATION_ERROR")


# --------------------------------------------------------------------------
# API key filter
# --------------------------------------------------------------------------

class TestApiKey:
    def test_api_requires_key_but_health_stays_open(self):
        client, _, _ = make_client(api_key="contract-secret")
        denied = client.post("/api/learners",
                             json={"externalRef": unique_user("key")})
        assert_error(denied, 401, "UNAUTHORIZED")
        assert denied.headers.get("X-Request-Id")  # correlation wraps even 401s

        wrong = client.post("/api/learners",
                            json={"externalRef": unique_user("key")},
                            headers={"X-API-Key": "nope"})
        assert wrong.status_code == 401

        allowed = client.post("/api/learners",
                              json={"externalRef": unique_user("key")},
                              headers={"X-API-Key": "contract-secret"})
        assert allowed.status_code == 201

        assert client.get("/api/health").status_code == 200
        assert client.get("/actuator/health").status_code == 200

    def test_api_is_open_when_key_blank(self):
        client, _, _ = make_client()
        r = client.post("/api/learners",
                        json={"externalRef": unique_user("open")})
        assert r.status_code == 201

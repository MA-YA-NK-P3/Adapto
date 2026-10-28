"""HTTP API contract tests — the Python counterpart of ApiContractIT / ApiKeyIT /
LearningLoopIT / ResearchExportIT (docs/api-flow.md).

Runs create_app against in-memory SQLite (StaticPool shares the single connection
across the TestClient worker threads) with catalog seeding disabled; concepts and
problems are registered through the API itself, which doubles as the CRUD round-trip.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from knowledge_tracer.api import create_app
from knowledge_tracer.api.routes_feedback import get_feedback_service
from knowledge_tracer.config import Settings
from knowledge_tracer.errors import NotFoundError

TS = "2026-09-30T15:04:05Z"


def make_client(configure=None) -> TestClient:
    settings = Settings()
    settings.catalog_path = "__definitely_missing__.json"  # no seed in tests
    if configure is not None:
        configure(settings)
    return TestClient(create_app(settings, database_url="sqlite://"))


@pytest.fixture()
def client() -> TestClient:
    return make_client()


def seed_catalog(client: TestClient) -> None:
    """Two concepts (one edge) and two problems, registered via the API."""
    assert client.post("/api/concepts", json={"code": "ARRAYS", "name": "Arrays"}).status_code == 201
    assert client.post("/api/concepts", json={"code": "TWO_POINTERS", "name": "Two Pointers"}).status_code == 201
    assert client.post("/api/concepts/TWO_POINTERS/prerequisites",
                       json={"prerequisiteCode": "ARRAYS"}).status_code == 201
    assert client.post("/api/problems", json={
        "problemKey": "LC1", "title": "Two Sum", "staticDifficulty": "EASY",
        "concepts": [{"conceptCode": "ARRAYS", "weight": 1.0}],
        "acceptanceRate": 0.5}).status_code == 201
    assert client.post("/api/problems", json={
        "problemKey": "LC2", "title": "Three Sum", "staticDifficulty": "MEDIUM",
        "concepts": [{"conceptCode": "TWO_POINTERS", "weight": 0.9},
                     {"conceptCode": "ARRAYS", "weight": 0.5}]}).status_code == 201


def event_body(user="U1", problem="LC1", event_id=None, result="WRONG_ANSWER", **extra):
    body = {"userId": user, "problemId": problem, "timestamp": TS,
            "attemptNumber": 1, "result": result, "timeSpentSeconds": 120}
    if event_id is not None:
        body["eventId"] = str(event_id)
    body.update(extra)
    return body


def assert_api_error(body, status, error):
    assert set(body) == {"timestamp", "status", "error", "message", "path", "details"}
    assert body["status"] == status
    assert body["error"] == error
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["details"], list)


# --- learners ------------------------------------------------------------------

def test_create_learner_then_existing(client):
    r = client.post("/api/learners", json={"externalRef": "U1"})
    assert r.status_code == 201
    body = r.json()
    assert body["externalRef"] == "U1"
    assert body["created"] is True
    assert body["createdAt"].endswith("Z")

    r = client.post("/api/learners", json={"externalRef": "U1"})
    assert r.status_code == 200
    assert r.json()["created"] is False


def test_create_learner_blank_ref_is_400(client):
    r = client.post("/api/learners", json={"externalRef": ""})
    assert r.status_code == 400
    body = r.json()
    assert_api_error(body, 400, "VALIDATION_ERROR")
    assert body["message"] == "Invalid request body"
    assert body["path"] == "/api/learners"
    assert any(d.startswith("externalRef: ") for d in body["details"])


def test_unknown_learner_state_is_404(client):
    r = client.get("/api/learners/ghost/state")
    assert r.status_code == 404
    body = r.json()
    assert_api_error(body, 404, "NOT_FOUND")
    assert body["message"] == "Learner not found: ghost"


def test_learner_state_cold_start_prior(client):
    client.post("/api/learners", json={"externalRef": "U1"})
    r = client.get("/api/learners/U1/state")
    assert r.status_code == 200
    body = r.json()
    assert body["userId"] == "U1"
    assert body["ability"] == 5.0
    assert body["uncertainty"] == 2.0
    assert body["calibrated"] is False
    assert body["totalEvents"] == 0
    assert body["concepts"] == {}


# --- events --------------------------------------------------------------------

def test_post_event_accepted(client):
    seed_catalog(client)
    event_id = uuid.uuid4()
    r = client.post("/api/events", json=event_body(event_id=event_id))
    assert r.status_code == 201
    body = r.json()
    assert body["eventId"] == str(event_id)
    assert body["userId"] == "U1"
    assert body["problemId"] == "LC1"
    assert body["timestamp"] == TS
    assert body["result"] == "WRONG_ANSWER"
    assert body["duplicate"] is False
    assert uuid.UUID(body["id"])  # internal id is a UUID


def test_duplicate_event_replay_is_idempotent(client):
    seed_catalog(client)
    body = event_body(event_id=uuid.uuid4())
    assert client.post("/api/events", json=body).status_code == 201

    for _ in range(3):
        r = client.post("/api/events", json=body)
        assert r.status_code == 200
        assert r.json()["duplicate"] is True

    state = client.get("/api/learners/U1/state").json()
    assert state["totalEvents"] == 1  # replays never re-update state
    history = client.get("/api/learners/U1/history").json()
    assert len(history) == 1


def test_event_id_reused_for_another_learner_or_problem_is_409(client):
    seed_catalog(client)
    event_id = uuid.uuid4()
    assert client.post("/api/events", json=event_body(event_id=event_id)).status_code == 201

    r = client.post("/api/events", json=event_body(user="U2", event_id=event_id))
    assert r.status_code == 409
    body = r.json()
    assert_api_error(body, 409, "CONFLICT")
    assert "already recorded for a different learner or problem" in body["message"]

    r = client.post("/api/events", json=event_body(problem="LC2", event_id=event_id))
    assert r.status_code == 409

    # The conflict is checked before any learner is auto-provisioned.
    assert client.get("/api/learners/U2/state").status_code == 404


def test_event_unknown_problem_is_422(client):
    seed_catalog(client)
    r = client.post("/api/events", json=event_body(problem="LC_NOPE"))
    assert r.status_code == 422
    body = r.json()
    assert_api_error(body, 422, "BUSINESS_RULE_VIOLATION")
    assert body["message"] == "Unknown problemId 'LC_NOPE': register the problem first"


def test_event_invalid_body_is_400_validation_error(client):
    seed_catalog(client)
    r = client.post("/api/events", json=event_body(attemptNumber=0))
    assert r.status_code == 400
    body = r.json()
    assert_api_error(body, 400, "VALIDATION_ERROR")
    assert body["message"] == "Invalid request body"
    assert any(d.startswith("attemptNumber: ") for d in body["details"])


def test_event_malformed_json_is_400_malformed_request(client):
    r = client.post("/api/events", content="{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    body = r.json()
    assert_api_error(body, 400, "MALFORMED_REQUEST")
    assert body["message"] == "Request body is malformed or contains invalid values"


def test_event_unknown_result_enum_is_400_malformed_request(client):
    seed_catalog(client)
    r = client.post("/api/events", json=event_body(result="NOPE"))
    assert r.status_code == 400
    assert_api_error(r.json(), 400, "MALFORMED_REQUEST")


def test_event_non_json_content_type_is_415(client):
    r = client.post("/api/events", content="hello",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    body = r.json()
    assert_api_error(body, 415, "UNSUPPORTED_MEDIA_TYPE")
    assert "text/plain" in body["message"]


def test_get_on_events_is_405(client):
    r = client.get("/api/events")
    assert r.status_code == 405
    assert_api_error(r.json(), 405, "METHOD_NOT_ALLOWED")


def test_unknown_route_is_404_no_endpoint(client):
    r = client.get("/api/definitely-not-here")
    assert r.status_code == 404
    body = r.json()
    assert_api_error(body, 404, "NOT_FOUND")
    assert body["message"] == "No endpoint at this path"


# --- next-action / history -------------------------------------------------------

def test_next_action_returns_recommendation_with_explanation(client):
    seed_catalog(client)
    client.post("/api/events", json=event_body())
    r = client.get("/api/learners/U1/next-action")
    assert r.status_code == 200
    body = r.json()

    assert body["userId"] == "U1"
    assert set(body["state"]) == {"ability", "uncertainty", "calibrated", "totalEvents"}
    assert set(body["gap"]) == {"type", "concept", "prerequisite", "evidence", "confidence"}
    assert isinstance(body["gap"]["evidence"], list)

    rec = body["recommendation"]
    assert uuid.UUID(rec["recommendationId"])
    assert rec["type"] in ("CALIBRATION_PROBLEM", "EASIER_PROBLEM", "SAME_LEVEL_PROBLEM",
                           "HARDER_PROBLEM", "PREREQUISITE_REVIEW", "RETRY_PROBLEM",
                           "SPACED_REVIEW", "FEEDBACK_REQUEST")
    assert rec["reason"]  # the explanation
    assert isinstance(rec["evidence"], list) and rec["evidence"]
    assert 0.0 <= rec["confidence"] <= 1.0
    assert rec["decidedBy"] in ("JEV", "FALLBACK")
    assert isinstance(rec["fallbackUsed"], bool)

    assert set(body["feedback"]) == {"required", "questions"}
    assert body["models"]["irt"] == "ENABLED (Rasch 1PL, MAP)"
    assert body["models"]["jev"] == "ENABLED (deterministic)"
    assert body["dktKnowledge"] == {}


def test_next_action_unknown_learner_is_404(client):
    r = client.get("/api/learners/ghost/next-action")
    assert r.status_code == 404
    assert_api_error(r.json(), 404, "NOT_FOUND")


def test_recommendations_audit_trail(client):
    seed_catalog(client)
    client.post("/api/events", json=event_body())
    assert client.get("/api/learners/U1/recommendations").json() == []
    client.get("/api/learners/U1/next-action")
    recs = client.get("/api/learners/U1/recommendations").json()
    assert len(recs) == 1
    entry = recs[0]
    assert uuid.UUID(entry["recommendationId"])
    assert entry["decidedBy"] == "JEV"
    assert entry["provider"] == "deterministic"
    assert entry["fallbackUsed"] is False
    assert entry["reward"] is None  # until an outcome is attributed


def test_learner_history_newest_first(client):
    seed_catalog(client)
    client.post("/api/events", json=event_body(
        timestamp="2026-09-30T15:00:00Z", result="WRONG_ANSWER"))
    client.post("/api/events", json=event_body(
        timestamp=TS, result="ACCEPTED", attemptNumber=2, confidence=4,
        perceivedDifficulty=3, blocker="EDGE_CASES", sessionId="s-1"))
    r = client.get("/api/learners/U1/history")
    assert r.status_code == 200
    history = r.json()
    assert [h["result"] for h in history] == ["ACCEPTED", "WRONG_ANSWER"]
    first = history[0]
    assert first["problemId"] == "LC1"
    assert first["attemptNumber"] == 2
    assert first["timeSpentSeconds"] == 120
    assert first["hintUsed"] is False
    assert first["solutionViewed"] is False
    assert first["confidence"] == 4
    assert first["perceivedDifficulty"] == 3
    assert first["blocker"] == "EDGE_CASES"


def test_history_limit_validation(client):
    seed_catalog(client)
    client.post("/api/events", json=event_body())
    r = client.get("/api/learners/U1/history?limit=0")
    assert r.status_code == 400
    body = r.json()
    assert_api_error(body, 400, "VALIDATION_ERROR")
    assert body["message"] == "Invalid request parameters"
    assert any(d.startswith("limit: ") for d in body["details"])

    r = client.get("/api/learners/U1/history?limit=abc")
    assert r.status_code == 400
    body = r.json()
    assert_api_error(body, 400, "VALIDATION_ERROR")
    assert body["message"] == "Invalid value for parameter 'limit'"


# --- problems and concepts CRUD ----------------------------------------------------

def test_problem_crud_round_trip(client):
    client.post("/api/concepts", json={"code": "ARRAYS", "name": "Arrays"})
    r = client.post("/api/problems", json={
        "problemKey": "LC9999", "title": "Example", "staticDifficulty": "MEDIUM",
        "url": "https://leetcode.com/problems/example/",
        "concepts": [{"conceptCode": "ARRAYS", "weight": 0.8}],
        "acceptanceRate": 0.58, "paidOnly": False, "topicTags": ["Array"]})
    assert r.status_code == 201
    body = r.json()
    assert body["problemId"] == "LC9999"
    assert body["staticDifficulty"] == "MEDIUM"
    assert body["estimatedDifficulty"] == body["priorDifficulty"]
    assert body["difficultyUncertainty"] == 1.5
    assert body["sampleSize"] == 0
    assert body["source"] == "LEETCODE"
    assert body["concepts"] == ["ARRAYS"]
    assert body["conceptTags"] == [{"code": "ARRAYS", "weight": 0.8}]
    assert body["acceptanceRate"] == 0.58
    assert body["paidOnly"] is False
    assert body["topicTags"] == ["Array"]

    r = client.get("/api/problems/LC9999")
    assert r.status_code == 200
    assert r.json() == body

    keys = [p["problemId"] for p in client.get("/api/problems").json()]
    assert keys == ["LC9999"]

    r = client.post("/api/problems", json={
        "problemKey": "LC9999", "title": "Dupe", "staticDifficulty": "EASY"})
    assert r.status_code == 409
    assert_api_error(r.json(), 409, "CONFLICT")

    r = client.post("/api/problems", json={
        "problemKey": "LC_X", "title": "Bad tag", "staticDifficulty": "EASY",
        "concepts": [{"conceptCode": "NOPE"}]})
    assert r.status_code == 404
    assert r.json()["message"] == "Concept not found: NOPE"
    assert client.get("/api/problems/LC_X").status_code == 404  # rolled back

    r = client.get("/api/problems/LC_NOPE")
    assert r.status_code == 404
    assert r.json()["message"] == "Problem not found: LC_NOPE"


def test_concept_crud_round_trip(client):
    r = client.post("/api/concepts", json={
        "code": "MONOTONIC_QUEUE", "name": "Monotonic queue",
        "description": "Deque that keeps a monotone order"})
    assert r.status_code == 201
    assert r.json() == {"code": "MONOTONIC_QUEUE", "name": "Monotonic queue",
                        "description": "Deque that keeps a monotone order",
                        "directPrerequisites": [], "allPrerequisites": []}

    r = client.post("/api/concepts", json={"code": "lower_case", "name": "Nope"})
    assert r.status_code == 400
    assert_api_error(r.json(), 400, "VALIDATION_ERROR")

    r = client.post("/api/concepts", json={"code": "MONOTONIC_QUEUE", "name": "Dupe"})
    assert r.status_code == 409

    client.post("/api/concepts", json={"code": "STACK", "name": "Stack"})
    r = client.post("/api/concepts/MONOTONIC_QUEUE/prerequisites",
                    json={"prerequisiteCode": "STACK"})
    assert r.status_code == 201
    body = r.json()
    assert body["directPrerequisites"] == ["STACK"]
    assert body["allPrerequisites"] == ["STACK"]

    r = client.post("/api/concepts/MONOTONIC_QUEUE/prerequisites",
                    json={"prerequisiteCode": "STACK"})
    assert r.status_code == 409  # duplicate edge

    r = client.post("/api/concepts/STACK/prerequisites",
                    json={"prerequisiteCode": "MONOTONIC_QUEUE"})
    assert r.status_code == 422  # would close a cycle
    assert "would create a cycle" in r.json()["message"]

    r = client.post("/api/concepts/STACK/prerequisites",
                    json={"prerequisiteCode": "STACK"})
    assert r.status_code == 422
    assert r.json()["message"] == "A concept cannot be its own prerequisite"

    r = client.post("/api/concepts/STACK/prerequisites",
                    json={"prerequisiteCode": "NOPE"})
    assert r.status_code == 404

    codes = [c["code"] for c in client.get("/api/concepts").json()]
    assert codes == sorted(codes)
    assert client.get("/api/concepts/NOPE").status_code == 404


# --- model status ------------------------------------------------------------------

def test_models_dkt_status(client):
    r = client.get("/api/models/dkt")
    assert r.status_code == 200
    body = r.json()
    assert body == {"enabled": False, "available": False, "status": "DISABLED",
                    "modelPath": body["modelPath"], "blendWeight": 0.5, "minHistory": 3}


def test_models_jev_status(client):
    r = client.get("/api/models/jev")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["provider"] == "deterministic"
    assert body["providers"] == ["deterministic", "bandit", "http"]
    assert body["confidenceThreshold"] == 0.35
    bandit = body["bandit"]
    assert bandit["alpha"] == 0.5
    assert bandit["lambda"] == 1.0
    assert bandit["guarded"] is True
    assert bandit["features"] == ["bias", "predictedSuccess", "information",
                                  "relativeDifficulty", "abilityUncertainty",
                                  "calibrated", "gapMatch", "expectedValue"]
    assert len(bandit["arms"]) == 8
    for arm in bandit["arms"]:
        assert arm["updates"] == 0
        assert arm["meanReward"] is None
        assert len(arm["theta"]) == 8
    assert body["http"] == {"url": "http://localhost:8090/decide", "timeoutMs": 2000}


# --- research exports ---------------------------------------------------------------

def test_research_exports_disabled_are_404(client):
    for path in ("/api/research/events", "/api/research/catalog", "/api/research/decisions"):
        r = client.get(path)
        assert r.status_code == 404
        assert_api_error(r.json(), 404, "NOT_FOUND")


def research_client() -> TestClient:
    def configure(settings):
        settings.research.export_enabled = True
        settings.research.pseudonym_salt = "test-secret-salt"
    return make_client(configure)


def test_research_events_ndjson():
    client = research_client()
    seed_catalog(client)
    client.post("/api/events", json=event_body(sessionId="sess-1"))
    r = client.get("/api/research/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["content-disposition"] == 'attachment; filename="events.jsonl"'
    lines = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert len(lines) == 1
    row = lines[0]
    assert row["schema"] == "kt-events-v1"
    assert row["learnerId"].startswith("L-")
    assert row["seq"] == 0
    assert row["problemId"] == "LC1"
    assert row["result"] == "WRONG_ANSWER"
    assert row["correct"] == 0
    assert "confidence" not in row  # self-reports are never exported


def test_research_catalog_export():
    client = research_client()
    seed_catalog(client)
    r = client.get("/api/research/catalog")
    assert r.status_code == 200
    body = r.json()
    codes = [c["code"] for c in body["concepts"]]
    assert codes == ["ARRAYS", "TWO_POINTERS"]
    assert body["concepts"][1]["prerequisites"] == ["ARRAYS"]
    assert [p["problemId"] for p in body["problems"]] == ["LC1", "LC2"]


def test_research_decisions_ndjson_after_reward():
    client = research_client()
    seed_catalog(client)
    client.post("/api/events", json=event_body())

    r = client.get("/api/research/decisions")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert r.headers["content-disposition"] == 'attachment; filename="decisions.jsonl"'
    assert r.text == ""  # no rewarded decisions yet

    rec = client.get("/api/learners/U1/next-action").json()["recommendation"]
    assert rec["problemId"] is not None
    client.post("/api/events", json=event_body(
        problem=rec["problemId"], result="ACCEPTED", attemptNumber=1))

    r = client.get("/api/research/decisions")
    lines = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert len(lines) == 1
    row = lines[0]
    assert row["schema"] == "kt-decisions-v1"
    assert row["problemId"] == rec["problemId"]
    assert row["decidedBy"] == "JEV"
    assert row["reward"] is not None
    assert row["rewardedAt"] is not None
    assert row["learnerId"].startswith("L-")


def test_research_decisions_limit_validation():
    client = research_client()
    r = client.get("/api/research/decisions?limit=0")
    assert r.status_code == 400  # checked before the export guards, as in Java
    assert_api_error(r.json(), 400, "VALIDATION_ERROR")


def test_research_export_insecure_salt_is_422():
    def configure(settings):
        settings.research.export_enabled = True  # salt stays "change-me"
    client = make_client(configure)
    r = client.get("/api/research/events")
    assert r.status_code == 422
    assert_api_error(r.json(), 422, "BUSINESS_RULE_VIOLATION")


# --- feedback -----------------------------------------------------------------------

class FakeFeedbackService:
    """Test double for knowledge_tracer.feedback.FeedbackService (built in parallel)."""

    def __init__(self):
        self.submissions = []
        self.feedback_id = uuid.uuid4()

    def submit(self, submission):
        self.submissions.append(submission)
        if submission.user_id == "ghost":
            raise NotFoundError("Learner not found: ghost")
        return self.feedback_id


def feedback_client() -> tuple:
    client = make_client()
    fake = FakeFeedbackService()
    client.app.dependency_overrides[get_feedback_service] = lambda: fake
    return client, fake


def test_feedback_submit_is_201():
    client, fake = feedback_client()
    r = client.post("/api/feedback", json={
        "userId": "U1", "problemId": "LC1",
        "recommendationId": str(uuid.uuid4()),
        "answers": [{"question": "What prevented you from solving the problem?",
                     "answer": "pointer updates"}],
        "freeText": "I keep losing the head node."})
    assert r.status_code == 201
    assert r.json() == {"feedbackId": str(fake.feedback_id), "status": "RECORDED"}

    submission = fake.submissions[0]
    assert submission.user_id == "U1"
    assert submission.problem_id == "LC1"
    assert submission.answers[0].question == "What prevented you from solving the problem?"
    assert submission.answers[0].answer == "pointer updates"
    assert submission.free_text == "I keep losing the head node."


def test_feedback_unknown_learner_is_404():
    client, _ = feedback_client()
    r = client.post("/api/feedback", json={
        "userId": "ghost",
        "answers": [{"question": "q", "answer": "a"}]})
    assert r.status_code == 404
    assert_api_error(r.json(), 404, "NOT_FOUND")


def test_feedback_empty_answers_is_400():
    client, fake = feedback_client()
    r = client.post("/api/feedback", json={"userId": "U1", "answers": []})
    assert r.status_code == 400
    assert_api_error(r.json(), 400, "VALIDATION_ERROR")
    assert fake.submissions == []  # validation fires before the service


def test_feedback_with_real_service_when_available():
    pytest.importorskip("knowledge_tracer.feedback")
    client = make_client()
    seed_catalog(client)
    client.post("/api/learners", json={"externalRef": "U1"})
    r = client.post("/api/feedback", json={
        "userId": "U1", "problemId": "LC1",
        "answers": [{"question": "q", "answer": "a"}]})
    assert r.status_code == 201
    assert r.json()["status"] == "RECORDED"

    r = client.post("/api/feedback", json={
        "userId": "ghost", "answers": [{"question": "q", "answer": "a"}]})
    assert r.status_code == 404


# --- API key ------------------------------------------------------------------------

def keyed_client() -> TestClient:
    def configure(settings):
        settings.security.api_key = "topsecret"
    return make_client(configure)


def test_api_key_required_when_enabled():
    client = keyed_client()
    r = client.get("/api/learners/U1/state")
    assert r.status_code == 401
    body = r.json()
    assert_api_error(body, 401, "UNAUTHORIZED")
    assert body["message"] == "Missing or invalid API key"
    assert body["path"] == "/api/learners/U1/state"
    assert body["details"] == []

    assert client.get("/api/learners/U1/state",
                      headers={"X-API-Key": "wrong"}).status_code == 401

    # The right key passes the filter (the learner simply does not exist).
    r = client.get("/api/learners/U1/state", headers={"X-API-Key": "topsecret"})
    assert r.status_code == 404


def test_health_is_open_when_api_key_enabled():
    client = keyed_client()
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "UP"}


def test_non_api_paths_skip_the_filter():
    client = keyed_client()
    r = client.get("/not-an-api-path")
    assert r.status_code == 404  # routing 404, not a 401


def test_api_key_beats_media_type():
    client = keyed_client()
    r = client.post("/api/events", content="x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 401  # the filter runs before the dispatcher's 415


def test_no_api_key_required_by_default(client):
    # Default settings have a blank key: the filter is disabled (local dev).
    assert client.get("/api/learners/U1/state").status_code == 404


def test_health_open_by_default(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "UP"}


# --- app factory -------------------------------------------------------------------

def test_dashboard_is_served(client):
    r = client.get("/dashboard.html")
    assert r.status_code == 200
    assert "html" in r.headers["content-type"]


def test_database_url_from_env(monkeypatch):
    # create_app() with no arguments honors KT_DATABASE_URL (the run_server.py contract).
    monkeypatch.setenv("KT_DATABASE_URL", "sqlite://")
    monkeypatch.setenv("KT_CATALOG_PATH", "__definitely_missing__.json")
    app = create_app()
    assert app.state.engine.url.database in (None, ":memory:")


def test_default_database_url_is_kt_python_db():
    from knowledge_tracer.api.app import _default_database_url
    assert _default_database_url().endswith("KT_python/kt_python.db")

"""End-to-end learning-loop proof over HTTP — the Python port of
backend/src/test/java/com/knowledgetracer/pipeline/LearningLoopIT.java.

Unlike tests/test_api_contract.py (which seeds the conftest mini catalog through the
ORM via a Container), this suite seeds its catalog THROUGH the API itself —
POST /api/concepts, POST /api/concepts/{code}/prerequisites, POST /api/problems —
against create_app(settings, database_url="sqlite://") on shared in-memory SQLite,
then drives the full loop entirely over HTTP:

    create learner -> post events -> state updates -> next-action recommends with an
    explanation -> outcome event on the recommended problem -> the recommendation
    audit row carries a recorded reward -> history reflects every event.

Plus the two ingestion edge cases LearningLoopIT covers: a duplicate eventId replay
is idempotent (200, state untouched), and an eventId reused for another learner or
problem is a 409, checked before any learner is created.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from knowledge_tracer.api import create_app
from knowledge_tracer.config import Settings

_CLOCK = itertools.count()
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ts() -> str:
    """Strictly increasing timestamps (history is ordered by occurred_at desc)."""
    return (_EPOCH + timedelta(minutes=5 * next(_CLOCK))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def unique_user(prefix: str) -> str:
    return "e2e-%s-%s" % (prefix, uuid.uuid4().hex[:8])


# --- The small catalog, seeded through the API ---------------------------------
#
#   ARRAYS <- LINKED_LIST <- TREES       STRINGS (standalone)
#
# The easy LINKED_LIST problem is never attempted by the looping learner: it is the
# remediation target the pipeline should recommend once LINKED_LIST goes weak.

CONCEPTS = ["ARRAYS", "LINKED_LIST", "TREES", "STRINGS"]
PREREQUISITES = [("LINKED_LIST", "ARRAYS"), ("TREES", "LINKED_LIST")]
PROBLEMS = [
    # problemKey, staticDifficulty, acceptanceRate, [(conceptCode, weight)]
    ("E2E_A1", "EASY", 0.55, [("ARRAYS", 1.0)]),
    ("E2E_A2", "EASY", 0.50, [("ARRAYS", 0.9)]),
    ("E2E_LL_E", "EASY", 0.52, [("LINKED_LIST", 1.0)]),
    ("E2E_LL_M1", "MEDIUM", 0.40, [("LINKED_LIST", 1.0)]),
    ("E2E_LL_M2", "MEDIUM", 0.38, [("LINKED_LIST", 1.0)]),
    ("E2E_T1", "MEDIUM", 0.35, [("TREES", 1.0)]),
]


@pytest.fixture()
def client(tmp_path) -> TestClient:
    """The app on shared in-memory SQLite with an EMPTY catalog (a missing catalog
    file — the Java CatalogSeeder guard); the suite seeds it via the API."""
    settings = Settings()
    settings.catalog_path = str(tmp_path / "no-catalog.json")
    return TestClient(create_app(settings, database_url="sqlite://"))


@pytest.fixture()
def seeded(client) -> TestClient:
    seed_catalog(client)
    return client


def seed_catalog(client: TestClient) -> None:
    for code in CONCEPTS:
        r = client.post("/api/concepts",
                        json={"code": code, "name": code.replace("_", " ").title()})
        assert r.status_code == 201, r.text
    for code, prereq in PREREQUISITES:
        r = client.post("/api/concepts/%s/prerequisites" % code,
                        json={"prerequisiteCode": prereq})
        assert r.status_code == 201, r.text
    for key, difficulty, acceptance, tags in PROBLEMS:
        r = client.post("/api/problems", json={
            "problemKey": key, "title": key, "staticDifficulty": difficulty,
            "acceptanceRate": acceptance,
            "concepts": [{"conceptCode": c, "weight": w} for c, w in tags]})
        assert r.status_code == 201, r.text
    # The whole catalog is listed, and the prerequisite edges are visible.
    assert len(client.get("/api/problems").json()) == len(PROBLEMS)
    tree = client.get("/api/concepts/TREES").json()
    assert tree["directPrerequisites"] == ["LINKED_LIST"]
    assert sorted(tree["allPrerequisites"]) == ["ARRAYS", "LINKED_LIST"]


def post_event(client: TestClient, user: str, problem: str, result: str,
               attempt: int = 1, event_id: str | None = None, **extra):
    payload = {
        "userId": user,
        "problemId": problem,
        "eventId": event_id or str(uuid.uuid4()),
        "timestamp": _ts(),
        "attemptNumber": attempt,
        "result": result,
        "timeSpentSeconds": 600,
    }
    payload.update(extra)
    return client.post("/api/events", json=payload), payload


def send_event(client: TestClient, user: str, problem: str, result: str,
               attempt: int = 1, **extra) -> dict:
    r, _ = post_event(client, user, problem, result, attempt, **extra)
    assert r.status_code == 201, r.text
    assert r.json()["duplicate"] is False
    return r.json()


def assert_error(response, status: int, error: str) -> dict:
    """The ApiError record shape (common/error/ApiError.java)."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["status"] == status
    assert body["error"] == error
    assert body["message"] is not None
    return body


# --------------------------------------------------------------------------
# Cold start (LearningLoopIT.newLearnerWithZeroHistoryGetsCalibrationItem)
# --------------------------------------------------------------------------

class TestColdStart:
    def test_new_learner_gets_calibration_item(self, seeded):
        user = unique_user("new")
        assert seeded.get("/api/learners/%s/state" % user).status_code == 404

        created = seeded.post("/api/learners", json={"externalRef": user})
        assert created.status_code == 201
        assert created.json()["created"] is True
        again = seeded.post("/api/learners", json={"externalRef": user})
        assert again.status_code == 200
        assert again.json()["created"] is False

        s = seeded.get("/api/learners/%s/state" % user).json()
        assert s["ability"] == 5.0
        assert s["uncertainty"] == 2.0
        assert s["totalEvents"] == 0
        assert s["calibrated"] is False
        assert s["concepts"] == {}

        nxt = seeded.get("/api/learners/%s/next-action" % user).json()
        assert nxt["gap"]["type"] == "UNKNOWN"
        assert nxt["recommendation"]["type"] == "CALIBRATION_PROBLEM"
        problem_id = nxt["recommendation"]["problemId"]
        assert seeded.get("/api/problems/" + problem_id).status_code == 200


# --------------------------------------------------------------------------
# The full loop (LearningLoopIT.repeatedFailure... + nextInteractionBecomesNewEvidence)
# --------------------------------------------------------------------------

class TestLearningLoop:
    def test_full_loop_events_state_recommendation_reward_history(self, seeded):
        user = unique_user("loop")
        assert seeded.post("/api/learners",
                           json={"externalRef": user}).status_code == 201

        # --- Evidence: one ARRAYS success, then repeated LINKED_LIST failure ------
        sent_ids = [send_event(seeded, user, "E2E_A1", "ACCEPTED")["eventId"],
                    send_event(seeded, user, "E2E_LL_M1", "WRONG_ANSWER")["eventId"],
                    send_event(seeded, user, "E2E_LL_M2", "WRONG_ANSWER")["eventId"]]

        # --- State reflects the evidence ------------------------------------------
        s = seeded.get("/api/learners/%s/state" % user).json()
        assert s["totalEvents"] == 3
        assert s["ability"] != 5.0
        assert s["uncertainty"] < 2.0
        linked = s["concepts"]["LINKED_LIST"]
        assert linked["mastery"] < 0.45  # Beta(1, 3) = 0.25 — weak
        assert linked["evidenceCount"] == 2
        assert s["concepts"]["ARRAYS"]["mastery"] > 0.5
        assert "TREES" not in s["concepts"]  # no evidence = unknown, not weak

        # --- Next action: a gap-directed recommendation with an explanation -------
        nxt = seeded.get("/api/learners/%s/next-action" % user).json()
        assert nxt["gap"]["type"] == "CONCEPT_GAP"
        assert nxt["gap"]["concept"] == "LINKED_LIST"
        assert any("LINKED_LIST mastery estimate" in e
                   for e in nxt["gap"]["evidence"])
        rec = nxt["recommendation"]
        assert rec["type"] == "EASIER_PROBLEM"
        assert rec["topic"] == "LINKED_LIST"
        assert rec["decidedBy"] == "JEV"
        assert rec["reason"]
        assert isinstance(rec["evidence"], list) and rec["evidence"]
        assert 0.0 <= rec["confidence"] <= 1.0
        assert 0.0 <= rec["difficulty"] <= 10.0
        assert nxt["feedback"]["required"] is True
        recommended = rec["problemId"]
        problem = seeded.get("/api/problems/" + recommended)
        assert problem.status_code == 200
        assert "LINKED_LIST" in problem.json()["concepts"]
        mastery_before = linked["mastery"]

        # --- The outcome event on the recommended problem becomes new evidence ----
        sent_ids.append(
            send_event(seeded, user, recommended, "ACCEPTED")["eventId"])

        s = seeded.get("/api/learners/%s/state" % user).json()
        assert s["totalEvents"] == 4
        assert s["concepts"]["LINKED_LIST"]["mastery"] > mastery_before

        # --- The audit row now carries a recorded reward --------------------------
        recs = seeded.get("/api/learners/%s/recommendations" % user).json()
        assert len(recs) == 1
        row = recs[0]
        assert row["recommendationId"] == rec["recommendationId"]
        assert row["problemId"] == recommended
        assert row["type"] == "EASIER_PROBLEM"
        assert row["decidedBy"] == "JEV"
        assert row["reward"] is not None
        assert 0.0 <= row["reward"] <= 1.0

        # --- History reflects every event, newest first ---------------------------
        history = seeded.get("/api/learners/%s/history" % user).json()
        assert len(history) == 4
        assert [h["problemId"] for h in history] == [
            recommended, "E2E_LL_M2", "E2E_LL_M1", "E2E_A1"]
        assert history[0]["result"] == "ACCEPTED"
        # Every posted event is stored exactly once, under its own eventId.
        assert [h["eventId"] for h in history] == list(reversed(sent_ids))

    def test_mastery_rises_monotonically_with_repeated_success(self, seeded):
        """LearningLoopIT.repeatedSuccessOnSameConceptRaisesMasteryMonotonically."""
        user = unique_user("rep")
        previous = 0.5  # the Beta(1,1) prior mean
        for problem in ("E2E_A1", "E2E_A2"):
            send_event(seeded, user, problem, "ACCEPTED")
            mastery = seeded.get("/api/learners/%s/state" % user).json()[
                "concepts"]["ARRAYS"]["mastery"]
            assert mastery > previous
            previous = mastery
        nxt = seeded.get("/api/learners/%s/next-action" % user).json()
        assert nxt["gap"]["type"] != "CONCEPT_GAP"


# --------------------------------------------------------------------------
# Idempotency and conflicts (LearningLoopIT.duplicateEvent... / eventIdReused...)
# --------------------------------------------------------------------------

class TestEventEdgeCases:
    def test_duplicate_event_replay_is_idempotent(self, seeded):
        user = unique_user("dup")
        r, payload = post_event(seeded, user, "E2E_LL_M1", "WRONG_ANSWER")
        assert r.status_code == 201
        before = seeded.get("/api/learners/%s/state" % user).json()

        for _ in range(3):
            # Re-post the exact same payload (same eventId, same timestamp).
            replay = seeded.post("/api/events", json=payload)
            assert replay.status_code == 200
            assert replay.json()["duplicate"] is True
            assert replay.json()["eventId"] == payload["eventId"]

        assert seeded.get("/api/learners/%s/state" % user).json() == before
        assert len(seeded.get("/api/learners/%s/history" % user).json()) == 1

    def test_event_id_reused_for_another_learner_or_problem_is_409(self, seeded):
        user = unique_user("reuse")
        r, payload = post_event(seeded, user, "E2E_LL_M1", "WRONG_ANSWER")
        assert r.status_code == 201

        stranger = unique_user("stranger")
        clash, _ = post_event(seeded, stranger, "E2E_LL_M1", "WRONG_ANSWER",
                              event_id=payload["eventId"])
        assert_error(clash, 409, "CONFLICT")
        # The conflict is checked BEFORE any learner is created.
        assert seeded.get("/api/learners/%s/state" % stranger).status_code == 404

        other_problem, _ = post_event(seeded, user, "E2E_A1", "ACCEPTED",
                                      event_id=payload["eventId"])
        assert_error(other_problem, 409, "CONFLICT")
        assert len(seeded.get("/api/learners/%s/history" % user).json()) == 1

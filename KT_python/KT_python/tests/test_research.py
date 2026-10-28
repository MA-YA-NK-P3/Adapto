"""Tests for knowledge_tracer.research — the ResearchExportIT behaviours:
gating (404 while disabled, 422 while the pseudonym salt is insecure), the
pseudonymised kt-events-v1 event export, the catalog export and the
kt-decisions-v1 rewarded-decision export (docs/api-flow.md §5.17-5.19,
docs/privacy.md).
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from knowledge_tracer import pseudonyms
from knowledge_tracer.domain import ActionType, Learner, Problem, Recommendation
from knowledge_tracer.errors import BusinessRuleError, NotFoundError
from knowledge_tracer.research import BANDIT_FEATURE_NAMES, ResearchExportService
from tests.conftest import event_request

SECURE_SALT = "s3cr3t-research-salt"


def _enable(settings, salt: str = SECURE_SALT):
    settings.research.export_enabled = True
    settings.research.pseudonym_salt = salt
    return settings


def _learner(session, external_ref: str) -> Learner:
    return session.scalar(select(Learner).where(Learner.external_ref == external_ref))


def _recommendation(learner_id, problem_id=None, reward=0.6, **kwargs) -> Recommendation:
    rewarded_at = kwargs.pop("rewarded_at", None)
    if reward is not None and rewarded_at is None:
        rewarded_at = datetime.now(timezone.utc)
    return Recommendation(
        learner_id=learner_id,
        action=kwargs.pop("action", ActionType.SAME_LEVEL_PROBLEM),
        problem_id=problem_id,
        decided_by=kwargs.pop("decided_by", "JEV"),
        provider=kwargs.pop("provider", "deterministic"),
        confidence=kwargs.pop("confidence", 0.8),
        expected_value=kwargs.pop("expected_value", 0.75),
        predicted_success=kwargs.pop("predicted_success", 0.51),
        ability_at_decision=kwargs.pop("ability_at_decision", 4.55),
        context_json=kwargs.pop("context_json", None),
        reason=kwargs.pop("reason", "test"),
        reward=reward,
        rewarded_at=rewarded_at,
        **kwargs)


# --- Gating (ResearchExportService.requireEnabled) ---------------------------------


class TestGating:
    def test_export_disabled_by_default_raises_not_found(self, session, settings):
        service = ResearchExportService(session, settings)
        with pytest.raises(NotFoundError) as exc:
            service.require_enabled()
        assert "Research export is disabled" in str(exc.value)
        assert exc.value.http_status == 404

    def test_default_salt_is_insecure(self, session, settings):
        settings.research.export_enabled = True  # salt stays "change-me"
        service = ResearchExportService(session, settings)
        with pytest.raises(BusinessRuleError) as exc:
            service.require_enabled()
        assert "KT_PSEUDONYM_SALT" in str(exc.value)
        assert exc.value.http_status == 422

    def test_blank_salt_is_insecure(self, session, settings):
        settings.research.export_enabled = True
        settings.research.pseudonym_salt = "   "
        with pytest.raises(BusinessRuleError):
            ResearchExportService(session, settings).require_enabled()

    def test_enabled_with_secure_salt_passes(self, session, settings):
        _enable(settings)
        ResearchExportService(session, settings).require_enabled()  # no raise


# --- kt-events-v1 event export -------------------------------------------------------


class TestEventExport:
    def test_events_are_pseudonymised_grouped_and_sequenced(
            self, session, settings, mini_catalog, services):
        _enable(settings)
        services.ingestion.ingest(event_request(user="U1", problem="LC_E1",
                                                result="ACCEPTED", minutes_ago=50))
        services.ingestion.ingest(event_request(user="U2", problem="LC_E2",
                                                result="WRONG_ANSWER", minutes_ago=40))
        services.ingestion.ingest(event_request(user="U1", problem="LC_E2", result="ACCEPTED",
                                                minutes_ago=30, session_id="sess-1",
                                                time_spent_seconds=120))
        services.ingestion.ingest(event_request(user="U1", problem="LC_E1", result="ACCEPTED",
                                                attempt=2, minutes_ago=20, hint_used=True))
        services.ingestion.ingest(event_request(user="U2", problem="LC_E3",
                                                result="ABANDONED", minutes_ago=10))
        session.commit()

        out = io.BytesIO()
        written = ResearchExportService(session, settings).write_events(out)
        assert written == 5
        raw = out.getvalue().decode("utf-8")
        assert '"U1"' not in raw and '"U2"' not in raw  # raw external refs never exported

        records = [json.loads(line) for line in raw.splitlines()]
        by_learner = {}
        for r in records:
            assert r["schema"] == "kt-events-v1"
            assert r["learnerId"].startswith("L-")
            by_learner.setdefault(r["learnerId"], []).append(r)
        assert len(by_learner) == 2
        for rows in by_learner.values():
            assert [r["seq"] for r in rows] == list(range(len(rows)))
            timestamps = [r["timestamp"] for r in rows]
            assert timestamps == sorted(timestamps)  # chronological within a learner

        u1 = by_learner[pseudonyms.of("U1", SECURE_SALT)]
        assert len(u1) == 3
        first = u1[0]
        assert first["problemId"] == "LC_E1"
        assert first["result"] == "ACCEPTED"
        assert first["correct"] == 1
        assert first["attemptNumber"] == 1
        assert first["hintUsed"] is False
        assert first["solutionViewed"] is False
        assert first["timeSpentSeconds"] is None
        assert first["sessionId"] is None
        assert first["timestamp"].endswith("Z")
        uuid.UUID(first["eventId"])  # parses as a UUID
        second = u1[1]  # LC_E2, 30 minutes ago
        assert second["problemId"] == "LC_E2"
        assert second["sessionId"] == "sess-1"
        assert second["timeSpentSeconds"] == 120
        third = u1[2]  # LC_E1 retry, 20 minutes ago
        assert third["problemId"] == "LC_E1"
        assert third["attemptNumber"] == 2
        assert third["hintUsed"] is True
        u2 = by_learner[pseudonyms.of("U2", SECURE_SALT)]
        assert [r["correct"] for r in u2] == [0, 0]
        assert u2[0]["result"] == "WRONG_ANSWER"

        # self-reports and free text are never exported
        for r in records:
            for field in ("confidence", "perceivedDifficulty", "blocker"):
                assert field not in r

    def test_pseudonym_stable_per_salt_and_unlinkable_after_rotation(
            self, session, settings, mini_catalog, services):
        services.ingestion.ingest(event_request(user="U1", problem="LC_E1"))
        session.commit()
        _enable(settings, salt="salt-one")

        first = io.BytesIO()
        ResearchExportService(session, settings).write_events(first)
        id_first = json.loads(first.getvalue().splitlines()[0])["learnerId"]
        again = io.BytesIO()
        ResearchExportService(session, settings).write_events(again)
        id_again = json.loads(again.getvalue().splitlines()[0])["learnerId"]
        assert id_first == id_again == pseudonyms.of("U1", "salt-one")

        settings.research.pseudonym_salt = "salt-two"  # rotate the salt
        rotated = io.BytesIO()
        ResearchExportService(session, settings).write_events(rotated)
        id_rotated = json.loads(rotated.getvalue().splitlines()[0])["learnerId"]
        assert id_rotated == pseudonyms.of("U1", "salt-two")
        assert id_rotated != id_first
        assert "U1" not in id_first and "U1" not in id_rotated

    def test_events_export_pages(self, session, settings, mini_catalog, services, monkeypatch):
        monkeypatch.setattr("knowledge_tracer.research.PAGE", 2)
        _enable(settings)
        for i in range(3):
            services.ingestion.ingest(event_request(user="U1", problem="LC_E1",
                                                    attempt=i + 1, minutes_ago=30 - i * 10))
        services.ingestion.ingest(event_request(user="U2", problem="LC_E2", minutes_ago=5))
        services.ingestion.ingest(event_request(user="U2", problem="LC_E3", minutes_ago=1))
        session.commit()

        out = io.BytesIO()
        assert ResearchExportService(session, settings).write_events(out) == 5
        records = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
        by_learner = {}
        for r in records:
            by_learner.setdefault(r["learnerId"], []).append(r["seq"])
        assert sorted(by_learner[pseudonyms.of("U1", SECURE_SALT)]) == [0, 1, 2]
        assert sorted(by_learner[pseudonyms.of("U2", SECURE_SALT)]) == [0, 1]

    def test_empty_database_exports_nothing(self, session, settings):
        _enable(settings)
        out = io.BytesIO()
        assert ResearchExportService(session, settings).write_events(out) == 0
        assert out.getvalue() == b""


# --- Catalog export ------------------------------------------------------------------


class TestCatalogExport:
    def test_catalog_schema(self, session, settings, mini_catalog):
        _enable(settings)
        doc = ResearchExportService(session, settings).catalog()
        assert doc["_note"] == "Exported by the backend (GET /api/research/catalog)"

        codes = [c["code"] for c in doc["concepts"]]
        assert codes == sorted(codes)
        assert len(codes) == 5
        dp = next(c for c in doc["concepts"] if c["code"] == "DYNAMIC_PROGRAMMING")
        assert dp["name"] == "Dynamic Programming"
        assert dp["prerequisites"] == ["ARRAYS", "HASHING"]
        arrays = next(c for c in doc["concepts"] if c["code"] == "ARRAYS")
        assert arrays["prerequisites"] == []

        keys = [p["problemId"] for p in doc["problems"]]
        assert keys == sorted(keys)
        assert len(keys) == 11
        e2 = next(p for p in doc["problems"] if p["problemId"] == "LC_E2")
        assert e2["title"] == "LC_E2"
        assert e2["staticDifficulty"] == "EASY"
        assert e2["priorDifficulty"] == 3.2
        assert e2["estimatedDifficulty"] == 3.2
        assert e2["acceptanceRate"] == 0.50
        assert e2["likes"] is None
        assert e2["paidOnly"] is False
        # concept weights are strongest first, and `concepts` mirrors their key order
        assert list(e2["conceptWeights"].items()) == [("HASHING", 0.9), ("ARRAYS", 0.6)]
        assert e2["concepts"] == ["HASHING", "ARRAYS"]
        paid = next(p for p in doc["problems"] if p["problemId"] == "LC_PAID")
        assert paid["paidOnly"] is True

        # the catalog is public reference data — it contains no learner identifiers
        assert "learner" not in json.dumps(doc).lower()


# --- kt-decisions-v1 decision export -------------------------------------------------


class TestDecisionExport:
    def test_rewarded_decisions_exported_pseudonymised(
            self, session, settings, mini_catalog, services):
        _enable(settings)
        services.ingestion.ingest(event_request(user="U1", problem="LC_E1"))
        session.commit()
        learner = _learner(session, "U1")
        problem = session.scalar(select(Problem).where(Problem.problem_key == "LC_E1"))
        context = [1.0, 0.51, 1.0, -0.01, 0.81, 0.0, 0.0, 0.75]
        rewarded = _recommendation(learner.id, problem.id,
                                   context_json=json.dumps(context))
        pending = _recommendation(learner.id, problem.id, reward=None)  # never exported
        session.add_all([rewarded, pending])
        session.commit()

        out = io.BytesIO()
        n = ResearchExportService(session, settings).write_decisions(out, 10000)
        assert n == 1
        raw = out.getvalue().decode("utf-8")
        assert '"U1"' not in raw
        row = json.loads(raw.splitlines()[0])
        assert row["schema"] == "kt-decisions-v1"
        uuid.UUID(row["recommendationId"])
        assert row["learnerId"] == pseudonyms.of("U1", SECURE_SALT)
        assert row["createdAt"].endswith("Z")
        assert row["action"] == "SAME_LEVEL_PROBLEM"
        assert row["problemId"] == "LC_E1"
        assert row["provider"] == "deterministic"
        assert row["decidedBy"] == "JEV"
        assert row["confidence"] == 0.8
        assert row["expectedValue"] == 0.75
        assert row["predictedSuccess"] == 0.51
        assert row["abilityAtDecision"] == 4.55
        assert row["contextFeatures"] == BANDIT_FEATURE_NAMES
        assert row["context"] == context
        assert row["reward"] == 0.6
        assert row["rewardedAt"].endswith("Z")

    def test_newest_reward_first_and_limit_clamped(
            self, session, settings, mini_catalog, services):
        _enable(settings)
        services.ingestion.ingest(event_request(user="U1", problem="LC_E1"))
        session.commit()
        learner = _learner(session, "U1")
        t0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        session.add(_recommendation(learner.id, reward=0.1, rewarded_at=t0))
        session.add(_recommendation(learner.id, reward=0.9,
                                    rewarded_at=t0 + timedelta(hours=2)))
        session.add(_recommendation(learner.id, reward=0.5,
                                    rewarded_at=t0 + timedelta(hours=1)))
        session.commit()
        service = ResearchExportService(session, settings)

        out = io.BytesIO()
        assert service.write_decisions(out, 10000) == 3
        rows = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
        assert [r["reward"] for r in rows] == [0.9, 0.5, 0.1]

        out = io.BytesIO()
        assert service.write_decisions(out, 2) == 2  # limit honoured
        # a non-positive limit is clamped to 1 (the API layer rejects it with 400 first)
        out = io.BytesIO()
        assert service.write_decisions(out, 0) == 1

    def test_unparseable_context_exports_null(self, session, settings, mini_catalog, services):
        _enable(settings)
        services.ingestion.ingest(event_request(user="U1", problem="LC_E1"))
        session.commit()
        learner = _learner(session, "U1")
        session.add(_recommendation(learner.id, reward=0.5, context_json="{broken"))
        session.add(_recommendation(learner.id, reward=0.4, context_json=None))
        session.commit()

        out = io.BytesIO()
        assert ResearchExportService(session, settings).write_decisions(out, 10) == 2
        rows = [json.loads(line) for line in out.getvalue().decode("utf-8").splitlines()]
        assert [r["context"] for r in rows] == [None, None]

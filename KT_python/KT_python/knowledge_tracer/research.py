"""Research exports (port of research.ResearchExportService; docs/data.md,
docs/privacy.md, docs/api-flow.md §5.17-5.19).

The event export uses the same kt-events-v1 schema as the synthetic dataset, so
model/dkt/train_dkt.py and model/evaluation/evaluate_kt.py run unchanged on real
data. Learners appear only as keyed pseudonyms (pseudonyms.py); free-text fields
(blocker, feedback) and self-reports (confidence, perceivedDifficulty) are never
exported.

Every export is gated by require_enabled() — the API layer (the
ResearchExportController equivalent) calls it BEFORE invoking an export, exactly
like the Java controller: export disabled -> NotFoundError (404), blank or default
pseudonym salt -> BusinessRuleError (422). The export methods themselves do not
re-check, mirroring the Java service.

The NDJSON writers take a BINARY stream (anything with write(bytes), e.g.
io.BytesIO or a file opened "wb") and flush it per page, like the Java
OutputStream-based streaming.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import pseudonyms
from .config import Settings
from .domain import (Concept, InteractionEvent, Learner, Prerequisite, Problem,
                     ProblemConcept, Recommendation)
from .errors import BusinessRuleError, NotFoundError

# ResearchExportService.PAGE: events are exported page by page (bounded memory).
PAGE = 2000

# write_decisions clamps the requested limit into [1, MAX_DECISIONS_LIMIT], the
# Java Math.max(1, Math.min(limit, 100_000)).
MAX_DECISIONS_LIMIT = 100_000

# decision.bandit.BanditFeatures.NAMES (BanditFeatures.VERSION = 1). Duplicated here
# so the research export does not depend on the decision module; must change when
# BanditFeatures.VERSION does.
BANDIT_FEATURE_NAMES: List[str] = [
    "bias", "predictedSuccess", "information", "relativeDifficulty",
    "abilityUncertainty", "calibrated", "gapMatch", "expectedValue",
]


def _iso_instant(dt: datetime) -> str:
    """Format like Java's Instant.toString(): UTC with a 'Z' suffix, fractional
    seconds omitted when zero else 3 or 6 digits (our precision is microseconds)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    micros = dt.microsecond
    if micros == 0:
        return base + "Z"
    if micros % 1000 == 0:
        return f"{base}.{micros // 1000:03d}Z"
    return f"{base}.{micros:06d}Z"


def _json_line(record: Dict[str, Any]) -> bytes:
    """One compact JSON line, UTF-8 — ObjectMapper.writeValueAsBytes(record) + '\\n'."""
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class ResearchExportService:
    """Pseudonymised research exports (port of research.ResearchExportService)."""

    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def require_enabled(self) -> None:
        """The export guards, checked by the API layer before any export runs."""
        if not self.settings.research.export_enabled:
            raise NotFoundError(
                "Research export is disabled (knowledge-tracer.research.export-enabled=false)")
        if pseudonyms.is_insecure_salt(self.settings.research.pseudonym_salt):
            raise BusinessRuleError(
                "Set a secret KT_PSEUDONYM_SALT before exporting research data")

    def write_events(self, out) -> int:
        """Stream all events as kt-events-v1 JSON lines, page by page (bounded memory).

        One line per event, grouped by learner, chronological within a learner;
        `seq` restarts at 0 for each learner. Returns the number of lines written.
        """
        salt = self.settings.research.pseudonym_salt
        written = 0
        current_learner: Optional[uuid.UUID] = None
        seq = 0
        page = 0
        while True:
            rows = list(self.session.scalars(
                select(InteractionEvent)
                .order_by(InteractionEvent.learner_id, InteractionEvent.occurred_at,
                          InteractionEvent.created_at, InteractionEvent.id)
                .limit(PAGE)
                .offset(page * PAGE)))
            if not rows:
                return written
            for e in rows:
                if e.learner_id != current_learner:
                    current_learner = e.learner_id
                    seq = 0
                record = {
                    "schema": "kt-events-v1",
                    "learnerId": pseudonyms.of(e.learner.external_ref, salt),
                    "seq": seq,
                    "eventId": str(e.event_id),
                    "problemId": e.problem.problem_key,
                    "timestamp": _iso_instant(e.occurred_at),
                    "sessionId": e.session_id,
                    "attemptNumber": e.attempt_number,
                    "result": e.result.name,
                    "correct": 1 if e.result.is_correct else 0,
                    "timeSpentSeconds": e.time_spent_seconds,
                    "hintUsed": e.hint_used,
                    "solutionViewed": e.solution_viewed,
                }
                seq += 1
                out.write(_json_line(record))
                written += 1
            out.flush()
            page += 1

    def catalog(self) -> Dict[str, Any]:
        """Current catalog in the data/seed/catalog_1000.json schema (for training on
        real data). Concepts sorted by code, problems by problemKey, concept weights
        strongest first."""
        tags: Dict[uuid.UUID, List[ProblemConcept]] = {}
        for pc in self.session.scalars(select(ProblemConcept)):
            tags.setdefault(pc.problem_id, []).append(pc)
        concepts_by_id = {c.id: c for c in self.session.scalars(select(Concept))}
        prereq_codes: Dict[uuid.UUID, List[str]] = {}
        for p in self.session.scalars(select(Prerequisite)):
            prereq_codes.setdefault(p.concept_id, []).append(
                concepts_by_id[p.prerequisite_id].code)

        concepts = []
        for c in sorted(concepts_by_id.values(), key=lambda c: c.code):
            concepts.append({
                "code": c.code,
                "name": c.name,
                "prerequisites": sorted(prereq_codes.get(c.id, [])),
            })
        problems = []
        for p in sorted(self.session.scalars(select(Problem)), key=lambda p: p.problem_key):
            weights: Dict[str, float] = {}
            for pc in sorted(tags.get(p.id, []), key=lambda pc: pc.weight, reverse=True):
                weights[pc.concept.code] = pc.weight
            problems.append({
                "problemId": p.problem_key,
                "title": p.title,
                "staticDifficulty": p.static_difficulty.name,
                "priorDifficulty": p.prior_difficulty,
                "estimatedDifficulty": p.estimated_difficulty,
                "acceptanceRate": p.acceptance_rate,
                "likes": p.likes,
                "paidOnly": p.paid_only,
                "concepts": list(weights.keys()),
                "conceptWeights": weights,
            })
        return {
            "_note": "Exported by the backend (GET /api/research/catalog)",
            "concepts": concepts,
            "problems": problems,
        }

    def write_decisions(self, out, limit: int) -> int:
        """Logged Jev decisions with their rewards (offline policy evaluation / bandit
        warm start): only recommendations that already have a reward, newest reward
        first, as kt-decisions-v1 JSON lines. Returns the number of lines written."""
        salt = self.settings.research.pseudonym_salt
        capped = max(1, min(limit, MAX_DECISIONS_LIMIT))
        rows = list(self.session.scalars(
            select(Recommendation)
            .where(Recommendation.reward.is_not(None))
            .order_by(Recommendation.rewarded_at.desc())
            .limit(capped)))
        n = 0
        for r in rows:
            out.write(_json_line(self._decision_row(r, salt)))
            n += 1
        out.flush()
        return n

    def _decision_row(self, r: Recommendation, salt: str) -> Dict[str, Any]:
        learner = self.session.get(Learner, r.learner_id)
        context: Any = None
        if r.context_json is not None:
            try:
                context = json.loads(r.context_json)
            except ValueError:
                context = None
        return {
            "schema": "kt-decisions-v1",
            "recommendationId": str(r.id),
            "learnerId": pseudonyms.of(learner.external_ref, salt),
            "createdAt": _iso_instant(r.created_at),
            "action": r.action.name,
            "problemId": r.problem.problem_key if r.problem is not None else None,
            "provider": r.provider,
            "decidedBy": r.decided_by,
            "confidence": r.confidence,
            "expectedValue": r.expected_value,
            "predictedSuccess": r.predicted_success,
            "abilityAtDecision": r.ability_at_decision,
            "contextFeatures": list(BANDIT_FEATURE_NAMES),
            "context": context,
            "reward": r.reward,
            "rewardedAt": _iso_instant(r.rewarded_at) if r.rewarded_at is not None else None,
        }

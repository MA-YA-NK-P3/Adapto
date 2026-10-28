#!/usr/bin/env python3
"""End-to-end demo of the learning loop, no HTTP server needed.

A simulated learner with a hidden "true" ability of 4.0 works through the real
1000-problem catalog. Each round the system recommends an action, the learner acts
on it (success probability = sigmoid(true ability - difficulty), seeded RNG), and the
outcome is ingested as an event — closing the observe -> estimate -> gap -> recommend
-> reward loop (docs/model-flow.md).

Run from KT_python/:
    PYTHONPATH=pylib:. python3 scripts/demo.py
"""
from __future__ import annotations

import random
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow running from anywhere

from knowledge_tracer.config import Settings
from knowledge_tracer.container import Container
from knowledge_tracer.domain import InteractionResult
from knowledge_tracer.ingestion import InteractionEventRequest
from knowledge_tracer.irt import sigmoid

TRUE_ABILITY = 4.0          # the simulated learner's hidden skill on the 0-10 scale
ROUNDS = 14
RNG = random.Random(42)     # fixed seed: the demo tells the same story every run


def main() -> None:
    settings = Settings()
    settings.database_url = "sqlite://"          # shared in-memory database
    container = Container(settings)              # seeds the real 1000-problem catalog
    user = "demo-learner"
    attempts = {}                                # problem_key -> attempts so far

    print(f"Knowledge Tracer demo — simulated learner with TRUE ability {TRUE_ABILITY} "
          f"(the system does not know this)\n")

    with container.session_scope() as svc:
        svc.learners.create_if_absent(user)

    for round_no in range(1, ROUNDS + 1):
        with container.session_scope() as svc:
            response = svc.recommendation.next_action(user)
            rec = response.recommendation
            state = response.state

            if not rec.problem_id:
                print(f"round {round_no:2d} | {rec.type} (no problem) — stopping the loop")
                break

            problem_key = rec.problem_id
            difficulty = rec.difficulty if rec.difficulty is not None else 5.0
            correct = RNG.random() < sigmoid(TRUE_ABILITY - difficulty)
            attempts[problem_key] = attempts.get(problem_key, 0) + 1

            svc.ingestion.ingest(InteractionEventRequest(
                user_id=user, problem_id=problem_key,
                timestamp=datetime.now(timezone.utc),
                attempt_number=attempts[problem_key],
                result=InteractionResult.ACCEPTED if correct else InteractionResult.WRONG_ANSWER,
                time_spent_seconds=300 + RNG.randint(0, 600),
                event_id=uuid.uuid4()))

            after = svc.state.get_or_create_state(svc.learners.get_by_external_ref(user))
            gap = response.gap
            print(f"round {round_no:2d} | {rec.type:<20s} {problem_key:<7s} "
                  f"b={difficulty:4.1f} -> {'PASS' if correct else 'fail'} | "
                  f"ability {after.ability:4.2f} ± {after.uncertainty:3.2f} | "
                  f"calibrated={str(after.calibrated):5s} | gap={gap.type}"
                  + (f"({gap.concept})" if gap.concept else ""))

    with container.session_scope() as svc:
        learner = svc.learners.get_by_external_ref(user)
        state = svc.state.get_or_create_state(learner)
        masteries = sorted(svc.state.masteries_of(learner),
                           key=lambda m: m.mastery)
        history = svc.recommendation.history(user, 100)
        rewarded = [h for h in history if h.reward is not None]

        print(f"\n--- Final state ------------------------------------------------")
        print(f"ability estimate : {state.ability:.2f} ± {state.uncertainty:.2f} "
              f"(true value {TRUE_ABILITY})")
        print(f"calibrated       : {state.calibrated} after {state.total_events} events")
        print(f"audit trail      : {len(history)} recommendations, "
              f"{len(rewarded)} rewarded by observed outcomes")
        print(f"\n--- Concept mastery (weakest first) ------------------------------")
        for m in masteries[:8]:
            bar = "#" * int(m.mastery * 20)
            print(f"  {m.concept.code:<22s} {m.mastery:4.2f} {bar:<20s} "
                  f"(evidence {m.evidence_count})")


if __name__ == "__main__":
    main()

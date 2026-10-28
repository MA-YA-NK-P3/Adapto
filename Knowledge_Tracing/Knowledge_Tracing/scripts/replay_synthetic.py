#!/usr/bin/env python3
"""End-to-end check: replay SYNTHETIC learners through the running Java backend.

Each synthetic learner (true ability ~2 / ~5 / ~8) answers random-order problems drawn
from the simulator; events are POSTed to /api/events exactly as the extension would.
Afterwards the backend's ability estimates are compared with the simulator's truth.

    *** SYNTHETIC / DEVELOPMENT DATA — validates plumbing + estimator behaviour only ***

Usage: python3 scripts/replay_synthetic.py [--base-url http://localhost:8080] [--learners 30] [--events 20]
"""
import argparse
import datetime as dt
import json
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "model"))

from kt import metrics  # noqa: E402
from kt.catalog import Catalog  # noqa: E402
from kt.synthetic import SyntheticWorld  # noqa: E402


def call(base, method, path, body=None, api_key=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--learners", type=int, default=30)
    ap.add_argument("--events", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    catalog = Catalog.load()
    world = SyntheticWorld(catalog, seed=args.seed)
    run = uuid.uuid4().hex[:6].upper()
    learners = world.population(args.learners, prefix=f"SYN-{run}")
    clock = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

    true_ab, est_ab, uncertainties, gaps_hit, gap_types = [], [], [], 0, {}
    for learner in learners:
        order = list(catalog.labels)
        world.rng.shuffle(order)
        for pid in order[:args.events]:
            r = world.attempt(learner, pid)
            clock += dt.timedelta(minutes=5)
            status, body = call(args.base_url, "POST", "/api/events", {
                "userId": learner.learner_id, "problemId": pid, "eventId": str(uuid.uuid4()),
                "timestamp": clock.isoformat().replace("+00:00", "Z"),
                "attemptNumber": r["attemptNumber"], "result": r["result"],
                "timeSpentSeconds": r["timeSpentSeconds"], "hintUsed": False, "solutionViewed": False,
                "sessionId": f"synthetic-{run}",
            }, args.api_key)
            if status != 201:
                sys.exit(f"event rejected ({status}): {body}")
        _, state = call(args.base_url, "GET", f"/api/learners/{learner.learner_id}/state", api_key=args.api_key)
        _, nxt = call(args.base_url, "GET", f"/api/learners/{learner.learner_id}/next-action", api_key=args.api_key)
        true_ab.append(learner.ability)
        est_ab.append(state["ability"])
        uncertainties.append(state["uncertainty"])
        gap = nxt.get("gap", {})
        gap_types[gap.get("type")] = gap_types.get(gap.get("type"), 0) + 1
        if learner.weak_concept and learner.weak_concept in (gap.get("concept"), gap.get("prerequisite")):
            gaps_hit += 1

    report = {
        "banner": "SYNTHETIC / DEVELOPMENT DATA",
        "run": run,
        "learners": len(learners),
        "eventsPerLearner": args.events,
        "abilitySpearman": round(metrics.spearman(est_ab, true_ab), 3),
        "abilityMAE": round(metrics.mae(est_ab, true_ab), 3),
        "meanUncertainty": round(sum(uncertainties) / len(uncertainties), 3),
        "weakConceptFlaggedByBackend": f"{gaps_hit}/{len(learners)}",
        "gapTypes": gap_types,
    }
    print(json.dumps(report, indent=2))
    out = ROOT / "model" / "reports" / "replay-results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    if report["abilitySpearman"] < 0.6:
        sys.exit("ability ranking not recovered (Spearman < 0.6)")


if __name__ == "__main__":
    main()

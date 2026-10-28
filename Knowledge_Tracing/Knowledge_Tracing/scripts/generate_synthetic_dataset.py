#!/usr/bin/env python3
"""Generate the synthetic kt-events-v1 dataset over the 1000-problem catalog.

    *** SYNTHETIC / DEVELOPMENT DATA — NOT EVIDENCE OF REAL LEARNING OUTCOMES ***

Usage: python3 scripts/generate_synthetic_dataset.py [--learners 2500] [--seed 20260929]
                                                     [--catalog data/seed/catalog_1000.json]
                                                     [--out data/synthetic/v2]
Outputs (deterministic for a given seed):
  events.jsonl.gz        kt-events-v1 events with a per-event `truth` block
  events_sample.jsonl    first 30 learners, uncompressed, for browsing
  learners.jsonl.gz      per-learner ground truth (cohort, ability, weak concepts, mastery)
  items_truth.json       per-problem true difficulty vs catalog prior
  summary.json           dataset statistics (also printed)
The generative model is documented in model/kt/simulator.py and docs/data.md.
"""
import argparse
import json
import pathlib
import statistics
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model"))

from kt import events as ev  # noqa: E402
from kt.catalog import Catalog  # noqa: E402
from kt.simulator import World  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--learners", type=int, default=2500)
    ap.add_argument("--seed", type=int, default=20260929)
    ap.add_argument("--catalog", default=str(ROOT / "data" / "seed" / "catalog_1000.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "synthetic" / "v2"))
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    world = World(Catalog.load(pathlib.Path(args.catalog)), seed=args.seed)

    learners, sample = [], []
    stats = Counter()
    results = Counter()
    seq_lengths, p_true = [], []

    def all_events():
        for i, (events, truth) in enumerate(world.generate(args.learners)):
            learners.append(truth)
            seq_lengths.append(len(events))
            for seq, e in enumerate(events):
                record = {"schema": ev.SCHEMA, "eventId": ev.event_id(e["learnerId"], seq), "seq": seq, **e}
                results[e["result"]] += 1
                stats["hint"] += int(e["hintUsed"])
                stats["solution"] += int(e["solutionViewed"])
                stats["retry"] += int(e["attemptNumber"] > 1)
                p_true.append(e["truth"]["pCorrect"])
                if i < 30:
                    sample.append(record)
                yield record

    n_events = ev.write_jsonl(out / "events.jsonl.gz", all_events())
    ev.write_jsonl(out / "events_sample.jsonl", sample)
    ev.write_jsonl(out / "learners.jsonl.gz", learners)
    (out / "items_truth.json").write_text(json.dumps(
        {"_note": "SYNTHETIC ground truth; trueDifficulty0to10 = 5 + 1.25 * trueDifficultyLogit",
         "items": world.item_truth()}, indent=1))

    touched = Counter()
    for r in ev.read_jsonl(out / "events.jsonl.gz"):
        touched[r["problemId"]] += 1
    summary = {
        "_note": "SYNTHETIC / DEVELOPMENT DATA — NOT EVIDENCE OF REAL LEARNING OUTCOMES",
        "schema": ev.SCHEMA,
        "seed": args.seed,
        "catalog": pathlib.Path(args.catalog).name,
        "learners": len(learners),
        "events": n_events,
        "problemsCovered": len(touched),
        "eventsPerProblem": {"median": statistics.median(touched.values()), "max": max(touched.values())},
        "sequenceLength": {"mean": round(statistics.fmean(seq_lengths), 1),
                           "median": statistics.median(seq_lengths),
                           "p90": sorted(seq_lengths)[int(0.9 * len(seq_lengths))],
                           "max": max(seq_lengths)},
        "results": dict(results.most_common()),
        "correctRate": round(results["ACCEPTED"] / n_events, 4),
        "meanTrueP": round(statistics.fmean(p_true), 4),
        "hintRate": round(stats["hint"] / n_events, 4),
        "solutionViewRate": round(stats["solution"] / n_events, 4),
        "retryRate": round(stats["retry"] / n_events, 4),
        "cohorts": dict(Counter(l["cohort"] for l in learners)),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

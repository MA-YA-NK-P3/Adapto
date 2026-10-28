#!/usr/bin/env python3
"""Generate the Java/Python DKT parity fixture (standard library only).

Writes backend/src/test/resources/dkt/fixture-model.json (a small random kt-dkt-v1 model
over the real 25 catalog concepts, H=4) and fixture-expected.json (a 12-step sequence
with the reference predictions and mastery computed by features.ReferenceDkt).
DktModelTest replays the sequence in Java and must match to 1e-9.
"""
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from features import FORMAT, ReferenceDkt, scaled_difficulty  # noqa: E402

OUT = ROOT / "backend" / "src" / "test" / "resources" / "dkt"


def main():
    rng = random.Random(42)
    catalog = json.loads((ROOT / "data" / "seed" / "catalog_1000.json").read_text())
    concepts = [c["code"] for c in catalog["concepts"]]
    k, h = len(concepts), 4
    d = 2 * k + 2

    def mat(rows, cols, scale):
        return [[round(rng.uniform(-scale, scale), 6) for _ in range(cols)] for _ in range(rows)]

    model = {
        "format": FORMAT, "createdAt": "2026-01-01T00:00:00Z", "trainedOn": "fixture",
        "concepts": concepts, "inputSize": d, "hiddenSize": h, "maxSequenceLength": 200,
        "conceptBaseline": {c: round(rng.uniform(-0.2, 0.2), 6) for c in concepts},
        "metrics": {"test": {"auc": 0.5}},
        "weights": {
            "lstm.weight_ih": mat(4 * h, d, 0.8), "lstm.weight_hh": mat(4 * h, h, 0.8),
            "lstm.bias_ih": mat(1, 4 * h, 0.3)[0], "lstm.bias_hh": mat(1, 4 * h, 0.3)[0],
            "out.weight": mat(k, h, 1.0), "out.bias": mat(1, k, 0.5)[0],
            "gamma": 0.9, "rho": 0.4, "bias": 0.6,
        },
        "itemOffsets": {"LC1": -0.25, "LC21": 0.3},
    }
    ref = ReferenceDkt(model)
    problems = {p["problemId"]: p for p in catalog["problems"]}
    keys = ["LC1", "LC21", "LC206", "LC1", "LC104", "LC200", "LC322", "LC21", "LC704", "LC15", "LC3", "LC70"]
    state = ref.initial_state()
    seen, steps = set(), []
    for key in keys:
        p = problems[key]
        w = [float(p["conceptWeights"].get(c, 0.0)) for c in concepts]
        diff = scaled_difficulty(float(p["priorDifficulty"]))
        pred = ref.predict(state, w, diff, key in seen, key)
        correct = rng.random() < 0.6
        assisted = rng.random() < 0.2
        state = ref.step(state, w_input(w, correct, diff, assisted))
        steps.append({"problemKey": key, "weights": w, "scaledDifficulty": diff, "attempted": key in seen,
                      "correct": correct, "assisted": assisted, "predictionBefore": pred,
                      "masteryAfter": ref.mastery(state)})
        seen.add(key)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "fixture-model.json").write_text(json.dumps(model, separators=(",", ":")))
    (OUT / "fixture-expected.json").write_text(json.dumps({"steps": steps}, indent=1))
    print(f"wrote {OUT}/fixture-model.json and fixture-expected.json ({len(steps)} steps)")


def w_input(w, correct, diff, assisted):
    y = 1.0 if correct else 0.0
    return [v * y for v in w] + [v * (1 - y) for v in w] + [diff, 1.0 if assisted else 0.0]


if __name__ == "__main__":
    main()

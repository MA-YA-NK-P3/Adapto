#!/usr/bin/env python3
"""Offline evaluation: prediction baselines, IRT recovery, policy simulation + ablation.

    *** ALL RESULTS ARE ON SYNTHETIC / DEVELOPMENT DATA ***

Usage (from repo root):
    python3 model/evaluation/run_evaluation.py [--seed 7] [--train 300] [--test 150] [--sim 150]

Writes:
    data/synthetic/interactions.jsonl        generated dataset (train + test)
    model/reports/evaluation-results.json    machine-readable results
    model/reports/evaluation-report.md       human-readable report
"""
import argparse
import copy
import json
import math
import pathlib
import random
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))

from kt import dkt, metrics  # noqa: E402
from kt.catalog import Catalog  # noqa: E402
from kt.irt import STATIC_PRIOR, calibrate_items, estimate_ability  # noqa: E402
from kt.mastery import MasteryState  # noqa: E402
from kt.policies import Observed, all_policies  # noqa: E402
from kt.predictors import (IrtMasteryPredictor, StaticDifficultyPredictor,  # noqa: E402
                           SuccessRatePredictor)
from kt.synthetic import SyntheticWorld  # noqa: E402

BANNER = "SYNTHETIC / DEVELOPMENT DATA — not evidence of real learning outcomes"
SEQUENCE_LENGTH = 20
SIM_STEPS = 20


# ---------------------------------------------------------------- data generation
def generate(world: SyntheticWorld, n: int, prefix: str):
    learners = world.population(n, prefix=prefix)
    records, sequences = [], []
    for learner in learners:
        order = list(world.catalog.labels)
        world.rng.shuffle(order)
        seq = []
        for step, pid in enumerate(order[:SEQUENCE_LENGTH]):
            r = world.attempt(learner, pid)
            seq.append((pid, r["correct"]))
            records.append({"synthetic": True, "learnerId": learner.learner_id, "step": step,
                            "trueAbility": round(learner.ability, 3), **{k: r[k] for k in
                            ("problemId", "correct", "result", "timeSpentSeconds", "attemptNumber")},
                            "trueP": round(r["trueP"], 4)})
        sequences.append(seq)
    return learners, sequences, records


# ---------------------------------------------------------------- prediction eval
def run_predictor(pred, sequences):
    ys, ps = [], []
    for seq in sequences:
        pred.reset()
        for pid, y in seq:
            ps.append(pred.predict(pid))
            ys.append(y)
            pred.observe(pid, y)
    return ys, ps


def fit_weights(catalog, difficulty, train):
    grid = [0.0, 0.25, 0.5, 0.75, 1.0, 1.5]

    def loss(wm, wg):
        return metrics.log_loss(*run_predictor(IrtMasteryPredictor("fit", catalog, difficulty, wm, wg), train))

    wm = min(grid, key=lambda w: loss(w, 0.0))
    wg_only = min(grid, key=lambda w: loss(0.0, w))
    wg_full = min(grid, key=lambda w: loss(wm, w))
    return wm, wg_only, wg_full


# ---------------------------------------------------------------- policy simulation
def simulate(world, learners, difficulty, seed):
    results = {}
    for policy in all_policies():
        rows = []
        for i, base in enumerate(learners):
            learner = copy.deepcopy(base)
            world.rng = random.Random(seed * 100_003 + i)  # common random numbers across policies
            policy_rng = random.Random(seed * 7 + i)
            obs = Observed(world.catalog, difficulty)
            true_ps, ys, streak, frustrations = [], [], 0, 0
            for _ in range(SIM_STEPS):
                pid = policy.choose(obs, policy_rng)
                if pid is None:
                    break
                r = world.attempt(learner, pid)
                obs.record(pid, r["correct"])
                true_ps.append(r["trueP"])
                ys.append(r["correct"])
                streak = 0 if r["correct"] else streak + 1
                if streak == 3:
                    frustrations += 1
            concepts = world.catalog.concepts
            rows.append({
                "ability": base.ability,
                "success": sum(ys) / len(ys) if ys else float("nan"),
                "zoneDistance": sum(abs(p - 0.65) for p in true_ps) / len(true_ps) if true_ps else float("nan"),
                "tooEasy": sum(p > 0.9 for p in true_ps) / len(true_ps) if true_ps else float("nan"),
                "tooHard": sum(p < 0.2 for p in true_ps) / len(true_ps) if true_ps else float("nan"),
                "frustrationStreaks": frustrations,
                "skillGain": sum(learner.skill[c] - base.skill[c] for c in concepts) / len(concepts),
                "weakConceptGain": (learner.skill[base.weak_concept] - base.skill[base.weak_concept])
                if base.weak_concept else 0.0,
                "abilityError": abs(obs.ability - base.ability),
            })
        results[policy.name] = rows
    return results


def mean_ci(xs):
    xs = [x for x in xs if not (isinstance(x, float) and math.isnan(x))]
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return m, 1.96 * sd / math.sqrt(n)


SIM_METRICS = [
    ("success", "next-problem success rate", "context"),
    ("zoneDistance", "mean |true P - 0.65| (targeting error)", "lower"),
    ("tooEasy", "share of attempts with true P > 0.9", "lower"),
    ("tooHard", "share of attempts with true P < 0.2", "lower"),
    ("frustrationStreaks", "3-failure streaks per learner", "lower"),
    ("skillGain", "mean simulated skill gain per concept", "higher"),
    ("weakConceptGain", "simulated gain on the weak concept", "higher"),
    ("abilityError", "|final ability estimate - true ability|", "lower"),
]


# ---------------------------------------------------------------- report
def fmt(x, nd=3):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def write_report(out_dir, res):
    lines = [f"# Offline evaluation report", "", f"> **{BANNER}.**", ">",
             f"> Generated {res['generatedAt']} by `model/evaluation/run_evaluation.py` "
             f"(seed {res['config']['seed']}). Re-run to reproduce; numbers change with the seed.", "",
             "## 1. Setup", "",
             f"- Problem pool: {res['config']['problems']} seeded LeetCode problems, "
             f"{res['config']['concepts']} concepts (`data/seed/catalog.json`).",
             f"- Train learners: {res['config']['train']}, test learners: {res['config']['test']}, "
             f"{SEQUENCE_LENGTH} random-order attempts each (random order avoids selection bias in the eval set).",
             "- Item difficulties calibrated on the TRAIN split only; predictor weights fitted on TRAIN only.",
             "- Simulator: per-concept skills, one weak concept per learner, prerequisite penalty, "
             "noisy true difficulty vs static label, practice-driven learning (see `model/kt/synthetic.py`).", "",
             "## 2. Item difficulty recovery (TRAIN calibration vs simulator truth)", "",
             "| Estimate | MAE | RMSE | Spearman |", "|---|---|---|---|"]
    for k, v in res["itemRecovery"].items():
        lines.append(f"| {k} | {fmt(v['mae'])} | {fmt(v['rmse'])} | {fmt(v['spearman'])} |")
    ar = res["abilityRecovery"]
    lines += ["", "## 3. Ability recovery (TEST learners, after 20 responses)", "",
              f"- Spearman(final estimate, true initial ability) = **{fmt(ar['spearman'])}**; "
              f"MAE = {fmt(ar['mae'])} on the 0-10 scale; mean reported uncertainty = {fmt(ar['meanUncertainty'])}.",
              "- Learners also learn during the sequence, so part of the error is real drift, not estimator error.", "",
              "## 3b. Gap detection (TEST learners, mastery layer after 20 random-order responses)", "",
              f"- A gap (weakest concept, >=2 observations, mastery < 0.45) was flagged for "
              f"{res['gapDetection']['gapFlagged']}/{res['gapDetection']['learners']} learners.",
              f"- When flagged, it was the simulator's designated weak concept "
              f"{fmt(res['gapDetection']['top1HitRateWhenFlagged'])} of the time "
              f"(chance {fmt(res['gapDetection']['chanceTop1'])}).",
              f"- Designated weak concept among the 3 lowest-mastery concepts: "
              f"{fmt(res['gapDetection']['top3HitRate'])} (chance {fmt(res['gapDetection']['chanceTop3'])}).",
              "- Low-ability learners fail most concepts, so their weakest concept is often not the designated one.", "",
              "## 4. Next-response prediction (TEST split, online: predict before observing)", "",
              "| Model | Accuracy | AUC | Log loss | Brier | ECE |", "|---|---|---|---|---|---|"]
    for name, m in res["prediction"].items():
        if m.get("status") == "UNAVAILABLE":
            lines.append(f"| {name} | UNAVAILABLE | – | – | – | – |")
        else:
            lines.append(f"| {name} | {fmt(m['accuracy'])} | {fmt(m['auc'])} | {fmt(m['logLoss'])} | "
                         f"{fmt(m['brier'])} | {fmt(m['ece'])} |")
    w = res["fittedWeights"]
    lines += ["", f"Fitted weights (train): mastery term w_m = {w['wMastery']}, prerequisite term "
              f"w_g = {w['wGraphOnly']} (graph only) / {w['wGraphFull']} (with mastery).", "",
              "## 5. Recommendation policy simulation + ablation", "",
              f"{res['config']['sim']} fresh learners (abilities centred at 2 / 5 / 8), {SIM_STEPS} recommended "
              "problems each, identical random streams per learner across policies. Mean ± 95% CI.", ""]
    names = list(res["simulation"].keys())
    lines.append("| Metric | better | " + " | ".join(names) + " |")
    lines.append("|---|---|" + "---|" * len(names))
    for key, label, better in SIM_METRICS:
        cells = [f"{fmt(res['simulation'][n][key][0])} ± {fmt(res['simulation'][n][key][1], 2)}" for n in names]
        lines.append(f"| {label} | {better} | " + " | ".join(cells) + " |")
    d = res["dkt"]
    lines += ["", "## 6. DKT", "", f"Status: **{d['status']}**.", ""] + [f"- {r}" for r in d["reasons"]]
    lines += ["", "## 7. How to read this", "",
              "- These numbers validate that the implementation behaves as designed **under the simulator's "
              "assumptions**. They are not evidence that the system improves real learning.",
              "- Learning-gain metrics depend entirely on the assumed learning dynamics "
              "(gain ∝ p(1-p), i.e. productive struggle). A different assumption can reorder policies.",
              "- The system policies reuse the difficulty scale the simulator was calibrated against; "
              "on real data item calibration would be noisier.",
              "- Real validation requires consented real learner data and a held-out evaluation (docs/evaluation.md).", ""]
    (out_dir / "evaluation-report.md").write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--train", type=int, default=300)
    ap.add_argument("--test", type=int, default=150)
    ap.add_argument("--sim", type=int, default=150)
    ap.add_argument("--out", default=str(ROOT / "model" / "reports"))
    args = ap.parse_args()
    started = time.time()

    catalog = Catalog.load()
    world = SyntheticWorld(catalog, seed=args.seed)
    _, train_seqs, train_rec = generate(world, args.train, "TRAIN")
    test_learners, test_seqs, test_rec = generate(world, args.test, "TEST")

    data_dir = ROOT / "data" / "synthetic"
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "interactions.jsonl").open("w") as f:
        for r in train_rec + [dict(r, split="test") for r in test_rec]:
            f.write(json.dumps(dict({"split": "train"}, **r)) + "\n")

    # item recovery
    difficulty = calibrate_items(train_seqs, catalog.labels)
    pids = list(catalog.labels)
    truth = [world.true_difficulty[p] for p in pids]
    static = [STATIC_PRIOR[catalog.labels[p]] for p in pids]
    calibrated = [difficulty[p] for p in pids]
    item_recovery = {
        "static label prior": {"mae": metrics.mae(static, truth), "rmse": metrics.rmse(static, truth),
                               "spearman": metrics.spearman(static, truth)},
        "IRT calibrated": {"mae": metrics.mae(calibrated, truth), "rmse": metrics.rmse(calibrated, truth),
                           "spearman": metrics.spearman(calibrated, truth)},
    }

    # ability recovery
    est = [estimate_ability([(difficulty[p], y) for p, y in seq]) for seq in test_seqs]
    true_ab = [l.ability for l in test_learners]
    ability_recovery = {"spearman": metrics.spearman([e[0] for e in est], true_ab),
                        "mae": metrics.mae([e[0] for e in est], true_ab),
                        "meanUncertainty": sum(e[1] for e in est) / len(est)}

    # gap detection: does the mastery layer point at the simulator's designated weak concept?
    flagged, hits, hits_top3 = 0, 0, 0
    for learner, seq in zip(test_learners, test_seqs):
        ms = MasteryState()
        for pid, y in seq:
            ms.update(catalog.tags[pid], y)
        weakest = ms.weakest()
        ranked = sorted((m.mastery, c) for c, m in ms.concepts.items() if m.evidence >= 2)
        if weakest:
            flagged += 1
            hits += weakest == learner.weak_concept
        hits_top3 += learner.weak_concept in [c for _, c in ranked[:3]]
    gap_detection = {"learners": len(test_learners), "gapFlagged": flagged,
                     "top1HitRateWhenFlagged": hits / flagged if flagged else float("nan"),
                     "top3HitRate": hits_top3 / len(test_learners),
                     "chanceTop1": 1 / len(catalog.concepts), "chanceTop3": 3 / len(catalog.concepts)}

    # prediction
    wm, wg_only, wg_full = fit_weights(catalog, difficulty, train_seqs)
    predictors = [
        StaticDifficultyPredictor(catalog),
        SuccessRatePredictor(),
        IrtMasteryPredictor("irt-only", catalog, difficulty),
        IrtMasteryPredictor("irt+graph", catalog, difficulty, 0.0, wg_only),
        IrtMasteryPredictor("irt+knowledge-tracing", catalog, difficulty, wm, 0.0),
        IrtMasteryPredictor("full (irt+kt+graph)", catalog, difficulty, wm, wg_full),
    ]
    prediction = {p.name: metrics.summary(*run_predictor(p, test_seqs)) for p in predictors}
    dkt_status = dkt.availability(dkt.build_dataset(train_seqs, catalog.tags), synthetic=True)
    prediction["dkt"] = {"status": "UNAVAILABLE", "reasons": dkt_status["reasons"]}

    # policy simulation on a fresh population
    sim_world = SyntheticWorld(catalog, seed=args.seed + 1)
    sim_world.true_difficulty = world.true_difficulty
    sim_learners = sim_world.population(args.sim, prefix="SIM")
    sim_rows = simulate(sim_world, sim_learners, difficulty, args.seed)
    simulation = {name: {k: mean_ci([r[k] for r in rows]) for k, _, _ in SIM_METRICS}
                  for name, rows in sim_rows.items()}

    res = {
        "banner": BANNER,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {"seed": args.seed, "train": args.train, "test": args.test, "sim": args.sim,
                   "problems": len(catalog.problems), "concepts": len(catalog.concepts)},
        "itemRecovery": item_recovery,
        "abilityRecovery": ability_recovery,
        "gapDetection": gap_detection,
        "fittedWeights": {"wMastery": wm, "wGraphOnly": wg_only, "wGraphFull": wg_full},
        "prediction": prediction,
        "simulation": simulation,
        "dkt": dkt_status,
        "runtimeSeconds": round(time.time() - started, 1),
    }
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evaluation-results.json").write_text(json.dumps(res, indent=2) + "\n")
    write_report(out_dir, res)
    print(f"[{BANNER}]")
    print(json.dumps({k: res[k] for k in ("itemRecovery", "abilityRecovery", "gapDetection", "fittedWeights",
                                          "prediction")}, indent=1))
    for name, m in simulation.items():
        print(f"{name:20s} " + " ".join(f"{k}={m[k][0]:.3f}" for k, _, _ in SIM_METRICS))
    print(f"wrote {out_dir / 'evaluation-report.md'} in {res['runtimeSeconds']}s")


if __name__ == "__main__":
    main()

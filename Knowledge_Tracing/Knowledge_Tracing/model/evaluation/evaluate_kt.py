#!/usr/bin/env python3
"""Held-out evaluation of all knowledge-tracing models on kt-events-v1 data.

    *** On synthetic data these numbers describe behaviour UNDER THE SIMULATOR'S ASSUMPTIONS ***

Usage: python3 model/evaluation/evaluate_kt.py [--events ...] [--catalog ...] [--dkt model/artifacts/dkt/dkt-model.json]

Protocol
  * learner-level split identical to training (kt.events.split_learners, 70/15/15);
  * item difficulties for the "calibrated" models are estimated on TRAIN learners only
    (alternating MAP, like the backend's IrtService.recalibrateItem);
  * every predictor is evaluated online on TEST learners: predict(event) BEFORE observe(event).
Predictors
  oracle            simulator's true P(correct)  (upper bound; synthetic data only)
  prior-only        Rasch with theta = 5 and catalog prior difficulty
  success-rate      Laplace-smoothed running success rate
  irt-prior         Rasch, online MAP ability, catalog prior difficulty   (backend at cold start)
  irt-calibrated    Rasch, online MAP ability, train-calibrated difficulty (backend steady state)
  irt+mastery       irt-calibrated + 0.5 * weighted logit(Beta concept mastery)
  dkt               kt-dkt-v1 LSTM (exported JSON; numpy forward, or pure Python fallback)
Also reported (synthetic only): concept-mastery recovery vs ground truth, weakest-concept
detection, and item-difficulty recovery.
Outputs: model/reports/kt-evaluation.json, model/reports/kt-evaluation.md, model/reports/figures/*.png
"""
import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "model"))
sys.path.insert(0, str(ROOT / "model" / "dkt"))

from kt import events as ev  # noqa: E402
from kt import metrics  # noqa: E402
from kt.irt import estimate_ability, estimate_difficulty, sigmoid  # noqa: E402
from kt.mastery import MasteryState, logit  # noqa: E402

from features import Encoder, ReferenceDkt, assisted  # noqa: E402

WINDOW = 100


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(ROOT / "data" / "synthetic" / "v2" / "events.jsonl.gz"))
    ap.add_argument("--learners", default=str(ROOT / "data" / "synthetic" / "v2" / "learners.jsonl.gz"))
    ap.add_argument("--items", default=str(ROOT / "data" / "synthetic" / "v2" / "items_truth.json"))
    ap.add_argument("--catalog", default=str(ROOT / "data" / "seed" / "catalog_1000.json"))
    ap.add_argument("--dkt", default=str(ROOT / "model" / "artifacts" / "dkt" / "dkt-model.json"))
    ap.add_argument("--out", default=str(ROOT / "model" / "reports"))
    ap.add_argument("--max-test-learners", type=int, default=0, help="0 = all test learners")
    return ap.parse_args()


# ------------------------------------------------------------------ DKT forward
class NumpyDkt:
    """Vectorised kt-dkt-v1 forward pass (identical maths to features.ReferenceDkt)."""

    def __init__(self, doc):
        import numpy as np
        self.np = np
        w = doc["weights"]
        self.w_ih = np.array(w["lstm.weight_ih"])
        self.w_hh = np.array(w["lstm.weight_hh"])
        self.b = np.array(w["lstm.bias_ih"]) + np.array(w["lstm.bias_hh"])
        self.w_out = np.array(w["out.weight"])
        self.b_out = np.array(w["out.bias"])
        self.gamma, self.rho, self.bias = float(w["gamma"]), float(w["rho"]), float(w["bias"])
        self.h = int(doc["hiddenSize"])
        self.item_offsets = {k: float(v) for k, v in doc.get("itemOffsets", {}).items()}

    def initial_state(self):
        return self.np.zeros(self.h), self.np.zeros(self.h)

    def step(self, state, x):
        np = self.np
        h, c = state
        # numpy 2.x + macOS Accelerate raises spurious FP flags in small matmuls; results
        # are verified equal to ReferenceDkt (model/tests/test_dkt_reference.py).
        with np.errstate(all="ignore"):
            g = self.w_ih @ np.asarray(x) + self.w_hh @ h + self.b
        H = self.h
        i, f = 1 / (1 + np.exp(-g[:H])), 1 / (1 + np.exp(-g[H:2 * H]))
        gg, o = np.tanh(g[2 * H:3 * H]), 1 / (1 + np.exp(-g[3 * H:]))
        c = f * c + i * gg
        return o * np.tanh(c), c

    def concept_logits(self, state):
        with self.np.errstate(all="ignore"):
            return self.w_out @ state[0] + self.b_out

    def predict(self, state, weights, difficulty, attempted, problem_id=None):
        y = self.concept_logits(state)
        w = self.np.asarray(weights)
        total = w.sum()
        concept = float(w @ y / total) if total > 0 else 0.0
        return sigmoid(concept - self.gamma * difficulty - self.item_offsets.get(problem_id, 0.0)
                       + (self.rho if attempted else 0.0) + self.bias)

    def mastery(self, state):
        return [sigmoid(float(v) + self.bias) for v in self.concept_logits(state)]


def load_dkt(path):
    p = pathlib.Path(path)
    if not p.exists():
        return None, f"no exported model at {p} (run model/dkt/train_dkt.py or the Colab notebook)"
    doc = json.loads(p.read_text())
    try:
        return NumpyDkt(doc), doc
    except ImportError:
        return ReferenceDkt(doc), doc


# ------------------------------------------------------------------ calibration
def first_attempts(seq):
    """One response per problem (the first), as the backend IRT uses (irt.first-attempts-only)."""
    seen, out = set(), []
    for e in seq:
        if e["problemId"] not in seen:
            seen.add(e["problemId"])
            out.append(e)
    return out


def calibrate(train_seqs, prior, rounds=4, prior_sd=1.5):
    b = dict(prior)
    firsts = [first_attempts(seq) for seq in train_seqs]
    for _ in range(rounds):
        thetas = [estimate_ability([(b[e["problemId"]], e["correct"]) for e in seq[-WINDOW:]])[0]
                  for seq in firsts]
        by_item = {}
        for theta, seq in zip(thetas, firsts):
            for e in seq:
                by_item.setdefault(e["problemId"], []).append((theta, bool(e["correct"])))
        for pid, obs in by_item.items():
            b[pid] = estimate_difficulty(obs[-200:], prior[pid], prior_sd)[0]
    return b


# ------------------------------------------------------------------ online predictors
def run_online(test_seqs, encoder, prior, calibrated, dkt, tags):
    names = ["oracle", "prior-only", "success-rate", "irt-prior", "irt-calibrated", "irt+mastery", "dkt"]
    preds = {n: [] for n in names}
    labels, first_mask = [], []
    final_state = {}
    for lid, seq in test_seqs.items():
        hist_prior, hist_cal = [], []
        theta_p = theta_c = 5.0
        s = n = 0
        mastery = MasteryState()
        state = dkt.initial_state() if dkt else None
        seen = set()
        res_num, res_den = {}, {}
        window_start = max(0, len(seq) - 200)
        for t, e in enumerate(seq):
            pid, y = e["problemId"], bool(e["correct"])
            labels.append(y)
            first_mask.append(e["attemptNumber"] == 1 and not assisted(e))
            preds["oracle"].append(e.get("truth", {}).get("pCorrect"))
            preds["prior-only"].append(sigmoid(5.0 - prior[pid]))
            preds["success-rate"].append((s + 1) / (n + 2))
            preds["irt-prior"].append(sigmoid(theta_p - prior[pid]))
            preds["irt-calibrated"].append(sigmoid(theta_c - calibrated[pid]))
            w = tags[pid]
            total = sum(w.values())
            term = sum(logit(mastery.mastery_or_prior(c)) * v for c, v in w.items()) / total
            preds["irt+mastery"].append(sigmoid(theta_c - calibrated[pid] + 0.5 * term))
            if dkt:
                preds["dkt"].append(dkt.predict(state, encoder.weights[pid], encoder.difficulty[pid],
                                                pid in seen, pid))
            # observe
            s, n = s + int(y), n + 1
            if pid not in seen:  # backend: one Rasch response (the first attempt) per problem
                hist_prior.append((prior[pid], y))
                hist_cal.append((calibrated[pid], y))
                theta_p = estimate_ability(hist_prior[-WINDOW:])[0]
                theta_c = estimate_ability(hist_cal[-WINDOW:])[0]
            mastery.update(w, y)
            p_irt = preds["irt-calibrated"][-1]
            top = max(w.values())
            for c, v in w.items():
                res_num[c] = res_num.get(c, 0.0) + (v / top) * ((1.0 if y else 0.0) - p_irt)
                res_den[c] = res_den.get(c, 0.0) + (v / top)
            if dkt:
                state = dkt.step(state, encoder.input_vector(pid, int(y), assisted(e)))
            seen.add(pid)
        dkt_final = None
        if dkt:
            st = dkt.initial_state()
            for e in seq[window_start:]:
                st = dkt.step(st, encoder.input_vector(e["problemId"], int(e["correct"]), assisted(e)))
            dkt_final = dict(zip(encoder.concepts, dkt.mastery(st)))
            logits = dict(zip(encoder.concepts, [float(v) for v in dkt.concept_logits(st)]))
        final_state[lid] = {"beta": {c: m.mastery for c, m in mastery.concepts.items()},
                            "evidence": {c: m.evidence for c, m in mastery.concepts.items()},
                            "dkt": dkt_final,
                            "dktLogits": logits if dkt else None,
                            "residual": {c: res_num[c] / (res_den[c] + 1.0) for c in res_num}}
    return labels, first_mask, preds, final_state


def summarise(labels, first_mask, preds):
    out = {}
    for name, p in preds.items():
        if not p or any(v is None for v in p):
            continue
        row = metrics.summary(labels, p)
        y1 = [y for y, m in zip(labels, first_mask) if m]
        p1 = [v for v, m in zip(p, first_mask) if m]
        row["aucFirstUnassisted"] = metrics.auc(y1, p1)
        out[name] = {k: round(v, 4) if isinstance(v, float) else v for k, v in row.items()}
    return out


def mastery_recovery(final_state, truths, baseline):
    """Spearman/MAE of estimated vs true final mastery, and weakest-concept detection.

    Hit rate: fraction of learners whose estimated weakest PRACTICED concept is one of
    their true weak concepts. "truth" ranks by true mastery (ceiling), "random" is chance.
    "dkt-relative" ranks by concept logit minus the training-population baseline.
    """
    rows = {"beta": ([], []), "dkt": ([], [])}
    hits = {"truth": 0, "beta": 0, "irt-residual": 0, "dkt": 0, "dkt-relative": 0, "random": 0.0}
    n_learners = 0
    for lid, st in final_state.items():
        truth = truths.get(lid)
        if not truth:
            continue
        practiced = [c for c, k in st["evidence"].items() if k >= 2]
        for c in practiced:
            rows["beta"][0].append(truth["finalMastery"][c])
            rows["beta"][1].append(st["beta"][c])
            if st["dkt"]:
                rows["dkt"][0].append(truth["finalMastery"][c])
                rows["dkt"][1].append(st["dkt"][c])
        weak = set(truth["weakConcepts"]) & set(practiced)
        if len(practiced) >= 3 and weak:
            n_learners += 1
            hits["truth"] += min(practiced, key=lambda c: truth["finalMastery"][c]) in weak
            hits["beta"] += min(practiced, key=lambda c: st["beta"][c]) in weak
            hits["irt-residual"] += min(practiced, key=lambda c: st["residual"][c]) in weak
            if st["dkt"]:
                hits["dkt"] += min(practiced, key=lambda c: st["dkt"][c]) in weak
                hits["dkt-relative"] += min(
                    practiced, key=lambda c: st["dktLogits"][c] - baseline.get(c, 0.0)) in weak
            hits["random"] += len(weak) / len(practiced)
    out = {}
    for name, (t, e) in rows.items():
        if t:
            out[name] = {"pairs": len(t), "spearman": round(metrics.spearman(t, e), 4),
                         "mae": round(metrics.mae(t, e), 4)}
    if n_learners:
        out["weakestConceptHitRate"] = {**{k: round(v / n_learners, 4) for k, v in hits.items()},
                                        "learners": n_learners}
    return out


def item_recovery(items_truth, prior, calibrated, train_counts):
    truth = {i["problemId"]: i["trueDifficulty0to10"] for i in items_truth}
    ids = [p for p in truth if train_counts.get(p, 0) >= 20]
    t = [truth[p] for p in ids]
    return {"items": len(ids), "minTrainResponses": 20,
            "spearmanPrior": round(metrics.spearman(t, [prior[p] for p in ids]), 4),
            "spearmanCalibrated": round(metrics.spearman(t, [calibrated[p] for p in ids]), 4)}


def plots(out_dir, labels, preds, history_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    made = []
    fig, ax = plt.subplots(figsize=(5.5, 5))
    ax.plot([0, 1], [0, 1], "k--", lw=1)
    for name in ("oracle", "irt-calibrated", "irt+mastery", "dkt"):
        if preds.get(name) and None not in preds[name]:
            b = metrics.calibration_bins(labels, preds[name], 10)
            ax.plot([x for x, _, _ in b], [y for _, y, _ in b], marker="o", label=name)
    ax.set_xlabel("predicted P(correct)")
    ax.set_ylabel("observed success rate")
    ax.set_title("Calibration on held-out learners")
    ax.legend()
    fig.tight_layout()
    fig.savefig(fig_dir / "calibration.png", dpi=120)
    made.append("figures/calibration.png")
    plt.close(fig)
    hp = pathlib.Path(history_path)
    if hp.exists():
        hist = json.loads(hp.read_text())
        fig, ax = plt.subplots(1, 2, figsize=(10, 4))
        ax[0].plot([h["epoch"] for h in hist], [h["trainLoss"] for h in hist])
        ax[0].set_title("DKT training loss")
        ax[1].plot([h["epoch"] for h in hist], [h["val_auc"] for h in hist], color="tab:green")
        ax[1].set_title("DKT validation AUC")
        for a in ax:
            a.set_xlabel("epoch")
        fig.tight_layout()
        fig.savefig(fig_dir / "dkt_training.png", dpi=120)
        made.append("figures/dkt_training.png")
        plt.close(fig)
    return made


def main():
    args = parse_args()
    catalog = json.loads(pathlib.Path(args.catalog).read_text())
    concepts = [c["code"] for c in catalog["concepts"]]
    encoder = Encoder(concepts, catalog["problems"])
    prior = {p["problemId"]: float(p["priorDifficulty"]) for p in catalog["problems"]}
    tags = {p["problemId"]: {c: float(w) for c, w in p["conceptWeights"].items()} for p in catalog["problems"]}
    seqs = ev.load_sequences(pathlib.Path(args.events), known_problems=set(prior))
    tr, _, te = ev.split_learners(seqs)
    if args.max_test_learners:
        te = te[:args.max_test_learners]
    print(f"learners={len(seqs)} train={len(tr)} test={len(te)}")

    calibrated = calibrate([seqs[l] for l in tr], prior)
    train_counts = {}
    for l in tr:
        for e in seqs[l]:
            train_counts[e["problemId"]] = train_counts.get(e["problemId"], 0) + 1

    dkt, doc = load_dkt(args.dkt)
    if dkt is None:
        print("DKT:", doc)
    labels, first_mask, preds, final_state = run_online({l: seqs[l] for l in te}, encoder, prior,
                                                        calibrated, dkt, tags)
    result = {
        "_note": "Held-out learners; synthetic data measures behaviour under simulator assumptions only.",
        "events": pathlib.Path(args.events).name,
        "testLearners": len(te), "testEvents": len(labels),
        "dktModel": ({"trainedOn": doc.get("trainedOn"), "createdAt": doc.get("createdAt"),
                      "hiddenSize": doc.get("hiddenSize")} if dkt else doc),
        "prediction": summarise(labels, first_mask, preds),
    }
    lp, ip = pathlib.Path(args.learners), pathlib.Path(args.items)
    if lp.exists():
        truths = {r["learnerId"]: r for r in ev.read_jsonl(lp)}
        result["masteryRecovery"] = mastery_recovery(final_state, truths,
                                                     (doc or {}).get("conceptBaseline", {}) if dkt else {})
    if ip.exists():
        result["itemRecovery"] = item_recovery(json.loads(ip.read_text())["items"], prior, calibrated, train_counts)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result["figures"] = plots(out, labels, preds, pathlib.Path(args.dkt).parent / "history.json")
    (out / "kt-evaluation.json").write_text(json.dumps(result, indent=2))
    (out / "kt-evaluation.md").write_text(markdown(result))
    print(json.dumps(result, indent=2))


def markdown(r):
    lines = ["# Knowledge-tracing evaluation (held-out learners)", "",
             "> " + r["_note"], "",
             f"Data: `{r['events']}` — {r['testLearners']} test learners, {r['testEvents']} test events.", "",
             "## Next-attempt prediction", "",
             "| model | AUC | AUC (first, unassisted) | log loss | Brier | ECE | accuracy |",
             "|---|---|---|---|---|---|---|"]
    for name, m in r["prediction"].items():
        lines.append(f"| {name} | {m['auc']:.4f} | {m['aucFirstUnassisted']:.4f} | {m['logLoss']:.4f} | "
                     f"{m['brier']:.4f} | {m['ece']:.4f} | {m['accuracy']:.4f} |")
    if "masteryRecovery" in r:
        mr = r["masteryRecovery"]
        lines += ["", "## Concept-mastery recovery (vs simulator ground truth)", "",
                  "| estimator | pairs | Spearman | MAE |", "|---|---|---|---|"]
        for k in ("beta", "dkt"):
            if k in mr:
                lines.append(f"| {k} | {mr[k]['pairs']} | {mr[k]['spearman']:.4f} | {mr[k]['mae']:.4f} |")
        hr = mr.get("weakestConceptHitRate", {})
        if hr:
            lines += ["", f"Weakest-concept hit rate over {hr['learners']} learners (estimated weakest "
                          "practiced concept is a true weak concept): "
                      + ", ".join(f"{k} {v}" for k, v in hr.items() if k != "learners") + "."]
    if "itemRecovery" in r:
        ir = r["itemRecovery"]
        lines += ["", "## Item-difficulty recovery", "",
                  f"Spearman with true difficulty over {ir['items']} items (>= {ir['minTrainResponses']} "
                  f"train responses): catalog prior {ir['spearmanPrior']}, calibrated {ir['spearmanCalibrated']}."]
    for f in r.get("figures", []):
        lines += ["", f"![{f}]({f})"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()

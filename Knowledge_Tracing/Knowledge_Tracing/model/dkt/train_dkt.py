#!/usr/bin/env python3
"""Train, evaluate and export the kt-dkt-v1 Deep Knowledge Tracing model.

Usage:
  python3 model/dkt/train_dkt.py --events data/synthetic/v2/events.jsonl.gz --allow-synthetic
  python3 model/dkt/train_dkt.py --events exports/events.jsonl.gz --catalog exports/catalog.json --real

Requires PyTorch (see model/requirements-dkt.txt, or use notebooks/DKT_train_and_visualize.ipynb
on Google Colab). Without torch, or with too little / unflagged synthetic data, the script
prints the availability report with status UNAVAILABLE and exits 2.

Honesty rules (docs/dkt.md):
  * a model trained on SYNTHETIC data only learns the simulator; it is exported with
    "trainedOn": "synthetic" and the backend reports that in /models/dkt;
  * training on synthetic data therefore requires the explicit --allow-synthetic flag;
  * splits are by LEARNER (70/15/15, hash-based) so no learner leaks across splits;
  * early stopping uses validation AUC only; the test split is touched once.

Outputs (--out, default model/artifacts/dkt):
  dkt-model.json   kt-dkt-v1 weights for the backend (knowledge-tracer.dkt.model-path)
  dkt-model.pt     PyTorch state dict
  history.json     per-epoch train loss / validation metrics
  metrics.json     validation + test metrics
"""
import argparse
import json
import pathlib
import random
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "model"))

from kt import dkt as dkt_availability  # noqa: E402
from kt import events as ev  # noqa: E402
from kt import metrics  # noqa: E402

from data import encode_sequence, windows  # noqa: E402
from features import Encoder  # noqa: E402


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(ROOT / "data" / "synthetic" / "v2" / "events.jsonl.gz"))
    ap.add_argument("--catalog", default=str(ROOT / "data" / "seed" / "catalog_1000.json"))
    ap.add_argument("--out", default=str(ROOT / "model" / "artifacts" / "dkt"))
    ap.add_argument("--real", action="store_true", help="dataset is real, consented learner data")
    ap.add_argument("--allow-synthetic", action="store_true", help="explicitly allow training on synthetic data")
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--dropout", type=float, default=0.2)
    ap.add_argument("--max-len", type=int, default=200)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=1e-5)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--patience", type=int, default=5)
    ap.add_argument("--lambda-r", type=float, default=0.1)
    ap.add_argument("--lambda-w1", type=float, default=0.003)
    ap.add_argument("--lambda-w2", type=float, default=3.0)
    ap.add_argument("--lambda-item", type=float, default=0.05, help="L2 shrinkage of per-problem offsets")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--device", default="auto")
    return ap.parse_args()


def load(args):
    catalog = json.loads(pathlib.Path(args.catalog).read_text())
    concepts = [c["code"] for c in catalog["concepts"]]
    encoder = Encoder(concepts, catalog["problems"])
    by_learner = ev.load_sequences(pathlib.Path(args.events), known_problems=set(encoder.weights))
    return catalog, encoder, by_learner


def to_batches(torch, seqs, batch_size, shuffle, rng, device):
    order = list(range(len(seqs)))
    if shuffle:
        rng.shuffle(order)
    for s in range(0, len(order), batch_size):
        chunk = [seqs[i] for i in order[s:s + batch_size]]
        T = max(len(c["y"]) for c in chunk)
        B = len(chunk)
        D = len(chunk[0]["x"][0])
        K = len(chunk[0]["w"][0])
        x = torch.zeros(B, T, D)
        w = torch.zeros(B, T, K)
        d = torch.zeros(B, T)
        r = torch.zeros(B, T)
        q = torch.zeros(B, T, dtype=torch.long)
        y = torch.zeros(B, T)
        m = torch.zeros(B, T)
        for b, c in enumerate(chunk):
            n = len(c["y"])
            x[b, :n] = torch.tensor(c["x"])
            w[b, :n] = torch.tensor(c["w"])
            d[b, :n] = torch.tensor(c["d"])
            r[b, :n] = torch.tensor(c["r"])
            q[b, :n] = torch.tensor(c["q"])
            y[b, :n] = torch.tensor(c["y"])
            m[b, :n] = 1.0
        yield {k: v.to(device) for k, v in dict(x=x, w=w, d=d, r=r, q=q, y=y, mask=m).items()}


def evaluate(torch, model, seqs, args, device):
    from dkt_model import loss_fn
    model.eval()
    ys, ps = [], []
    with torch.no_grad():
        for batch in to_batches(torch, seqs, 256, False, None, device):
            _, _, p = loss_fn(model, batch, 0.0, 0.0, 0.0)
            mask = batch["mask"].bool()
            ys.extend(batch["y"][mask].cpu().tolist())
            ps.extend(p[mask].cpu().tolist())
    return metrics.summary([bool(v) for v in ys], ps)


def concept_baseline(torch, model, encoder, by_learner, ids, item_index, max_len, device):
    """Mean final concept logit over TRAIN learners, per concept.

    Raw concept logits mix learner skill with how hard a concept is for everyone; the
    backend ranks a learner's weak concepts by (logit - baseline) instead (docs/dkt.md).
    """
    model.eval()
    sums = [0.0] * encoder.k
    n = 0
    with torch.no_grad():
        for lid in ids:
            seq = encode_sequence(encoder, by_learner[lid], item_index)
            x = torch.tensor([seq["x"][-max_len:]], device=device)
            y = model.concept_logits(x)[0, -1].cpu().tolist()
            sums = [a + b for a, b in zip(sums, y)]
            n += 1
    return {c: round(s / max(1, n), 5) for c, s in zip(encoder.concepts, sums)}


def main():
    args = parse_args()
    catalog, encoder, by_learner = load(args)
    n_events = sum(len(s) for s in by_learner.values())
    print(f"learners={len(by_learner)} events={n_events} concepts={encoder.k} input={encoder.input_size}")

    synthetic = not args.real
    report = {
        "torchAvailable": dkt_availability.torch_available(),
        "learners": len(by_learner), "interactions": n_events,
        "minLearners": dkt_availability.MIN_LEARNERS, "minInteractions": dkt_availability.MIN_INTERACTIONS,
        "synthetic": synthetic,
    }
    problems = []
    if not report["torchAvailable"]:
        problems.append("PyTorch is not installed (pip install -r model/requirements-dkt.txt, or use Colab)")
    if len(by_learner) < dkt_availability.MIN_LEARNERS or n_events < dkt_availability.MIN_INTERACTIONS:
        problems.append("dataset below the minimum size for a meaningful DKT")
    if synthetic and not args.allow_synthetic:
        problems.append("data is SYNTHETIC: pass --allow-synthetic to train a simulator-only model")
    if problems:
        print(json.dumps({**report, "status": "UNAVAILABLE", "reasons": problems}, indent=2))
        return 2

    import torch
    from dkt_model import DKT, export, loss_fn

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available()
                          else ("cpu" if args.device == "auto" else args.device))

    tr_ids, va_ids, te_ids = ev.split_learners(by_learner)
    items = [p["problemId"] for p in catalog["problems"]]
    item_index = {pid: i for i, pid in enumerate(items)}
    split = {}
    for name, ids in (("train", tr_ids), ("val", va_ids), ("test", te_ids)):
        split[name] = [w for lid in ids
                       for w in windows(encode_sequence(encoder, by_learner[lid], item_index), args.max_len)]
    print({k: len(v) for k, v in split.items()}, "windows")

    model = DKT(encoder.k, len(items), args.hidden, args.dropout).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    rng = random.Random(args.seed)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    best_auc, best_state, bad, history = -1.0, None, 0, []

    for epoch in range(1, args.epochs + 1):
        model.train()
        t0, total, steps = time.time(), 0.0, 0
        for batch in to_batches(torch, split["train"], args.batch, True, rng, device):
            loss, _, _ = loss_fn(model, batch, args.lambda_r, args.lambda_w1, args.lambda_w2, args.lambda_item)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total += float(loss.detach())
            steps += 1
        val = evaluate(torch, model, split["val"], args, device)
        history.append({"epoch": epoch, "trainLoss": total / max(1, steps), **{f"val_{k}": v for k, v in val.items()},
                        "seconds": round(time.time() - t0, 1)})
        print(f"epoch {epoch:2d} loss={total / max(1, steps):.4f} val_auc={val['auc']:.4f} "
              f"val_logloss={val['logLoss']:.4f} ({time.time() - t0:.1f}s)")
        if val["auc"] > best_auc + 1e-4:
            best_auc, bad = val["auc"], 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= args.patience:
                print(f"early stop (no val AUC gain for {args.patience} epochs)")
                break

    model.load_state_dict(best_state)
    result = {"val": evaluate(torch, model, split["val"], args, device),
              "test": evaluate(torch, model, split["test"], args, device)}
    print("test:", json.dumps(result["test"]))
    baseline = concept_baseline(torch, model, encoder, by_learner, tr_ids, item_index, args.max_len, device)

    meta = {
        "trainedOn": "real" if args.real else "synthetic",
        "dataset": {"events": pathlib.Path(args.events).name, "learners": len(by_learner),
                    "interactions": n_events, "split": {k: len(v) for k, v in
                                                        (("train", tr_ids), ("val", va_ids), ("test", te_ids))}},
        "maxSequenceLength": args.max_len,
        "conceptBaseline": baseline,
        "hyperparameters": {k: getattr(args, k) for k in ("hidden", "dropout", "batch", "lr", "weight_decay",
                                                            "lambda_r", "lambda_w1", "lambda_w2", "lambda_item",
                                                            "seed")},
        "metrics": {k: {m: round(v, 4) for m, v in r.items() if isinstance(v, float)} for k, r in result.items()},
    }
    export(model.cpu(), encoder.concepts, items, out / "dkt-model.json", meta)
    torch.save(best_state, out / "dkt-model.pt")
    (out / "history.json").write_text(json.dumps(history, indent=1))
    (out / "metrics.json").write_text(json.dumps({**meta["metrics"], "trainedOn": meta["trainedOn"]}, indent=2))
    print(f"exported {out / 'dkt-model.json'}")


if __name__ == "__main__":
    sys.exit(main())

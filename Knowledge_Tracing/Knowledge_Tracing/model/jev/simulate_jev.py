#!/usr/bin/env python3
"""Compare Jev providers in the offline recommendation loop (synthetic learners).

  python3 model/jev/simulate_jev.py [--learners 300] [--steps 40] [--seed 7]

Writes model/reports/jev-simulation.{json,md} (+ figures/jev_*.png when matplotlib is
available). Synthetic results measure behaviour under simulator assumptions only.
"""
import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))

from kt.catalog import Catalog  # noqa: E402
from kt.simulator import World  # noqa: E402
from jev.linucb import FEATURE_NAMES  # noqa: E402
from jev import tutor  # noqa: E402
from jev.tutor import Bandit, Deterministic, RandomCandidate, run  # noqa: E402


def providers():
    return [RandomCandidate(), Deterministic(), Bandit(alpha=0.5, guarded=True), Bandit(alpha=0.5, guarded=False)]


def moving_average(xs, k):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= k:
            s -= xs[i - k]
        out.append(s / min(i + 1, k))
    return out


def plots(out_dir, results, bandits):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    files = []

    fig, ax = plt.subplots(figsize=(8, 4))
    for r in results:
        ax.plot(moving_average(r["rewardCurve"], 500), label=r["provider"])
    ax.set(xlabel="decision #", ylabel="reward (moving avg, 500)", title="Jev providers: online reward")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(fig_dir / "jev_reward.png", dpi=120)
    files.append("figures/jev_reward.png")

    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8))
    names = [r["provider"] for r in results]
    for ax, key, title in zip(axes, ["trueMasteryGain", "weakConceptGain", "productiveZoneRate"],
                              ["true mastery gain", "weak-concept gain", "in productive zone"]):
        ax.bar(range(len(results)), [r[key]["mean"] for r in results],
               yerr=[r[key]["ci95"] for r in results], capsize=3)
        ax.set_xticks(range(len(results)))
        ax.set_xticklabels(names, rotation=30, ha="right", fontsize=7)
        ax.set_title(title)
    fig.tight_layout()
    fig.savefig(fig_dir / "jev_outcomes.png", dpi=120)
    files.append("figures/jev_outcomes.png")

    b = next((x for x in bandits if not x.guarded), bandits[0])
    arms = sorted(b.model.arms)
    fig, ax = plt.subplots(figsize=(9, 3.8))
    width = 0.8 / max(1, len(arms))
    for i, arm in enumerate(arms):
        ax.bar([j + i * width for j in range(len(FEATURE_NAMES))], b.model.arms[arm].theta(), width, label=arm)
    ax.set_xticks([j + 0.4 for j in range(len(FEATURE_NAMES))])
    ax.set_xticklabels(FEATURE_NAMES, rotation=30, ha="right", fontsize=8)
    ax.set_title(f"learned LinUCB weights ({b.name})")
    ax.legend(fontsize=6, ncol=2)
    fig.tight_layout()
    fig.savefig(fig_dir / "jev_theta.png", dpi=120)
    files.append("figures/jev_theta.png")
    plt.close("all")
    return files


def markdown(report):
    lines = ["# Jev provider simulation (synthetic learners)", "",
             "> Synthetic data measures behaviour under simulator assumptions only.", "",
             f"{report['learners']} learners x {report['steps']} decisions per provider; identical learners "
             f"for every provider. Reward = {report['qualityWeight']} quality + {1 - report['qualityWeight']:.1f} "
             "growth of the first-attempt IRT ability (backend defaults).", "",
             "| provider | mean reward | true mastery gain | weak-concept gain | first-attempt solve | productive zone |",
             "|---|---|---|---|---|---|"]
    f = lambda d: f"{d['mean']:.4f} ± {d['ci95']:.4f}"  # noqa: E731
    for r in report["results"]:
        lines.append(f"| {r['provider']} | {r['meanReward']:.4f} | {f(r['trueMasteryGain'])} | "
                     f"{f(r['weakConceptGain'])} | {f(r['firstAttemptSolveRate'])} | {f(r['productiveZoneRate'])} |")
    lines += ["", "Action mix:", ""]
    for r in report["results"]:
        lines.append(f"- {r['provider']}: " + ", ".join(f"{k} {v}" for k, v in r["actions"].items()))
    lines += ["", "Reading: the reward is what the bandit optimises; true mastery gain is what matters "
              "pedagogically and is only observable in simulation. A provider that raises reward "
              "but not mastery is exploiting the reward definition (docs/jev.md, limitations).", ""]
    lines += [f"![{p}]({p})" for p in report.get("figures", [])]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default=str(ROOT / "data" / "seed" / "catalog_1000.json"))
    ap.add_argument("--learners", type=int, default=300)
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(ROOT / "model" / "reports"))
    args = ap.parse_args()
    catalog = Catalog.load(pathlib.Path(args.catalog))
    results, bandits = [], []
    for prov in providers():
        t0 = time.time()
        world = World(catalog, seed=20260929)
        r = run(world, prov, args.learners, args.steps, args.seed)
        print(f"{r['provider']:28s} reward {r['meanReward']:.4f}  mastery gain {r['trueMasteryGain']['mean']:.4f}"
              f"  weak gain {r['weakConceptGain']['mean']:.4f}  ({time.time() - t0:.1f}s)")
        results.append(r)
        if isinstance(prov, Bandit):
            bandits.append(prov)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"learners": args.learners, "steps": args.steps, "seed": args.seed,
              "qualityWeight": tutor.QUALITY_WEIGHT, "results": results}
    report["figures"] = plots(out, results, bandits)
    slim = dict(report, results=[{k: v for k, v in r.items() if k != "rewardCurve"} for r in results])
    for b in bandits:
        slim.setdefault("theta", {})[b.name] = {a: [round(v, 4) for v in arm.theta()] for a, arm in b.model.arms.items()}
    (out / "jev-simulation.json").write_text(json.dumps(slim, indent=1) + "\n")
    (out / "jev-simulation.md").write_text(markdown(slim))
    print(f"wrote {out / 'jev-simulation.md'}")


if __name__ == "__main__":
    main()

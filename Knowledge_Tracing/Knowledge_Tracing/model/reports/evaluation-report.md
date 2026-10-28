# Offline evaluation report

> **SYNTHETIC / DEVELOPMENT DATA — not evidence of real learning outcomes.**
>
> Generated 2026-09-29 23:01:08 by `model/evaluation/run_evaluation.py` (seed 7). Re-run to reproduce; numbers change with the seed.

## 1. Setup

- Problem pool: 28 seeded LeetCode problems, 15 concepts (`data/seed/catalog.json`).
- Train learners: 300, test learners: 150, 20 random-order attempts each (random order avoids selection bias in the eval set).
- Item difficulties calibrated on the TRAIN split only; predictor weights fitted on TRAIN only.
- Simulator: per-concept skills, one weak concept per learner, prerequisite penalty, noisy true difficulty vs static label, practice-driven learning (see `model/kt/synthetic.py`).

## 2. Item difficulty recovery (TRAIN calibration vs simulator truth)

| Estimate | MAE | RMSE | Spearman |
|---|---|---|---|
| static label prior | 0.603 | 0.751 | 0.823 |
| IRT calibrated | 0.251 | 0.309 | 0.939 |

## 3. Ability recovery (TEST learners, after 20 responses)

- Spearman(final estimate, true initial ability) = **0.950**; MAE = 0.754 on the 0-10 scale; mean reported uncertainty = 0.671.
- Learners also learn during the sequence, so part of the error is real drift, not estimator error.

## 3b. Gap detection (TEST learners, mastery layer after 20 random-order responses)

- A gap (weakest concept, >=2 observations, mastery < 0.45) was flagged for 102/150 learners.
- When flagged, it was the simulator's designated weak concept 0.147 of the time (chance 0.067).
- Designated weak concept among the 3 lowest-mastery concepts: 0.307 (chance 0.200).
- Low-ability learners fail most concepts, so their weakest concept is often not the designated one.

## 4. Next-response prediction (TEST split, online: predict before observing)

| Model | Accuracy | AUC | Log loss | Brier | ECE |
|---|---|---|---|---|---|
| static-difficulty | 0.637 | 0.631 | 0.691 | 0.238 | 0.106 |
| success-rate | 0.784 | 0.840 | 0.488 | 0.158 | 0.047 |
| irt-only | 0.811 | 0.890 | 0.407 | 0.130 | 0.014 |
| irt+graph | 0.811 | 0.890 | 0.407 | 0.130 | 0.014 |
| irt+knowledge-tracing | 0.815 | 0.891 | 0.405 | 0.129 | 0.015 |
| full (irt+kt+graph) | 0.815 | 0.891 | 0.405 | 0.129 | 0.015 |
| dkt | UNAVAILABLE | – | – | – | – |

Fitted weights (train): mastery term w_m = 0.25, prerequisite term w_g = 0.0 (graph only) / 0.0 (with mastery).

## 5. Recommendation policy simulation + ablation

150 fresh learners (abilities centred at 2 / 5 / 8), 20 recommended problems each, identical random streams per learner across policies. Mean ± 95% CI.

| Metric | better | random | static-difficulty | success-rate | irt-only | irt+localization | irt+mastery | full-system | full+retry-guard |
|---|---|---|---|---|---|---|---|---|---|
| next-problem success rate | context | 0.577 ± 0.05 | 0.638 ± 0.05 | 0.580 ± 0.05 | 0.623 ± 0.05 | 0.626 ± 0.05 | 0.621 ± 0.04 | 0.623 ± 0.04 | 0.617 ± 0.05 |
| mean |true P - 0.65| (targeting error) | lower | 0.338 ± 0.02 | 0.313 ± 0.02 | 0.318 ± 0.02 | 0.294 ± 0.02 | 0.301 ± 0.02 | 0.289 ± 0.02 | 0.288 ± 0.01 | 0.304 ± 0.02 |
| share of attempts with true P > 0.9 | lower | 0.333 ± 0.06 | 0.390 ± 0.06 | 0.305 ± 0.05 | 0.321 ± 0.05 | 0.323 ± 0.05 | 0.310 ± 0.05 | 0.309 ± 0.05 | 0.325 ± 0.05 |
| share of attempts with true P < 0.2 | lower | 0.267 ± 0.05 | 0.189 ± 0.05 | 0.232 ± 0.04 | 0.174 ± 0.04 | 0.186 ± 0.04 | 0.168 ± 0.04 | 0.166 ± 0.04 | 0.194 ± 0.04 |
| 3-failure streaks per learner | lower | 0.887 ± 0.17 | 0.807 ± 0.17 | 0.980 ± 0.18 | 0.787 ± 0.19 | 0.813 ± 0.19 | 0.773 ± 0.16 | 0.760 ± 0.16 | 0.813 ± 0.18 |
| mean simulated skill gain per concept | higher | 0.471 ± 0.04 | 0.495 ± 0.04 | 0.528 ± 0.03 | 0.567 ± 0.04 | 0.556 ± 0.04 | 0.579 ± 0.04 | 0.591 ± 0.04 | 0.550 ± 0.04 |
| simulated gain on the weak concept | higher | 0.484 ± 0.08 | 0.507 ± 0.09 | 0.492 ± 0.08 | 0.557 ± 0.09 | 0.565 ± 0.08 | 0.794 ± 0.16 | 0.749 ± 0.14 | 0.559 ± 0.08 |
| |final ability estimate - true ability| | lower | 0.664 ± 0.08 | 0.718 ± 0.08 | 0.624 ± 0.07 | 0.701 ± 0.07 | 0.734 ± 0.08 | 0.782 ± 0.08 | 0.776 ± 0.08 | 0.708 ± 0.07 |

## 6. DKT

Status: **UNAVAILABLE**.

- PyTorch is not installed in this environment
- only 300 learner sequences (< 1000)
- only 6000 interactions (< 50000)
- data is SYNTHETIC; a DKT trained on it would only learn the simulator

## 7. How to read this

- These numbers validate that the implementation behaves as designed **under the simulator's assumptions**. They are not evidence that the system improves real learning.
- Learning-gain metrics depend entirely on the assumed learning dynamics (gain ∝ p(1-p), i.e. productive struggle). A different assumption can reorder policies.
- The system policies reuse the difficulty scale the simulator was calibrated against; on real data item calibration would be noisier.
- Real validation requires consented real learner data and a held-out evaluation (docs/evaluation.md).

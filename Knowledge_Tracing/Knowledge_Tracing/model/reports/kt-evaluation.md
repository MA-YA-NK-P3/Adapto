# Knowledge-tracing evaluation (held-out learners)

> Held-out learners; synthetic data measures behaviour under simulator assumptions only.

Data: `events.jsonl.gz` — 384 test learners, 15534 test events.

## Next-attempt prediction

| model | AUC | AUC (first, unassisted) | log loss | Brier | ECE | accuracy |
|---|---|---|---|---|---|---|
| oracle | 0.7792 | 0.7699 | 0.4955 | 0.1623 | 0.0096 | 0.7678 |
| prior-only | 0.6876 | 0.6816 | 0.6107 | 0.1986 | 0.0961 | 0.7231 |
| success-rate | 0.6083 | 0.6056 | 0.6120 | 0.2107 | 0.0549 | 0.6853 |
| irt-prior | 0.7231 | 0.7166 | 0.5768 | 0.1909 | 0.0840 | 0.7198 |
| irt-calibrated | 0.7178 | 0.7113 | 0.5617 | 0.1867 | 0.0624 | 0.7285 |
| irt+mastery | 0.7171 | 0.7114 | 0.5741 | 0.1893 | 0.0769 | 0.7289 |
| dkt | 0.7309 | 0.7275 | 0.5362 | 0.1786 | 0.0164 | 0.7406 |

## Concept-mastery recovery (vs simulator ground truth)

| estimator | pairs | Spearman | MAE |
|---|---|---|---|
| beta | 5329 | 0.4512 | 0.1951 |
| dkt | 5329 | 0.6578 | 0.1391 |

Weakest-concept hit rate over 253 learners (estimated weakest practiced concept is a true weak concept): truth 0.8458, beta 0.166, irt-residual 0.1739, dkt 0.083, dkt-relative 0.0949, random 0.0966.

## Item-difficulty recovery

Spearman with true difficulty over 796 items (>= 20 train responses): catalog prior 0.9102, calibrated 0.9022.

![figures/calibration.png](figures/calibration.png)

![figures/dkt_training.png](figures/dkt_training.png)

# Jev provider simulation (synthetic learners)

> Synthetic data measures behaviour under simulator assumptions only.

300 learners x 40 decisions per provider; identical learners for every provider. Reward = 0.0 quality + 1.0 growth of the first-attempt IRT ability (backend defaults).

| provider | mean reward | true mastery gain | weak-concept gain | first-attempt solve | productive zone |
|---|---|---|---|---|---|
| random-candidate | 0.4898 | 0.0330 ± 0.0013 | 0.0292 ± 0.0046 | 0.3508 ± 0.0077 | 0.4797 ± 0.0133 |
| deterministic | 0.4865 | 0.0335 ± 0.0013 | 0.0319 ± 0.0049 | 0.6297 ± 0.0113 | 0.5273 ± 0.0171 |
| linucb-guarded(a=0.5) | 0.5014 | 0.0331 ± 0.0013 | 0.0357 ± 0.0068 | 0.3710 ± 0.0114 | 0.4165 ± 0.0156 |
| linucb(a=0.5) | 0.5004 | 0.0321 ± 0.0013 | 0.0295 ± 0.0060 | 0.1891 ± 0.0093 | 0.3670 ± 0.0139 |

Action mix:

- random-candidate: CALIBRATION_PROBLEM 1001, EASIER_PROBLEM 1699, HARDER_PROBLEM 791, PREREQUISITE_REVIEW 222, RETRY_PROBLEM 3038, SAME_LEVEL_PROBLEM 3212, SPACED_REVIEW 2037
- deterministic: CALIBRATION_PROBLEM 1433, EASIER_PROBLEM 3884, PREREQUISITE_REVIEW 782, SAME_LEVEL_PROBLEM 5901
- linucb-guarded(a=0.5): CALIBRATION_PROBLEM 4, EASIER_PROBLEM 3608, HARDER_PROBLEM 2053, PREREQUISITE_REVIEW 489, RETRY_PROBLEM 2696, SAME_LEVEL_PROBLEM 1169, SPACED_REVIEW 1981
- linucb(a=0.5): CALIBRATION_PROBLEM 8, EASIER_PROBLEM 1013, HARDER_PROBLEM 1377, PREREQUISITE_REVIEW 195, RETRY_PROBLEM 4642, SAME_LEVEL_PROBLEM 1422, SPACED_REVIEW 3343

Reading: the reward is what the bandit optimises; true mastery gain is what matters pedagogically and is only observable in simulation. A provider that raises reward but not mastery is exploiting the reward definition (docs/jev.md, limitations).

![figures/jev_reward.png](figures/jev_reward.png)
![figures/jev_outcomes.png](figures/jev_outcomes.png)
![figures/jev_theta.png](figures/jev_theta.png)

# Data

| path | what | origin |
|---|---|---|
| `data/leetcode/leetcode_all_metadata.json` | all 4068 LeetCode problems: id, slug, title, difficulty, acceptance, likes, topic tags, paid flag | public mirror `noworneverev/leetcode-api` (GitHub raw) |
| `data/leetcode/leetcode_problems_1000.{json,csv}` | the 1000 selected problems with concepts and priors | `scripts/build_leetcode_catalog.py` |
| `data/leetcode/tag_concept_map.json` | topic-tag → concept rules (tiers A 1.0 / B 0.7 / C 0.5) | same script |
| `data/seed/catalog.json` | 28 curated seed problems (the V3 dev data) | `scripts/export_catalog.py` |
| `data/seed/catalog_1000.json` | catalog used by the simulator, DKT and notebooks | builder |
| `backend/.../db/migration/V5__leetcode_catalog_1000.sql` | the same 1000 problems + concepts in PostgreSQL | builder |
| `data/synthetic/v2/` | synthetic benchmark (below) | `scripts/generate_synthetic_dataset.py` |
| `data/real/` | pseudonymised exports from a running backend (not committed) | `scripts/export_events.py` |

## LeetCode catalog (1000 problems)

Selection: free *Algorithms* problems (3117 of 4068) with at least one mapped concept; the
28 curated seed problems plus the most-liked remainder. Problem keys are `LC<frontendId>`.
Each problem carries `difficulty`, `acceptanceRate`, `topicTags`, `concepts`
(concept → weight, strongest = 1.0 except curated LC53 at 0.8, at most 4) and `priorDifficulty` ([irt.md](irt.md)).
Seed problems keep their curated V3 weights (`conceptSource: "curated (V3)"`); all others are
`generated`. The extension's `mapTags` (`extension/src/lib/extract.js`) reproduces the
generated weights exactly for all 972 generated problems (tested).

25 concepts, prerequisites form a DAG (tested): ARRAYS, HASHING, TWO_POINTERS,
SLIDING_WINDOW, SORTING, BINARY_SEARCH, LINKED_LIST, STACKS_QUEUES, RECURSION, TREES,
GRAPHS, DYNAMIC_PROGRAMMING, BACKTRACKING, GREEDY, HEAP, STRINGS, MATH, BIT_MANIPULATION,
PREFIX_SUM, MATRIX, MONOTONIC_STACK, UNION_FIND, TRIE, SEGMENT_TREE, DESIGN.

Rebuild: `python3 scripts/build_leetcode_catalog.py [--raw PATH] [--size 1000]`. Without
`--raw` it downloads the mirror once into `data/leetcode/raw/` (not committed).

## Event format `kt-events-v1`

JSON Lines, optionally gzip. Produced identically by the simulator and by
`GET /api/research/events`, so every training and evaluation script accepts either.

| field | required | meaning |
|---|---|---|
| `schema` | yes | `"kt-events-v1"` |
| `learnerId` | yes | opaque id (`L-` + HMAC pseudonym in real exports) |
| `seq` | yes | 0-based position in the learner's chronological sequence |
| `problemId` | yes | catalog key, e.g. `LC21` |
| `timestamp` | yes | ISO-8601 UTC |
| `attemptNumber` | yes | 1-based attempt number on this problem |
| `result` | yes | ACCEPTED, WRONG_ANSWER, TIME_LIMIT_EXCEEDED, RUNTIME_ERROR, ABANDONED |
| `correct` | yes | 1 iff ACCEPTED |
| `eventId`, `sessionId`, `timeSpentSeconds`, `hintUsed`, `solutionViewed` | no | |
| `truth` | synthetic only | `{"pCorrect": .., "mastery": {concept: ..}}` |

Splits are by learner (70/15/15 on a salted hash), never by event.

## Synthetic benchmark `data/synthetic/v2` — SYNTHETIC / DEVELOPMENT DATA

Simulator: `model/kt/simulator.py`, seed 20260929, reproducible byte for byte
(`python3 scripts/generate_synthetic_dataset.py`; the Colab notebook checks the numbers
against `summary.json`).

| | |
|---|---|
| learners / events | 2500 / 106 506 |
| problems covered | all 1000 (median 53.5 events per problem) |
| sequence length | mean 42.6, median 34, p90 82, max 335 |
| accepted rate | 0.685 (mean true P 0.686) |
| retry / hint / solution-view rate | 0.220 / 0.025 / 0.008 |

Generative model (every constant is documented in the docstring of `model/kt/simulator.py`;
ground truth in `items_truth.json` and `learners.jsonl.gz`). It is deliberately richer
than any estimator here, so each estimator can be wrong:

* Learner: cohort (beginner 35 % / intermediate 45 % / advanced 20 %), general ability,
  per-concept skill, 1–3 weak concepts (−1.5 logits), weak prerequisites drag dependents
  down, learning rate, per-concept memory that decays between sessions and grows with
  spaced practice.
* Item: true difficulty `(priorDifficulty − 5)/1.25 + N(0, 0.4)`, so the catalog prior is
  informative but noisy; concept weights from the catalog.
* Response: `z = 0.75·Σ w̃ s + 0.25·min s − b + bonus` (weakest-link term; bonuses for
  retries, hints, viewed solutions), `P = 0.02 + 0.94·σ(z)` (guess and slip).
* Learning peaks at P ≈ 0.5 (productive struggle), is halved after viewing the solution
  and spills 30 % onto direct prerequisites.
* Behaviour: sessions, study-plan / comfort / popular / review problem choice, retries,
  hints, solution views, abandonment, failure types and time spent.

What it cannot tell you: anything about real learners. The simulator encodes our
assumptions; models that match these assumptions (IRT, DKT) look good on it by design.
See [evaluation.md](evaluation.md) and [limitations.md](limitations.md).

## Real data

```bash
KT_RESEARCH_EXPORT=true KT_PSEUDONYM_SALT=<secret> ./mvnw spring-boot:run
python3 scripts/export_events.py --base http://localhost:8080 --api-key "$KT_API_KEY"
PYTHONPATH=model/pylib python3 model/dkt/train_dkt.py --real \
    --events data/real/events.jsonl.gz --catalog data/real/catalog.json
```

`train_dkt.py` refuses datasets below 1000 learners or 50 000 attempts. See
[privacy.md](privacy.md) before exporting.

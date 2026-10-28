# Self Evaluation

> Research prototype, evaluated 2026-09-30 against `mdplans/master.md`. A requirement is marked
> complete only where code, a passing test or a report is cited. All model results are on
> **synthetic data**. Live status: [project-status.md](project-status.md).

## Phase

All twelve phases of master plan §49 have an implementation (phase table in
[project-status.md](project-status.md#phase-status-master-plan-49)). This evaluation covers the
whole system at the "research prototype" milestone.

## Requirements Completed

Master plan §59, final definition of done.

### Architecture

- [x] Extension separated from backend intelligence: `extension/` only extracts, queues and
  displays; all estimation is in `backend/` ([architecture.md](architecture.md))
- [x] Backend separated into clear layers: controller → service → entity/repository; pure maths
  in `IrtModel`, `DktModel`, `LinUcb`
- [x] Model components separated: `irt`, `dkt`, `mastery`, `gap`, `localization`,
  `recommendation`, `decision` packages
- [x] Jev isolated as decision layer: only reachable through `DecisionLayer` /
  `DecisionLayerRouter`
- [x] Deterministic fallback exists: `FallbackPolicy`; `DecisionFallbackIT` (4),
  `FallbackPolicyTest` (3)

### Data

- [x] Raw events stored: `interaction_events` (append-only, idempotent by `eventId`)
- [x] Learner state stored: `learner_states`
- [x] Concept state represented: `concept_masteries` (alpha, beta, mastery)
- [x] Problem metadata represented: `problems`, 1000 LeetCode problems (migration `V5`)
- [x] Prerequisite graph represented: `concepts`, `prerequisites` (25 concepts, 21 edges)
- [x] Calibration information represented: `problems.estimated_difficulty`,
  `difficulty_uncertainty`, `sample_size`, `prior_difficulty`

### Intelligence

- [x] IRT baseline implemented: Rasch MAP, `IrtModelTest` (28)
- [x] Adaptive localization implemented: `AdaptiveLocalizationService`,
  `AdaptiveLocalizationServiceTest` (8)
- [x] Concept mastery implemented: Beta-Bernoulli, `ConceptMasteryServiceTest` (6)
- [x] Knowledge-gap detection implemented: `KnowledgeGapService` with evidence and confidence
- [x] Candidate generation implemented: `CandidateGenerationService`, bounded action set
- [x] Recommendation engine implemented: `RecommendationService`, `PolicyValidator`
- [x] DKT extension point implemented/documented: trained LSTM, Java inference parity to 1e-9
  (`DktModelTest`), `DktEnabledIT`; synthetic training data only ([dkt.md](dkt.md))
- [x] Jev integration implemented: deterministic, LinUCB and HTTP providers ([jev.md](jev.md))

### Extension

- [x] Problem metadata extraction: `extract.js`, `extract.test.js` (12)
- [x] Submission/event capture: `content.js` (library logic tested, DOM wiring not)
- [x] API communication: `api.js`, `queue.js` with retry, bounded queue, auth pause
- [x] Recommendation display: popup
- [x] Failure handling: offline queue, backoff, 401/403 pause with popup warning,
  `queue.test.js` (18)

### Testing

- [x] Unit tests: 92 backend, 66 Python, 30 extension
- [x] Integration tests: 45 (embedded PostgreSQL)
- [x] API tests: `ApiContractIT` (19), `ApiKeyIT`, Postman collection (36 requests)
- [x] Edge-case tests: new, sparse, repeated failure/success, concept-specific failure, missing
  metadata, duplicate event, reused `eventId` ([testing.md](testing.md))
- [x] Model invariant tests: probability, mastery and uncertainty bounds in `IrtModelTest`,
  `ConceptMasteryServiceTest`, `model/tests`
- [x] Synthetic dataset tests: `SyntheticRecoveryIT`, `model/tests/test_simulator.py`
- [ ] End-to-end tests: `LearningLoopIT` covers HTTP → DB → state → recommendation → new
  evidence, but nothing drives the real extension in a browser

### Evaluation

- [x] Baseline implemented: static difficulty, success rate, IRT only; IRT + graph, IRT + KT
  and full system partly ([evaluation.md](evaluation.md))
- [x] Metrics implemented: AUC, log loss, Brier, ECE, accuracy, MAE, recovery, simulated gain
- [x] Evaluation dataset available: `data/synthetic/v2` (2500 learners, 106 506 events)
- [x] Ablation support exists: component toggles; `GraphDisabledIT`, `IrtDisabledIT`,
  `JevDisabledIT`
- [x] Limitations documented: [limitations.md](limitations.md)

### Documentation

- [x] README, architecture, user flow, data flow, model flow, API flow, domain model
- [x] IRT, DKT, adaptive localization, knowledge graph, recommendation, JEV
- [x] Testing, evaluation, limitations, future work, self evaluation
  ([final-report.md](final-report.md) for the synthesis)

## Tests

| | Total | Passed | Failed | Skipped |
|---|---|---|---|---|
| Backend unit | 92 | 92 | 0 | 0 |
| Backend integration | 45 | 45 | 0 | 0 |
| Python | 66 | 66 | 0 | 0 |
| Extension | 31 | 30 | 0 | 1 (`live.test.js`, opt-in) |
| **All** | **234** | **233** | **0** | **1** |

## Scorecard (master plan §57)

No overall score is given, by design.

| Category | Status |
|---|---|
| FOUNDATION | PASS |
| DOMAIN MODEL | PASS |
| EVENT PIPELINE | PASS |
| IRT | PASS |
| KNOWLEDGE GRAPH | PARTIAL |
| KNOWLEDGE GAP DETECTION | PARTIAL |
| ADAPTIVE LOCALIZATION | PASS |
| RECOMMENDATION ENGINE | PASS |
| JEV INTEGRATION | PASS |
| BROWSER EXTENSION | PARTIAL |
| TESTING | PARTIAL |
| EVALUATION | PARTIAL |
| DOCUMENTATION | PASS |
| SECURITY | PARTIAL |
| OBSERVABILITY | PASS |

Notes on the PASS ratings that carry caveats:
- **Adaptive localization** meets its requirement (implemented, tested, documented with
  limitations), but in the synthetic evaluation it did not reduce final ability error (0.734
  vs 0.701 for IRT only, `model/reports/evaluation-report.md`).
- **Jev integration** meets the bounded-decision, validation and fallback requirements. The
  bandit is evaluated only in simulation, and the HTTP provider's LLM mode is not evaluated.

### PARTIAL categories

**KNOWLEDGE GRAPH**
- Missing: concept grouping (§14); `downstreamConcepts` has no caller; edges are curated, not
  validated against data; the cycle check is not safe under concurrent inserts.
- Why it matters: gap explanations cannot say what a weak concept blocks, and a wrong edge
  misdirects prerequisite reviews.
- Fix: validate edges against exported data, add groups, use downstream lookup in explanations
  ([future-work.md](future-work.md) item 10).

**KNOWLEDGE GAP DETECTION**
- Missing: `HIGH_ITEM_DIFFICULTY` is never emitted; only direct prerequisites are checked; one
  weak concept per report. Weakest-concept hit rate is 0.083–0.174 against random 0.0966
  (synthetic data, `model/reports/kt-evaluation.md`).
- Why it matters: the "why is the learner struggling" answer is often wrong at concept level.
- Fix: transitive prerequisites, several weak concepts, calibrate gap confidence against
  labelled cases (future-work item 9).

**BROWSER EXTENSION**
- Missing: the content script, service worker and popup were not run in Chrome during this
  review; only the library modules are unit tested. The extension depends on LeetCode DOM
  selectors and English text; `host_permissions` cover `localhost:8080` only.
- Why it matters: a markup change silently stops data collection.
- Fix: manual Chrome test now; selector health checks and a browser test harness later.

**TESTING**
- Missing: browser end-to-end test; concurrency and out-of-order timestamp tests; coverage
  measurement (no JaCoCo); CI.
- Why it matters: races (duplicate delivery, cycle check) and extension wiring are unverified.
- Fix: add JaCoCo and a CI workflow; add concurrent-ingestion ITs.

**EVALUATION**
- Missing: any real-learner data or controlled study; the backend IRT + DKT blend is not
  evaluated offline; the Python policy mirror differs from the backend in three rules; no run
  combines IRT + DKT + graph + bandit.
- Why it matters: no claim about learning effectiveness can be made.
- Fix: collect consented data through the research export; align the mirror; add the missing
  configurations (future-work items 11–12).

**SECURITY**
- Missing: per-user authentication and authorization, rate limiting, TLS in the app;
  `/actuator/metrics` needs no key.
- Why it matters: anyone with the shared key and a `userId` can read that learner's state.
- Fix: per-learner tokens, a reverse proxy for TLS and rate limiting
  ([security.md](security.md)).

## Architecture Checks

- [x] No business logic in controllers or repositories
- [x] Entities never returned from controllers (response DTOs)
- [x] Jev reachable only through `DecisionLayer`
- [x] Flyway owns the schema; Hibernate `ddl-auto: validate`
- [x] Transactions only at service methods that need atomicity
  ([domain-model.md](domain-model.md#transactions))
- [ ] External calls outside transactions: the HTTP Jev call runs inside `nextAction`'s
  transaction

## Model Checks

- [x] Higher ability → higher P(success); harder item → lower P(success) (`IrtModelTest`)
- [x] Success never lowers ability under the Rasch update (`IrtModelTest`)
- [x] One failure does not destroy mastery (`ConceptMasteryServiceTest`)
- [x] Uncertainty shrinks with informative evidence (`IrtModelTest`)
- [x] Probability, mastery ∈ [0, 1]; uncertainty ≥ 0; values finite
- [x] Java DKT inference matches Python to 1e-9 (`DktModelTest`)
- [x] Synthetic ability and item difficulty recovered (`SyntheticRecoveryIT`, reports)
- [ ] Concept-level diagnosis reliable: no (see gap detection above)

## Documentation Checks

- [x] Every §24 document exists; flow and architecture docs contain Mermaid diagrams
- [x] Every relative doc link resolves
- [x] Metrics labelled as synthetic data; no "production ready" claims
- [x] Observations distinguished from estimates ([architecture.md](architecture.md) §1,
  [domain-model.md](domain-model.md))

## Known Limitations

See [limitations.md](limitations.md). The main ones: synthetic-only evaluation; selection and
exposure bias in item calibration; DKT trained on simulated learners; sticky calibration with
no re-localization; DOM-dependent extension.

## Technical Debt

- Offline Python policy mirror (`model/kt/policies.py`) drifts from the Java rules
- `run_evaluation.py` always reports DKT as `UNAVAILABLE` (the trained model is evaluated in
  `evaluate_kt.py` instead)
- Candidate generation loads all problems per request
- Every `next-action` call writes an audit row; feedback rows are never read
- No state-rebuild command

## Next Required Work

1. Manual Chrome test of the extension against a running backend.
2. Collect consented real data; retrain DKT; re-run the evaluation on it.
3. Align the offline mirror and evaluate the IRT + DKT blend.
4. Per-user authentication, CI with coverage.
5. Initialise git and commit (master plan §73: not done automatically).

## Final self-evaluation (master plan §76)

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | Can a raw learner event enter the system? | Yes | `POST /api/events`, `ApiContractIT` |
| 2 | Can the system persist it? | Yes | `interaction_events`, `LearningLoopIT` |
| 3 | Can learner state be updated? | Yes | `LearnerStateService.applyEvent` |
| 4 | Can the system estimate ability? | Yes | `IrtService.estimateAbility`, `IrtModelTest` |
| 5 | Can the system estimate concept mastery? | Yes | `ConceptMasteryService`, optional DKT |
| 6 | Can it identify a potential knowledge gap? | Yes, with low concept-level accuracy on synthetic data | `KnowledgeGapService`, `kt-evaluation.md` |
| 7 | Can it identify prerequisite relationships? | Yes | `KnowledgeGraphService`, [knowledge-graph.md](knowledge-graph.md) |
| 8 | Can it generate multiple candidate actions? | Yes | `CandidateGenerationService` |
| 9 | Can the decision layer choose among bounded actions? | Yes | `PolicyValidator` rejects out-of-set actions, `DecisionFallbackIT` |
| 10 | Is there a deterministic fallback? | Yes | `FallbackPolicy`, `FallbackPolicyTest` |
| 11 | Can the system recommend another problem? | Yes | `GET /api/learners/{userId}/next-action` |
| 12 | Can the learner's next interaction become new evidence? | Yes | `OutcomeRewardService.onOutcome`, `BanditLoopIT` |
| 13 | Is the entire loop testable? | Yes, from HTTP inward; not through the browser | `LearningLoopIT` |
| 14 | Can the system operate without the browser extension? | Yes | Postman collection, `scripts/replay_synthetic.py` |
| 15 | Can synthetic learners be generated? | Yes | `scripts/generate_synthetic_dataset.py`, `data/synthetic/v2` |
| 16 | Can the model be evaluated against baselines? | Yes, on synthetic data | [evaluation.md](evaluation.md) |
| 17 | Can individual components be ablated? | Yes | yml toggles, `*DisabledIT` |
| 18 | Are uncertainty and limitations represented? | Yes | `uncertainty` fields, [limitations.md](limitations.md) |
| 19 | Are model estimates distinguished from observations? | Yes | data classes in [architecture.md](architecture.md) |
| 20 | Is every major architectural decision documented? | Yes | AD-1 to AD-9 in [architecture.md](architecture.md) |

No answer is NO, so the **research-prototype milestone** is reached. That is not a claim that
the system improves learning: that has not been evaluated with real learners.

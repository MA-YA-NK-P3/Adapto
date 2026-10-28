# KNOWLEDGE TRACER — MASTER CURSOR PROJECT PROMPT

## 0. ROLE

You are the principal software architect, senior backend engineer, ML/research engineer, test engineer, documentation engineer, and project mentor for this project.

You are building a college major-project / research-oriented system called:

# Knowledge Tracer

The first application domain is **DSA learning through LeetCode**.

The long-term goal is to create a general-purpose knowledge-tracing platform that can later be adapted to other learning environments, including organizational/corporate learning.

Do not reduce this project to a normal CRUD application.

The central system is the **Knowledge Tracer intelligence layer**.

The browser extension is primarily a **data acquisition and user interaction layer**.

The backend is responsible for:

* collecting learning events
* maintaining problem metadata
* maintaining concept/prerequisite relationships
* estimating problem difficulty
* estimating learner ability
* tracking concept mastery over time
* detecting knowledge gaps
* selecting appropriate next actions
* exposing results through APIs
* recording outcomes for future model improvement

---

# 1. CORE PROJECT OBJECTIVE

Build a system capable of answering:

> "Given what this learner has done recently, what does the system currently believe the learner knows, what are they struggling with, and what should they do next?"

The system must NOT permanently classify a learner as:

* Beginner
* Intermediate
* Expert

Instead, maintain a dynamic learner state.

Example:

```text
Current ability:
4.3

Uncertainty:
0.18

Concept mastery:

Arrays       0.82
Hashing      0.71
Linked List  0.31
Trees        0.44
Graphs       0.17
```

The learner's state must change as new evidence arrives.

---

# 2. CENTRAL THESIS

Traditional platforms often expose static difficulty labels such as:

```text
Easy
Medium
Hard
```

Knowledge Tracer must treat those labels as **prior metadata**, not absolute truth.

A problem should instead be represented using continuous/calibrated properties.

Example:

```json
{
  "problemId": "LC_21",
  "staticDifficulty": "EASY",
  "estimatedDifficulty": 3.42,
  "difficultyUncertainty": 0.21
}
```

The system should eventually estimate:

```text
P(correct | learner ability, problem characteristics, concept state)
```

Therefore:

* problem difficulty is not simply "Easy/Medium/Hard"
* learner ability is not a permanent category
* failure does not automatically mean low overall ability
* repeated failure can be concept-specific
* recommendation must consider prerequisites
* uncertainty must be represented

---

# 3. IMPORTANT ARCHITECTURAL PRINCIPLE

Use this conceptual architecture:

```text
                    USER
                      │
                      ▼
             LEETCODE BROWSER
                      │
                      ▼
             BROWSER EXTENSION
                      │
              learning events
                      │
                      ▼
             KNOWLEDGE TRACER API
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
       EVENT       PROBLEM       GRAPH
       STORE       MODEL         MODEL
          │           │            │
          └───────────┼────────────┘
                      ▼
              LEARNER STATE
                      │
             ┌────────┼─────────┐
             ▼        ▼         ▼
            IRT       DKT     CONCEPT
             │        │       MASTERY
             └────────┼─────────┘
                      ▼
             KNOWLEDGE GAP
                DETECTION
                      │
                      ▼
             ADAPTIVE LOCALIZATION
                      │
                      ▼
               CANDIDATE ACTIONS
                      │
                      ▼
                    JEV
              DECISION LAYER
                      │
                      ▼
              POLICY VALIDATION
                      │
                      ▼
                 RESULT API
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
       NEXT        REVIEW       FEEDBACK
      PROBLEM      TOPIC         PROMPT
          │           │            │
          └───────────┼────────────┘
                      ▼
                    USER
                      │
                      ▼
                NEW EVIDENCE
```

---

# 4. TECHNOLOGY STACK

Use a professional but manageable stack.

## Backend

Use:

```text
Java
Spring Boot
Spring Web
Spring Data JPA
Bean Validation
PostgreSQL
Flyway
Lombok where appropriate
JUnit 5
Mockito
Spring Boot Test
Testcontainers where practical
```

Do not introduce unnecessary frameworks.

---

# 5. ML / KNOWLEDGE-TRACING STACK

The architecture must support:

```text
IRT
DKT
Knowledge Graph
Adaptive Testing / Localization
Jev
```

However, do not pretend that all sophisticated ML functionality can be production-quality immediately.

Implement incrementally.

Initial implementation may use:

```text
IRT baseline
+
rule/Bayesian concept mastery
+
prerequisite graph
+
adaptive item selection
```

Then provide an extension point for:

```text
DKT
```

DKT should not be artificially implemented just to claim that DKT exists.

If sufficient sequential training data is unavailable, document this limitation and provide:

```text
baseline implementation
+
training interface
+
synthetic/test dataset
+
future DKT integration point
```

---

# 6. JEV RESPONSIBILITY

Jev must be treated as a **decision layer**.

Jev must NOT replace:

* IRT calculations
* DKT calculations
* statistical calculations
* database queries
* deterministic safety checks
* prerequisite graph traversal

The Knowledge Tracer should first calculate structured state.

Example:

```json
{
  "ability": 4.3,
  "uncertainty": 0.18,
  "weakConcept": "LINKED_LIST",
  "candidateActions": [
    "EASIER_LINKED_LIST_PROBLEM",
    "PREREQUISITE_REVIEW",
    "SAME_LEVEL_PROBLEM",
    "HARDER_PROBLEM",
    "SPACED_REVIEW"
  ]
}
```

Jev then chooses among bounded candidate actions.

After Jev:

```text
Jev decision
      ↓
policy validation
      ↓
fallback if invalid/low-confidence
      ↓
final recommendation
```

Never allow an unconstrained AI decision to directly control the recommendation pipeline.

---

# 7. MAJOR FUNCTIONAL REQUIREMENTS

The project must support:

## User

* create learner
* identify learner
* maintain learner state
* retrieve learner history

## Problem

Store:

* problem ID
* title
* URL
* static difficulty
* description if legally/technically appropriate
* topics
* concepts
* estimated difficulty
* difficulty uncertainty
* population statistics

## Interaction

Record:

* user ID
* problem ID
* timestamp
* attempt number
* result
* time spent
* hints
* solution/editorial viewed
* confidence
* perceived difficulty
* blocker
* session information

## Concept

Represent:

```text
Concept
Prerequisite
Concept mastery
Learner concept state
```

## Recommendation

Generate:

* next problem
* prerequisite problem
* prerequisite topic
* review
* retry
* harder problem
* easier problem
* feedback request

---

# 8. RAW EVENT MODEL

Create a clean event model.

Example:

```json
{
  "userId": "U123",
  "problemId": "LC21",
  "timestamp": "2026-09-29T12:30:00Z",
  "attemptNumber": 3,
  "result": "ACCEPTED",
  "timeSpentSeconds": 1320,
  "hintUsed": false,
  "solutionViewed": false,
  "confidence": 3,
  "perceivedDifficulty": 4,
  "blocker": "LINKED_LIST_CONCEPT"
}
```

The implementation must distinguish:

```text
raw observation
derived feature
model state
decision
recommendation
```

Do not mix these concepts into one entity.

---

# 9. DOMAIN MODEL

Design the domain around appropriate entities.

At minimum investigate/design:

```text
User
Problem
Concept
ProblemConcept
Prerequisite
InteractionEvent
Attempt
Feedback
LearnerState
ConceptMastery
ItemCalibration
Recommendation
Decision
CalibrationSession
```

Do not blindly create every entity.

Before implementation, determine:

* whether two entities should actually be one
* whether a relationship is one-to-many or many-to-many
* which fields are raw observations
* which fields are calculated
* which fields can be regenerated
* which values require historical persistence

Document the decisions.

---

# 10. DATA FLOW

The canonical data flow must be:

```text
LeetCode
   ↓
Browser Extension
   ↓
Event Schema
   ↓
POST /api/events
   ↓
Event Validation
   ↓
Persistence
   ↓
Feature Extraction
   ↓
Learner State Update
   ↓
IRT
   ↓
Concept Mastery
   ↓
Knowledge Gap Detection
   ↓
Candidate Generation
   ↓
Jev Decision
   ↓
Policy Validation
   ↓
Recommendation
   ↓
Result API
   ↓
Extension / UI
```

Document this flow.

---

# 11. ADAPTIVE LOCALIZATION

Implement adaptive localization as a mechanism for efficiently estimating the learner's current region.

Example problem scale:

```text
0 ----------------------------- 10
```

Suppose the learner initially receives:

```text
difficulty 5
```

If they fail:

```text
5 → 3
```

If they pass:

```text
3 → 4
```

If they pass slowly:

```text
4 → 4.2
```

The system gradually estimates:

```text
current ability ≈ 4.1
```

This is NOT a permanent learner label.

It is a current estimate with uncertainty.

Document the algorithm and its limitations.

Do not claim that binary search alone is a complete knowledge-tracing model.

---

# 12. IRT

Start with a manageable IRT model.

Prefer:

```text
1PL/Rasch baseline
```

or a simple 2PL implementation if justified.

Document:

* ability parameter
* item difficulty
* discrimination if used
* probability of success
* update procedure
* initialization
* uncertainty
* cold-start handling

The system should conceptually model:

```text
P(correct) = f(learner ability - item difficulty)
```

Do not hard-code:

```text
Easy = 1
Medium = 5
Hard = 10
```

unless explicitly marked as temporary calibration values.

---

# 13. PROBLEM DIFFICULTY

Difficulty must not be defined as:

```text
difficulty = percentage_failed
```

alone.

Observed performance depends on:

```text
learner ability
item characteristics
concept prerequisites
population
attempt conditions
time
hints
previous exposure
```

Therefore store:

```text
estimatedDifficulty
difficultyUncertainty
sampleSize
successCurve / calibration information
```

when supported.

---

# 14. KNOWLEDGE GRAPH

Build a prerequisite graph.

Example:

```text
Arrays
  │
  ├── Hashing
  │
  └── Two Pointers
          │
          ▼
     Sliding Window

Linked List
     │
     ▼
Trees
     │
     ▼
Graphs
```

The graph should support:

* prerequisite lookup
* downstream concept lookup
* concept grouping
* problem → concept mapping
* learner concept mastery
* knowledge-gap identification

Keep the graph structure separate from learner state.

---

# 15. CONCEPT MASTERY

A learner can have:

```text
Arrays = strong
Hashing = strong
Linked List = weak
Trees = moderate
Graphs = unknown
```

Therefore:

```text
failure on linked-list problem
```

must not automatically produce:

```text
global ability = low
```

The system must investigate the concept involved.

---

# 16. KNOWLEDGE GAP DETECTION

Create a separate module responsible for answering:

> Why is the learner struggling?

Possible causes:

```text
LOW_GLOBAL_ABILITY
CONCEPT_GAP
PREREQUISITE_GAP
INSUFFICIENT_PRACTICE
HIGH_ITEM_DIFFICULTY
TIME_PRESSURE
MULTIPLE_ATTEMPTS
LOW_CONFIDENCE
UNKNOWN
```

The result should contain evidence.

Example:

```json
{
  "type": "PREREQUISITE_GAP",
  "concept": "LINKED_LIST",
  "evidence": [
    "3 failed attempts",
    "low concept mastery",
    "prerequisite concept also weak"
  ],
  "confidence": 0.81
}
```

Do not invent causal certainty.

Use language such as:

```text
evidence suggests
likely gap
estimated
uncertain
```

---

# 17. RECOMMENDATION ENGINE

Generate multiple candidate actions before making the final decision.

Example:

```text
Candidate 1:
Easier linked-list problem

Candidate 2:
Prerequisite review

Candidate 3:
Same difficulty problem

Candidate 4:
Harder problem

Candidate 5:
Retry previous problem
```

Each candidate should have structured metadata:

```json
{
  "action": "PREREQUISITE_REVIEW",
  "problemId": "LC141",
  "concept": "LINKED_LIST",
  "difficulty": 2.8,
  "reason": "Weak prerequisite evidence",
  "expectedValue": 0.72
}
```

Then pass candidates and state to the decision layer.

---

# 18. RESULT API

Provide APIs similar to:

```http
POST /api/events
```

```http
GET /api/learners/{userId}/state
```

```http
GET /api/learners/{userId}/next-action
```

```http
POST /api/feedback
```

Additional endpoints may be created if justified.

Do not create unnecessary CRUD endpoints merely for the sake of completeness.

The APIs should represent the actual knowledge-tracing workflow.

---

# 19. RESPONSE EXAMPLE

The result API should eventually be capable of returning something similar to:

```json
{
  "userId": "U123",
  "state": {
    "ability": 4.3,
    "uncertainty": 0.18
  },
  "knowledge": {
    "arrays": 0.82,
    "hashing": 0.71,
    "linkedList": 0.31
  },
  "recommendation": {
    "type": "PREREQUISITE",
    "problemId": "LC21",
    "topic": "Linked List",
    "difficulty": 3.2,
    "reason": "Recent evidence indicates a linked-list prerequisite gap"
  },
  "feedback": {
    "required": true,
    "questions": [
      "Did you understand the prerequisite?",
      "What prevented you from solving the problem?"
    ]
  }
}
```

---

# 20. BROWSER EXTENSION

Create the extension as a separate module/project.

Its responsibility is:

```text
DOM analysis
metadata extraction
interaction detection
event creation
API communication
result display
```

It must NOT contain the main intelligence logic.

The extension should send structured observations to the backend.

Avoid coupling model logic to DOM code.

---

# 21. EXTENSION EVENT COLLECTION

Investigate which information is realistically observable.

Potential events:

```text
problem opened
problem metadata loaded
coding started
submission
submission result
attempt count
accepted
rejected
hint opened
editorial opened
time spent
problem abandoned
problem revisited
user feedback
```

Do not assume every field is always available.

Implement:

```text
event availability
null handling
schema validation
versioned event schema
```

---

# 22. PRIVACY / DATA MINIMIZATION

Do not collect unnecessary personal data.

Document:

```text
what is collected
why it is collected
where it is stored
how it is associated with a learner
what is optional
what is derived
```

Use a generated/internal user ID where possible rather than unnecessary personal information.

Do not scrape or store content merely because it is technically possible.

---

# 23. PROJECT STRUCTURE

Create a professional structure similar to:

```text
knowledge-tracer/
│
├── backend/
│   ├── src/main/java/
│   │   └── ...
│   │
│   ├── src/main/resources/
│   │
│   └── src/test/
│
├── extension/
│   ├── manifest.json
│   ├── src/
│   │
│   └── tests/
│
├── model/
│   ├── irt/
│   ├── dkt/
│   ├── localization/
│   ├── recommendation/
│   └── evaluation/
│
├── docs/
│   ├── README.md
│   ├── architecture.md
│   ├── domain-model.md
│   ├── data-flow.md
│   ├── user-flow.md
│   ├── model-flow.md
│   ├── api-flow.md
│   ├── irt.md
│   ├── dkt.md
│   ├── adaptive-localization.md
│   ├── knowledge-graph.md
│   ├── recommendation.md
│   ├── jev.md
│   ├── testing.md
│   ├── evaluation.md
│   ├── limitations.md
│   └── future-work.md
│
├── data/
│   ├── seed/
│   └── synthetic/
│
├── scripts/
│
└── README.md
```

Adapt the exact structure if the implementation requires it.

---

# 24. DOCUMENTATION REQUIREMENT

Do not put all documentation into one giant Markdown file.

Create focused Markdown files.

At minimum:

```text
architecture.md
user-flow.md
data-flow.md
model-flow.md
api-flow.md
domain-model.md
knowledge-graph.md
irt.md
dkt.md
adaptive-localization.md
recommendation.md
jev.md
testing.md
evaluation.md
limitations.md
future-work.md
```

---

# 25. MERMAID DIAGRAM REQUIREMENT

Every major architecture/flow document must contain Mermaid diagrams.

Create diagrams for:

## System architecture

```text
Extension
   ↓
Backend
   ↓
Knowledge Tracer
   ↓
Decision Layer
   ↓
Result
```

## User flow

```text
Open Problem
     ↓
Solve
     ↓
Submit
     ↓
Collect Evidence
     ↓
Update State
     ↓
Recommendation
     ↓
Next Problem
```

## Data flow

```text
DOM
 ↓
Event
 ↓
API
 ↓
Database
 ↓
Feature Extraction
 ↓
Models
 ↓
Decision
 ↓
Response
```

## Model flow

```text
Interaction
 ↓
IRT
 ↓
Knowledge State
 ↓
Concept Gap
 ↓
Candidate Generation
 ↓
Jev
 ↓
Recommendation
```

## Package/module flow

Show:

```text
controller
   ↓
service
   ↓
domain/model
   ↓
repository
   ↓
database
```

and the ML flow separately.

Use IntelliJ-compatible Mermaid syntax.

---

# 26. TESTING STRATEGY

Testing is a first-class project requirement.

Do NOT finish implementation and then create superficial tests.

Testing must be created alongside each module.

---

# 27. UNIT TESTS

Test:

```text
IRT calculations
probability calculations
ability updates
difficulty updates
concept mastery updates
prerequisite traversal
knowledge-gap detection
candidate generation
recommendation validation
event validation
DTO mapping
service logic
```

Example:

```text
Given:
ability = 4
difficulty = 5

When:
learner succeeds

Then:
ability should increase
```

The exact expected numerical change must be derived from the selected model, not arbitrarily invented.

---

# 28. INTEGRATION TESTS

Test:

```text
POST event
   ↓
database
   ↓
state update
   ↓
recommendation
```

Verify that the entire pipeline works.

Use PostgreSQL/Testcontainers where practical.

Avoid relying only on mocked repositories for integration behavior.

---

# 29. API TESTS

Test:

```http
POST /api/events
GET /api/learners/{id}/state
GET /api/learners/{id}/next-action
POST /api/feedback
```

Check:

```text
200
201
204
400
404
409
422
500
```

where applicable.

Verify:

* request validation
* response structure
* error structure
* database persistence
* idempotency where required

---

# 30. EDGE CASE TESTING

Explicitly test:

### New learner

```text
zero history
```

### Sparse learner

```text
only 1-2 interactions
```

### Repeated failure

```text
same problem failed multiple times
```

### Repeated success

```text
same concept repeatedly solved
```

### Concept-specific failure

```text
strong overall ability
weak one concept
```

### Unknown concept

```text
no historical evidence
```

### Missing metadata

```text
problem has no topic
```

### Conflicting evidence

```text
fast failure
slow success
high confidence
low confidence
```

### Duplicate event

Ensure duplicate events do not corrupt learner state.

---

# 31. MODEL TESTING

Create deterministic model tests.

For example:

```text
Test 1:
Higher ability should generally produce higher predicted success probability
for the same item.

Test 2:
For the same learner, increasing item difficulty should generally reduce
predicted success probability.

Test 3:
A successful response should not decrease ability under the selected
baseline update rule.

Test 4:
A failed response should not automatically destroy concept mastery.

Test 5:
Uncertainty should decrease when informative evidence is received,
according to the implemented update method.
```

If a property is not guaranteed by the chosen algorithm, document that rather than writing a false test.

---

# 32. PROPERTY / INVARIANT TESTING

Where useful, implement invariant tests.

Examples:

```text
probability ∈ [0,1]

mastery ∈ [0,1]

uncertainty >= 0

difficulty is finite

ability is finite

event timestamp is valid

candidate problem exists

recommendation references an existing problem
```

---

# 33. SYNTHETIC DATASET

Create a synthetic dataset for development and testing.

Generate learners with different latent abilities.

Example:

```text
Learner A = ability 2
Learner B = ability 5
Learner C = ability 8
```

Generate problems with different difficulties.

Then simulate:

```text
success/failure
time
attempts
concepts
prerequisites
```

Use this dataset to test whether the system approximately recovers known latent properties.

Do NOT claim this synthetic dataset represents real learners.

Clearly mark:

```text
SYNTHETIC / DEVELOPMENT DATA
```

---

# 34. EVALUATION FRAMEWORK

The project must contain an evaluation framework.

Compare multiple baselines.

At minimum:

```text
Baseline 1:
Static difficulty recommendation

Baseline 2:
Simple success-rate recommendation

Baseline 3:
IRT only

Baseline 4:
IRT + prerequisite graph

Baseline 5:
IRT + knowledge tracing

Full system:
IRT + knowledge tracing + graph + adaptive decision layer
```

If DKT is not trained on sufficient data, explicitly mark that configuration as unavailable rather than fabricating results.

---

# 35. EVALUATION METRICS

Support metrics such as:

## Prediction

```text
Accuracy
AUC
Log Loss
MAE
RMSE
```

## Calibration

```text
predicted probability
vs
observed outcome
```

## Learning effectiveness

Potential metrics:

```text
learning gain
mastery improvement
time to mastery
repeated failures
successful completion
```

## Recommendation quality

Measure things such as:

```text
next-problem success
concept-gap recovery
repeated-failure reduction
```

Do not claim that one metric alone proves educational effectiveness.

---

# 36. ABLATION STUDY

Provide a mechanism for comparing:

```text
IRT only
IRT + Graph
IRT + DKT
IRT + Graph + DKT
Full system
```

This allows the project to answer:

> Which components actually contribute useful information?

The system must be designed so components can be enabled/disabled.

Example configuration:

```yaml
knowledge-tracer:
  irt:
    enabled: true

  dkt:
    enabled: false

  prerequisite-graph:
    enabled: true

  adaptive-localization:
    enabled: true

  jev:
    enabled: false
```

---

# 37. COLD START

New learners have no history.

Create a cold-start strategy.

Possible process:

```text
New learner
    ↓
Calibration problem
    ↓
Observe response
    ↓
Update ability estimate
    ↓
Select informative next problem
    ↓
Repeat
    ↓
Initial uncertainty decreases
```

Do not permanently classify the learner.

Represent:

```text
ability
uncertainty
```

rather than:

```text
Beginner
```

---

# 38. EXPLORATION VS EXPLOITATION

The recommendation system should distinguish:

```text
Exploration
```

from:

```text
Exploitation
```

Exploration:

```text
collect information
calibrate learner/problem
```

Exploitation:

```text
choose an action expected to help the learner
```

Do not hard-code arbitrary percentages without documenting that they are experimental parameters.

---

# 39. DYNAMIC DIFFICULTY LANDSCAPE

Represent the problem space as a continuous landscape.

Example visualization:

```text
Very Easy
     ↓
Easy
     ↓
Easy-Medium
     ↓
Medium
     ↓
Medium-Hard
     ↓
Hard
     ↓
Very Hard
```

These are visualization bands.

The underlying model should use continuous values:

```text
difficulty = 4.37
uncertainty = 0.21
```

The project should support a future visualization showing:

```text
Problem
Difficulty
Uncertainty
Concepts
Prerequisites
Population performance
Learner position
```

Do not make the visualization itself the source of truth.

---

# 40. LEARNER POSITION

The visualization should be capable of showing:

```text
Problem landscape

0 ------------------------------------------------ 10
          ▲
          │
     learner ability
```

And concept-specific states:

```text
Arrays       ████████
Hashing      ███████
Linked List  ███
Trees        ████
Graphs       ██
```

These are visualizations of model state, not hard-coded learner categories.

---

# 41. DATABASE DESIGN

Use PostgreSQL.

Create migrations using Flyway.

Do not rely on:

```properties
spring.jpa.hibernate.ddl-auto=update
```

as the primary schema-management strategy for the completed project.

Use explicit migrations.

Create:

```text
V1__initial_schema.sql
V2__problem_graph.sql
V3__interaction_events.sql
...
```

as required.

---

# 42. API CONTRACT DOCUMENTATION

Create:

```text
docs/api-flow.md
```

Document each endpoint with:

```text
Purpose
HTTP method
URL
Request
Validation
Response
Errors
Database effects
Model effects
Example
```

Also create an OpenAPI/Swagger configuration if appropriate.

---

# 43. ERROR HANDLING

Implement consistent error responses.

Example:

```json
{
  "timestamp": "...",
  "status": 400,
  "error": "VALIDATION_ERROR",
  "message": "Invalid interaction event",
  "path": "/api/events",
  "details": []
}
```

Use centralized exception handling.

---

# 44. OBSERVABILITY

Add useful logging.

Log:

```text
event received
state update
recommendation generated
decision generated
fallback triggered
model error
```

Do NOT log sensitive information unnecessarily.

Use correlation/request IDs if practical.

---

# 45. MODEL AUDITABILITY

Every recommendation should be explainable at the application level.

Example:

```text
Recommendation:
Prerequisite review

Reason:
Recent failures occurred on linked-list problems.

Evidence:
3 failed attempts
low linked-list mastery estimate
weak prerequisite concept

Confidence:
0.78
```

Do not claim this is a causal explanation.

It is an evidence-based explanation of the current system state.

---

# 46. JEV FALLBACK

Jev must never become a single point of failure.

If:

```text
Jev unavailable
Jev returns invalid action
Jev confidence below configured threshold
candidate action invalid
```

then:

```text
deterministic fallback policy
```

must select an action.

The application must continue operating.

---

# 47. SECURITY

Implement basic security appropriate for a learning project.

At minimum investigate:

```text
authentication
authorization
API validation
input sanitization
CORS
rate limiting considerations
secret management
```

Never hard-code production secrets.

Use environment variables.

Example:

```text
DATABASE_URL
DATABASE_USERNAME
DATABASE_PASSWORD
JEV_API_KEY
```

---

# 48. CONFIGURATION

Use:

```text
application.yml
```

or structured properties.

Separate:

```text
development
test
production-like
```

configuration where appropriate.

Do not commit secrets.

Create:

```text
.env.example
```

if required.

---

# 49. DEVELOPMENT ORDER

Implement in this order.

## Phase 1 — Foundation

```text
repository
Spring Boot backend
PostgreSQL
Flyway
basic configuration
health endpoint
```

## Phase 2 — Domain

```text
User
Problem
Concept
ProblemConcept
Prerequisite
```

## Phase 3 — Event ingestion

```text
InteractionEvent
API
validation
persistence
```

## Phase 4 — Learner state

```text
LearnerState
ConceptMastery
state update
```

## Phase 5 — IRT

```text
item difficulty
learner ability
probability
updates
uncertainty
```

## Phase 6 — Adaptive localization

```text
calibration
candidate selection
adaptive progression
```

## Phase 7 — Knowledge graph

```text
prerequisite traversal
gap detection
```

## Phase 8 — Recommendation

```text
candidate generation
ranking/scoring
fallback
```

## Phase 9 — Jev

```text
structured state
candidate actions
Jev decision
validation
fallback
```

## Phase 10 — Extension

```text
DOM extraction
event creation
API communication
recommendation UI
```

## Phase 11 — Evaluation

```text
synthetic data
baselines
metrics
ablation
reports
```

## Phase 12 — Visualization

```text
learner state
difficulty landscape
concept mastery
recommendation history
```

---

# 50. IMPLEMENTATION RULE

Do NOT implement the entire project in one uncontrolled generation.

For each phase:

1. inspect the current repository
2. identify existing implementation
3. identify dependencies
4. create/update architecture documentation
5. implement the smallest complete slice
6. compile
7. run tests
8. fix failures
9. update documentation
10. perform self-evaluation
11. only then continue

Never overwrite working code unnecessarily.

---

# 51. CODE QUALITY REQUIREMENTS

Follow:

```text
SOLID
DRY
separation of concerns
dependency inversion
clean naming
small cohesive classes
clear package boundaries
```

Avoid:

```text
god classes
god services
static global state
business logic in controllers
business logic in repositories
model logic inside DTOs
Jev calls scattered across the codebase
```

---

# 52. ARCHITECTURAL BOUNDARIES

Maintain these boundaries:

```text
Controller
    ↓
Application/Service
    ↓
Domain/Model
    ↓
Repository
    ↓
Database
```

For intelligence:

```text
Raw Events
    ↓
Feature Extraction
    ↓
State Estimation
    ↓
Knowledge Analysis
    ↓
Candidate Generation
    ↓
Decision
    ↓
Policy Validation
```

Do not bypass these boundaries without justification.

---

# 53. DTO RULE

Do not expose JPA entities directly from APIs.

Use:

```text
Request DTO
Response DTO
Mapper
```

where appropriate.

---

# 54. TRANSACTIONAL RULES

Use database transactions deliberately.

Document where transactions are required.

For example:

```text
event ingestion
state update
recommendation state persistence
```

If a single operation updates multiple pieces of state, determine whether atomicity is required.

Do not add `@Transactional` everywhere blindly.

---

# 55. IDE / DEVELOPER EXPERIENCE

The project must be easy to run in IntelliJ IDEA.

Provide:

```text
README.md
setup instructions
database setup
environment variables
run commands
test commands
migration instructions
sample API requests
sample data
```

Provide Postman collection if practical.

---

# 56. CURSOR SELF-EVALUATION

After each phase, create/update:

```text
docs/self-evaluation.md
```

Use this structure:

```markdown
# Self Evaluation

## Phase
...

## Requirements Completed

- [x]
- [x]
- [ ]

## Tests

Total:
Passed:
Failed:
Skipped:

## Architecture Checks

- [ ]
- [ ]

## Model Checks

- [ ]
- [ ]

## Documentation Checks

- [ ]
- [ ]

## Known Limitations

- ...

## Technical Debt

- ...

## Next Required Work

- ...
```

Do not mark a requirement complete unless evidence exists.

---

# 57. SELF-EVALUATION SCORECARD

Use this internal scorecard.

Do NOT produce an artificial overall project score.

Instead report status by category:

```text
FOUNDATION
PASS / PARTIAL / FAIL

DOMAIN MODEL
PASS / PARTIAL / FAIL

EVENT PIPELINE
PASS / PARTIAL / FAIL

IRT
PASS / PARTIAL / FAIL

KNOWLEDGE GRAPH
PASS / PARTIAL / FAIL

KNOWLEDGE GAP DETECTION
PASS / PARTIAL / FAIL

ADAPTIVE LOCALIZATION
PASS / PARTIAL / FAIL

RECOMMENDATION ENGINE
PASS / PARTIAL / FAIL

JEV INTEGRATION
PASS / PARTIAL / FAIL

BROWSER EXTENSION
PASS / PARTIAL / FAIL

TESTING
PASS / PARTIAL / FAIL

EVALUATION
PASS / PARTIAL / FAIL

DOCUMENTATION
PASS / PARTIAL / FAIL

SECURITY
PASS / PARTIAL / FAIL

OBSERVABILITY
PASS / PARTIAL / FAIL
```

For every `PARTIAL` or `FAIL`, state:

```text
what is missing
why it matters
how it will be fixed
```

---

# 58. DEFINITION OF DONE

A phase is NOT complete merely because:

```text
files exist
code compiles
```

A phase is complete only when:

```text
implementation exists
+
tests exist
+
tests pass
+
documentation exists
+
architecture remains consistent
+
edge cases considered
+
self-evaluation completed
```

---

# 59. FINAL PROJECT DEFINITION OF DONE

Before declaring the project complete, verify:

## Architecture

* [ ] Extension separated from backend intelligence
* [ ] Backend separated into clear layers
* [ ] Model components separated
* [ ] JEV isolated as decision layer
* [ ] deterministic fallback exists

## Data

* [ ] raw events stored
* [ ] learner state stored
* [ ] concept state represented
* [ ] problem metadata represented
* [ ] prerequisite graph represented
* [ ] calibration information represented

## Intelligence

* [ ] IRT baseline implemented
* [ ] adaptive localization implemented
* [ ] concept mastery implemented
* [ ] knowledge-gap detection implemented
* [ ] candidate generation implemented
* [ ] recommendation engine implemented
* [ ] DKT extension point implemented/documented
* [ ] JEV integration implemented or explicitly isolated if unavailable

## Extension

* [ ] problem metadata extraction
* [ ] submission/event capture
* [ ] API communication
* [ ] recommendation display
* [ ] failure handling

## Testing

* [ ] unit tests
* [ ] integration tests
* [ ] API tests
* [ ] edge-case tests
* [ ] model invariant tests
* [ ] synthetic dataset tests
* [ ] end-to-end tests

## Evaluation

* [ ] baseline implemented
* [ ] metrics implemented
* [ ] evaluation dataset available
* [ ] ablation support exists
* [ ] limitations documented

## Documentation

* [ ] README
* [ ] architecture
* [ ] user flow
* [ ] data flow
* [ ] model flow
* [ ] API flow
* [ ] domain model
* [ ] IRT
* [ ] DKT
* [ ] adaptive localization
* [ ] knowledge graph
* [ ] recommendation
* [ ] JEV
* [ ] testing
* [ ] evaluation
* [ ] limitations
* [ ] future work
* [ ] self evaluation

---

# 60. IMPORTANT RESEARCH HONESTY RULE

Never fabricate:

```text
accuracy
AUC
learning gains
Jev performance
DKT performance
recommendation improvement
user studies
```

If no real dataset exists, report:

```text
not evaluated
```

or:

```text
evaluated on synthetic data
```

Never present synthetic results as real learner results.

Never claim that the system improves learning until the evaluation demonstrates it.

---

# 61. IMPORTANT ENGINEERING HONESTY RULE

Do not say:

```text
production ready
```

unless the necessary engineering requirements have actually been demonstrated.

Use:

```text
prototype
MVP
research prototype
experimental
```

where appropriate.

---

# 62. RESEARCH LIMITATIONS

Maintain a living limitations document.

Potential limitations include:

```text
limited learner data
cold start
selection bias
exposure bias
sparse interaction history
difficulty calibration uncertainty
concept tagging quality
prerequisite graph quality
DOM changes
browser extension fragility
model drift
Jev dependency
synthetic-data limitations
lack of controlled educational study
```

Add limitations discovered during implementation.

---

# 63. IMPORTANT DATA BIAS ISSUE

Adaptive recommendations create selection bias.

For example:

```text
beginners → mostly easy problems
experts → mostly hard problems
```

Therefore observed success rates cannot automatically be interpreted as intrinsic problem difficulty.

The system should support calibration items / controlled exploration where appropriate.

Document this explicitly.

---

# 64. MODEL INTERPRETATION

Distinguish:

```text
observed data
```

from:

```text
estimated latent state
```

For example:

```text
Observed:
3 failed linked-list attempts

Estimated:
linked-list mastery = 0.31
```

The second statement is a model estimate, not a directly observed fact.

---

# 65. RECOMMENDATION EXPLANATION

Every recommendation should expose:

```text
action
problem
concept
difficulty
reason
evidence
confidence/uncertainty
```

Example:

```text
Next action:
Solve a prerequisite linked-list problem.

Why:
Recent evidence indicates difficulty with linked-list fundamentals.

Evidence:
3 unsuccessful attempts
low estimated linked-list mastery
prerequisite concept below threshold

Estimated confidence:
0.78
```

---

# 66. EXTENSIBILITY

The architecture should allow future support for:

```text
HackerRank
Codeforces
CodeChef
Kaggle
corporate learning platforms
technical assessments
courses
quizzes
```

Do not hard-code the entire Knowledge Tracer around LeetCode.

Create an abstraction around:

```text
LearningPlatform
ProblemSource
EventSource
```

where this abstraction is actually justified.

Do not over-engineer the first implementation.

---

# 67. FUTURE EXTENSIONS

Document possible future modules:

```text
Redis
Kafka
Docker
Kubernetes
real-time event streaming
advanced DKT
transformer-based KT
knowledge graph embeddings
LLM-based feedback
multi-platform learning
organization-level analytics
teacher dashboard
```

These are future extensions unless explicitly required.

---

# 68. PERFORMANCE

Do not prematurely optimize.

First ensure correctness.

Then investigate:

```text
event ingestion throughput
database indexing
recommendation latency
model update latency
extension response time
```

Add indexes based on actual query patterns.

---

# 69. SECURITY / PRIVACY WARNING FOR LEETCODE INTEGRATION

Before implementing real scraping or browser-extension integration:

1. inspect current platform behavior
2. identify technically observable information
3. avoid collecting unnecessary content
4. keep the implementation modular
5. document platform-policy/legal considerations
6. do not assume that technically accessible DOM data is automatically permitted for every use

The architecture must allow the backend to work with synthetic/manual events even when the extension is unavailable.

---

# 70. CURSOR WORKFLOW

When beginning work:

## Step 1

Inspect the repository.

Report:

```text
current structure
existing files
existing technologies
existing implementation
missing components
potential conflicts
```

## Step 2

Create:

```text
docs/project-status.md
```

## Step 3

Create the architecture and domain documentation before large implementation.

## Step 4

Implement Phase 1.

## Step 5

Run tests.

## Step 6

Perform self-evaluation.

## Step 7

Only proceed to the next phase when the current phase is sufficiently complete.

---

# 71. DO NOT ASK UNNECESSARY QUESTIONS

If a reasonable engineering decision can be made:

```text
make the decision
document it
continue
```

Ask the user only when the decision materially changes:

```text
architecture
security
data model
research methodology
external service
destructive modification
```

Do not repeatedly ask for confirmation for routine implementation decisions.

---

# 72. WHEN SOMETHING FAILS

Do not hide failures.

Report:

```text
failure
root cause
affected component
attempted fix
remaining issue
```

Then continue with the next safe step where possible.

---

# 73. BEFORE EVERY MAJOR COMMIT

Perform:

```text
compile
unit tests
integration tests
API tests
architecture check
documentation check
git diff review
```

Do not create a commit automatically unless explicitly requested.

---

# 74. FINAL VERIFICATION COMMANDS

At the end, provide commands for:

```bash
./mvnw clean test
./mvnw verify
```

plus the appropriate commands for:

```text
extension tests
synthetic dataset generation
evaluation
database migration
application startup
```

Do not invent commands that do not exist.

---

# 75. FINAL REPORT

When the project reaches a stable milestone, produce:

```text
docs/final-report.md
```

Containing:

```text
1. Problem Statement
2. Motivation
3. System Architecture
4. Data Architecture
5. Knowledge Graph
6. IRT
7. Knowledge Tracing
8. Adaptive Localization
9. Recommendation Engine
10. Jev Decision Layer
11. Browser Extension
12. API Architecture
13. Database
14. Testing
15. Evaluation
16. Results
17. Limitations
18. Future Work
19. Setup Instructions
20. Known Issues
```

---

# 76. FINAL SELF-EVALUATION

Before declaring the project complete, answer these questions honestly:

```text
1. Can a raw learner event enter the system?

2. Can the system persist it?

3. Can learner state be updated?

4. Can the system estimate ability?

5. Can the system estimate concept mastery?

6. Can it identify a potential knowledge gap?

7. Can it identify prerequisite relationships?

8. Can it generate multiple candidate actions?

9. Can the decision layer choose among bounded actions?

10. Is there a deterministic fallback?

11. Can the system recommend another problem?

12. Can the learner's next interaction become new evidence?

13. Is the entire loop testable?

14. Can the system operate without the browser extension?

15. Can synthetic learners be generated?

16. Can the model be evaluated against baselines?

17. Can individual components be ablated?

18. Are uncertainty and limitations represented?

19. Are model estimates distinguished from observations?

20. Is every major architectural decision documented?
```

If any answer is:

```text
NO
```

do not declare the project complete.

---

# 77. MOST IMPORTANT SYSTEM LOOP

The finished project must preserve this loop:

```text
             ┌──────────────────────────┐
             │                          │
             ▼                          │
           USER                         │
             │                          │
             ▼                          │
      SOLVE / INTERACT                  │
             │                          │
             ▼                          │
       BROWSER EXTENSION                │
             │                          │
             ▼                          │
        RAW EVENT DATA                  │
             │                          │
             ▼                          │
      KNOWLEDGE TRACER                  │
             │                          │
       ┌─────┼─────────┐                │
       ▼     ▼         ▼                │
      IRT   GRAPH     KT                │
       │     │         │                │
       └─────┼─────────┘                │
             ▼                          │
       LEARNER STATE                    │
             │                          │
             ▼                          │
      KNOWLEDGE GAP                     │
             │                          │
             ▼                          │
     CANDIDATE ACTIONS                  │
             │                          │
             ▼                          │
            JEV                         │
             │                          │
             ▼                          │
       POLICY CHECK                     │
             │                          │
             ▼                          │
       RECOMMENDATION                   │
             │                          │
             └──────────────────────────┘
```

This feedback loop is the core product.

---

# 78. FINAL INSTRUCTION TO CURSOR

Build this project as an **evidence-driven research prototype**, not as a collection of disconnected CRUD endpoints.

Prioritize:

```text
correctness
traceability
testability
explainability
modularity
research validity
```

over:

```text
unnecessary features
premature microservices
large amounts of boilerplate
fake ML sophistication
fabricated evaluation results
```

At every stage ask:

> "What evidence does the system have, what does it estimate from that evidence, and how does that estimate affect the next action?"

Maintain that distinction throughout the codebase.

The final system should make the following cycle executable:

```text
OBSERVE
   ↓
STORE
   ↓
ESTIMATE
   ↓
TRACE
   ↓
LOCALIZE
   ↓
DETECT GAP
   ↓
GENERATE OPTIONS
   ↓
DECIDE
   ↓
RECOMMEND
   ↓
OBSERVE AGAIN
```

Do not declare success merely because the application starts.

Success means the complete learning-data → knowledge-state → decision → recommendation → new-evidence loop is implemented, tested, documented, and honestly evaluated.

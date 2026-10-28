# Knowledge Tracer — Python replica (`KT_python/`)

A complete Python port of the Knowledge Tracer backend: the same HTTP API, the same
models and the same maths as the Spring Boot service in [`../backend/`](../backend/),
rebuilt on **FastAPI + SQLAlchemy**. The package mirrors the Java architecture module
by module (`knowledge_tracer/irt.py` ports `irt.IrtService` + `irt.IrtModel`,
`knowledge_tracer/decision.py` ports the whole `decision` package, and so on), so the
two implementations can be read side by side. Each module docstring names the Java
classes it ports.

The wire contract is unchanged: same routes, same JSON shapes, same `ApiError` error
body, same status codes. The authoritative contract description is
[`../docs/api-flow.md`](../docs/api-flow.md) — clients written for the Java backend
(the browser extension, Postman collection, replay scripts) work against this port by
only changing the base URL.

**Deep documentation: [PROJECT.md](PROJECT.md)** — architecture, the module-by-module
Java→Python mapping, the learning loop, the models, testing and parity verification.

## Feature parity vs the Java backend

| Capability | Java backend (`backend/`) | KT_python |
|---|---|---|
| HTTP framework | Spring Boot 3.5 (Spring MVC) | FastAPI + uvicorn |
| Persistence | Spring Data JPA on PostgreSQL 17 | SQLAlchemy 2.0 on SQLite (any SQLAlchemy URL works) |
| Schema | Flyway migrations V1–V6 | `create_all` from the entities in `domain.py` |
| REST routes | 18 endpoints under `/api/**` | same routes, same request/response DTOs |
| Error shape | `ApiError` JSON (`GlobalExceptionHandler`) | same shape and status codes |
| Auth | `ApiKeyFilter`: `X-API-Key` on `/api/**` when `KT_API_KEY` is set, constant-time compare | same |
| Request correlation | `X-Request-Id` filter | same |
| Event ingestion | `POST /api/events`, idempotent by client `eventId` (200 replay / 409 conflict) | same (`ingestion.py`) |
| Learner state | IRT ability + uncertainty, calibration flag | same (`state.py`, `irt.py`) |
| IRT | Rasch 1PL, MAP estimates, first attempts only | same maths (`irt.py`) |
| Concept mastery | Beta-Bernoulli with quality-weighted evidence | same (`mastery.py`) |
| Knowledge graph | 25 concepts, 21 prerequisite edges, cycle-safe | same (`knowledge_graph.py`) |
| Gap detection | rule-based `GapReport` with evidence | same (`knowledge_gap.py`) |
| Adaptive localization | cold-start calibration items | same (`localization.py`) |
| Recommendation | candidate generation → policy validation → fallback, audit rows | same (`candidates.py`, `recommendation.py`) |
| Jev decision layer | deterministic / LinUCB bandit / HTTP providers behind one interface | same (`decision.py`) |
| DKT | LSTM (`kt-dkt-v1`) inference from the JSON export, off by default | same (`dkt.py`), numpy, **verified to 1e-9 against the recorded reference fixture** |
| Research exports | NDJSON `kt-events-v1` / `kt-decisions-v1`, HMAC pseudonyms | same (`research.py`, `pseudonyms.py`) |
| Catalog seed | Flyway V3/V5 | `scripts/seed_db.py` / the container loads the same `../data/seed/catalog_1000.json` |
| Dashboard | static `/dashboard.html` | ported, self-contained (`knowledge_tracer/static/dashboard.html`) |
| Health | Spring Actuator | `GET /actuator/health` → `{"status":"UP"}` |
| API docs | Swagger UI (springdoc) | FastAPI's built-in `/docs` |

## Layout

```
KT_python/
├── knowledge_tracer/            # the replica package (mirrors com.knowledgetracer)
│   ├── config.py                # feature flags + hyper-parameters (KT_* env vars)
│   ├── domain.py                # entities — one SQLAlchemy model per Flyway table
│   ├── db.py                    # engine / session / transaction helpers
│   ├── ingestion.py  state.py  irt.py  mastery.py  success.py
│   ├── knowledge_graph.py  knowledge_gap.py  localization.py
│   ├── candidates.py  recommendation.py  decision.py   # the recommendation pipeline + Jev
│   ├── dkt.py  research.py  pseudonyms.py  seed.py  errors.py  feedback.py
│   ├── services.py              # Learner/Problem/Concept CRUD services
│   ├── container.py             # the application context (wires everything)
│   ├── api/                     # FastAPI HTTP layer — one routes_*.py per Java controller
│   │                            #   (app factory: create_app, in api/app.py)
│   └── static/dashboard.html    # learner dashboard (self-contained, no CDN)
├── scripts/
│   ├── run_server.py            # uvicorn launcher (this is how you run the API)
│   ├── seed_db.py               # create the schema + load the 1000-problem catalog
│   ├── demo.py                  # end-to-end learning-loop demo, no server needed
│   ├── replay_synthetic.py      # replay the synthetic event stream against a running server
│   └── monitor_agents.py        # multi-agent build monitor (.agents/*.json)
├── tests/                       # pytest suite, 268 tests (PYTHONPATH=pylib:.)
├── requirements.txt             # fastapi, uvicorn, SQLAlchemy, numpy, pytest, httpx
├── pylib/                       # pip --target install location (already populated)
└── _attic/                      # superseded iterations from the multi-agent build, kept for reference
```

## Install

Dependencies are installed into the project-local `pylib/` directory (same convention
as `../model/`). **This is already done in this repo**; on a fresh checkout run:

```bash
cd KT_python
python3 -m pip install --target pylib -r requirements.txt
```

Tested with CPython 3.9.6 (macOS system Python); any Python ≥ 3.9 works.

## Run

```bash
cd KT_python
python3 scripts/run_server.py
```

Then open <http://127.0.0.1:8081/dashboard> — the learner dashboard (concept
mastery, knowledge graph, difficulty landscape, current recommendation with its
explanation, recommendation and event history). Enter a user id and press **Load**;
if the server has `KT_API_KEY` set, enter the same key in the settings bar. Base URL,
API key and user id are persisted in `localStorage`.

The launcher prepends `pylib/` and the project root to `sys.path` and runs `uvicorn`
against the app factory `knowledge_tracer.api.app:create_app`. Port **8081** is the
default so the Python port can run side by side with the Java backend on 8080. A fresh
database is created and seeded with the 1000-problem catalog automatically on first
start (`KT_DATABASE_URL`, default `sqlite:///./kt_python.db`).

### Try it without a server

```bash
cd KT_python
PYTHONPATH=pylib:. python3 scripts/demo.py
```

A simulated learner (hidden true ability 4.0) works through the real catalog for 14
rounds: watch the calibration phase step down/up, the ability estimate converge to
the true value, a concept gap get detected and remediated, and every recommendation
get rewarded by the observed outcome.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `KT_HOST` | `127.0.0.1` | bind host |
| `KT_PORT` | `8081` | bind port (Java backend uses 8080) |
| `KT_DATABASE_URL` | `sqlite:///./kt_python.db` (set by `run_server.py`) | SQLAlchemy database URL |
| `KT_API_KEY` | blank (auth disabled) | shared key required as `X-API-Key` on every `/api/**` request |
| `KT_CATALOG_PATH` | `../data/seed/catalog_1000.json` | catalog file for the seeder |
| `KT_IRT_ENABLED` / `KT_GRAPH_ENABLED` / `KT_LOCALIZATION_ENABLED` / `KT_JEV_ENABLED` | `true` | ablation toggles |
| `KT_DKT_ENABLED` | `false` | blend the LSTM DKT into P(success) estimates |
| `KT_JEV_PROVIDER` | `deterministic` | `deterministic` \| `bandit` \| `http` |
| `KT_RESEARCH_EXPORT` / `KT_PSEUDONYM_SALT` | `false` / `change-me` | research export guards |

## Test

```bash
cd KT_python
PYTHONPATH=pylib:. python3 -m pytest tests/ -q
```

**268 tests**, all against in-memory SQLite — no server, no network. The DKT suite
replays the recorded reference fixture from the Java side and matches it to 1e-9.

## API summary

Identical to the Java backend; see [`../docs/api-flow.md`](../docs/api-flow.md) for
request/response shapes, validation rules and error codes.

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/events` | Ingest one learning event; idempotent by client `eventId` (201 new / 200 replay / 409 conflict) |
| POST | `/api/learners` | Register a learner (idempotent; events also auto-provision) |
| GET | `/api/learners/{userId}/state` | Ability, uncertainty, calibration flag, per-concept Beta mastery |
| GET | `/api/learners/{userId}/next-action` | Run the full pipeline (gap → candidates → Jev → fallback); writes one audit row per call |
| GET | `/api/learners/{userId}/recommendations?limit=` | Recommendation audit trail, newest first |
| GET | `/api/learners/{userId}/history?limit=` | Raw stored events, newest first |
| POST | `/api/problems` | Register a problem with weighted concept tags |
| GET | `/api/problems` | List the whole catalog |
| GET | `/api/problems/{problemKey}` | One problem, with the calibrated difficulty estimate |
| POST | `/api/concepts` | Create a concept (`UPPER_SNAKE_CASE` code) |
| GET | `/api/concepts` | List concepts with direct + transitive prerequisites |
| GET | `/api/concepts/{code}` | One concept |
| POST | `/api/concepts/{code}/prerequisites` | Add a prerequisite edge (cycle-checked) |
| POST | `/api/feedback` | Learner self-report answering `next-action` feedback questions |
| GET | `/api/models/dkt` | DKT status (enabled / available / blend weight) |
| GET | `/api/models/jev` | Decision-layer status + per-arm LinUCB statistics |
| GET | `/api/research/events` | NDJSON export `kt-events-v1` (guarded by `KT_RESEARCH_EXPORT` + salt) |
| GET | `/api/research/catalog` | Catalog export in the seed-file schema (same guards) |
| GET | `/api/research/decisions?limit=` | NDJSON rewarded-decision log `kt-decisions-v1` (same guards) |
| GET | `/actuator/health` | Liveness probe `{"status":"UP"}` (open, mirrors the Java actuator) |
| GET | `/dashboard` | Learner dashboard (static file, no API key needed) |

**Auth.** When `KT_API_KEY` is non-blank, every `/api/**` request must send the same
value in the `X-API-Key` header (compared in constant time); a missing or wrong key
gets `401 {"error": "UNAUTHORIZED", ...}`. Blank disables the check for local
development. Only `/api/**` is guarded, exactly like the Java `ApiKeyFilter` —
`/actuator/health` and `/dashboard` are served without a key.

## Known deltas vs the Java backend

- **SQLite via SQLAlchemy `create_all` instead of PostgreSQL + Flyway.** The entities
  in `domain.py` are the schema (mirroring what V1–V6 produce); there are no
  migrations. `KT_DATABASE_URL` accepts any SQLAlchemy URL if you want PostgreSQL.
- **Single process.** One uvicorn worker, no embedded database server, no
  connection-pool tuning. Optimistic-locking behaviour under concurrent writes
  differs accordingly.
- **Actuator is reduced to `/actuator/health`**; there is no metrics endpoint.
- **Catalog seeding is a script / startup step, not a migration** (same source file
  the V3/V5 migrations were generated from).
- **No Swagger UI springdoc**; FastAPI serves its own interactive docs at `/docs`.
- **Dashboard has a configurable base URL** persisted in `localStorage` (the Java
  version used same-origin relative paths); everything else — panels, colours,
  layout — is a straight port.

## Multi-agent build record

This port was built by parallel sub-agents coordinated through status files in
`.agents/` (render them with `python3 scripts/monitor_agents.py`). The HTTP layer
was built twice in competing shapes (a single `api.py` module vs. an `api/`
package); the package won by converging to full compatibility with both test
suites and now ships. Earlier iterations of both shapes are preserved in
`_attic/` — see [PROJECT.md](PROJECT.md#build-record) for the full story.

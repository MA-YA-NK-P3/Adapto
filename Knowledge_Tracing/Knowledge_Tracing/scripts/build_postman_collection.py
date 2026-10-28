#!/usr/bin/env python3
"""Generate the Postman collection + environment for the Knowledge Tracer API.

Usage:  python3 scripts/build_postman_collection.py
Output: postman/KnowledgeTracer.postman_collection.json
        postman/KnowledgeTracer.local.postman_environment.json

Requests run top-to-bottom as one learner story: a fresh learner succeeds on arrays,
fails repeatedly on linked lists, and the system detects the gap and recommends
remediation. Every request carries pm.test assertions, so the collection doubles as
an API smoke test (Postman Collection Runner or `npx newman run ...`).
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "postman"


def tests(*lines):
    return [{"listen": "test", "script": {"type": "text/javascript", "exec": list(lines)}}]


def prerequest(*lines):
    return [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": list(lines)}}]


def request(name, method, path, body=None, test_lines=(), pre_lines=(), description=""):
    item = {
        "name": name,
        "request": {
            "method": method,
            "header": [
                {"key": "Content-Type", "value": "application/json"},
                {"key": "X-API-Key", "value": "{{apiKey}}", "disabled": False},
            ],
            "url": {
                "raw": "{{baseUrl}}" + path,
                "host": ["{{baseUrl}}"],
                "path": [p for p in path.split("?")[0].strip("/").split("/")],
            },
            "description": description,
        },
        "event": tests(*test_lines) + (prerequest(*pre_lines) if pre_lines else []),
    }
    if "?" in path:
        query = path.split("?", 1)[1]
        item["request"]["url"]["query"] = [
            {"key": kv.split("=")[0], "value": kv.split("=")[1]} for kv in query.split("&")
        ]
    if body is not None:
        item["request"]["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    return item


def status(code):
    return f"pm.test('status {code}', () => pm.response.to.have.status({code}));"


def event_body(problem, result, attempt=1, event_id="{{$guid}}", **extra):
    body = {
        "userId": "{{userId}}",
        "problemId": problem,
        "eventId": event_id,
        "timestamp": "{{$isoTimestamp}}",
        "attemptNumber": attempt,
        "result": result,
        "timeSpentSeconds": 900,
        "hintUsed": False,
        "solutionViewed": False,
    }
    body.update(extra)
    return body


ERROR_SHAPE = (
    "pm.test('consistent error body', () => {",
    "  const b = pm.response.json();",
    "  pm.expect(b).to.have.all.keys('timestamp','status','error','message','path','details');",
    "});",
)

health = {
    "name": "0. Health",
    "item": [
        request("Health", "GET", "/actuator/health",
                pre_lines=("// Each full run starts a brand-new learner.",
                           "pm.collectionVariables.set('userId', 'U-' + Math.random().toString(36).slice(2, 10));"),
                test_lines=(status(200), "pm.test('UP', () => pm.expect(pm.response.json().status).to.eql('UP'));")),
    ],
}

catalog = {
    "name": "1. Catalog (problems, concepts, prerequisite graph)",
    "item": [
        request("List problems", "GET", "/api/problems",
                test_lines=(status(200),
                            "pm.test('seeded pool present', () => pm.expect(pm.response.json().length).to.be.above(20));",
                            "pm.test('difficulty is continuous, label is prior', () => {",
                            "  const p = pm.response.json().find(x => x.problemId === 'LC21');",
                            "  pm.expect(p.staticDifficulty).to.eql('EASY');",
                            "  pm.expect(p.estimatedDifficulty).to.be.a('number');",
                            "  pm.expect(p.difficultyUncertainty).to.be.above(0);",
                            "});")),
        request("Get problem LC21", "GET", "/api/problems/LC21",
                test_lines=(status(200),
                            "pm.test('tagged with LINKED_LIST', () => pm.expect(pm.response.json().concepts).to.include('LINKED_LIST'));")),
        request("Get concept GRAPHS (transitive prerequisites)", "GET", "/api/concepts/GRAPHS",
                test_lines=(status(200),
                            "pm.test('transitive prerequisites', () => pm.expect(pm.response.json().allPrerequisites).to.include.members(['TREES','LINKED_LIST','RECURSION']));")),
        request("List concepts", "GET", "/api/concepts", test_lines=(status(200),)),
        request("Create concept", "POST", "/api/concepts",
                body={"code": "{{newConcept}}", "name": "Tries", "description": "Prefix trees"},
                pre_lines=("pm.collectionVariables.set('newConcept', 'TRIE_' + Math.floor(Math.random()*1e6));",),
                test_lines=(status(201),)),
        request("Add prerequisite (new concept requires TREES)", "POST", "/api/concepts/{{newConcept}}/prerequisites",
                body={"prerequisiteCode": "TREES"},
                test_lines=(status(201),
                            "pm.test('edge stored', () => pm.expect(pm.response.json().directPrerequisites).to.include('TREES'));")),
        request("Create problem", "POST", "/api/problems",
                body={"problemKey": "{{newProblem}}", "title": "Implement Trie (Prefix Tree)",
                      "url": "https://leetcode.com/problems/implement-trie-prefix-tree/",
                      "staticDifficulty": "MEDIUM",
                      "concepts": [{"conceptCode": "{{newConcept}}", "weight": 1.0},
                                   {"conceptCode": "HASHING", "weight": 0.5}]},
                pre_lines=("pm.collectionVariables.set('newProblem', 'LCP' + Math.floor(Math.random()*1e6));",),
                test_lines=(status(201),
                            "pm.test('prior difficulty from label', () => pm.expect(pm.response.json().estimatedDifficulty).to.eql(5));")),
    ],
}

loop = {
    "name": "2. Learning loop (observe -> estimate -> gap -> recommend)",
    "item": [
        request("Register learner (201 new / 200 existing)", "POST", "/api/learners",
                body={"externalRef": "{{userId}}"},
                test_lines=("pm.test('201 or 200', () => pm.expect(pm.response.code).to.be.oneOf([200,201]));",)),
        request("State: cold start", "GET", "/api/learners/{{userId}}/state",
                test_lines=(status(200),
                            "pm.test('prior state', () => {",
                            "  const s = pm.response.json();",
                            "  pm.expect(s.totalEvents).to.eql(0);",
                            "  pm.expect(s.calibrated).to.eql(false);",
                            "});")),
        request("Next action: cold start -> calibration", "GET", "/api/learners/{{userId}}/next-action",
                test_lines=(status(200),
                            "pm.test('calibration item', () => pm.expect(pm.response.json().recommendation.type).to.eql('CALIBRATION_PROBLEM'));")),
        request("Event: LC1 Two Sum ACCEPTED", "POST", "/api/events",
                body=event_body("LC1", "ACCEPTED", event_id="{{dupEventId}}", confidence=4, perceivedDifficulty=2),
                pre_lines=("pm.collectionVariables.set('dupEventId', pm.variables.replaceIn('{{$guid}}'));",),
                test_lines=(status(201),
                            "pm.test('not a duplicate', () => pm.expect(pm.response.json().duplicate).to.eql(false));")),
        request("Event: SAME eventId again (idempotent replay)", "POST", "/api/events",
                body=event_body("LC1", "ACCEPTED", event_id="{{dupEventId}}"),
                test_lines=(status(200),
                            "pm.test('flagged duplicate, state untouched', () => pm.expect(pm.response.json().duplicate).to.eql(true));")),
        request("Event: LC217 Contains Duplicate ACCEPTED", "POST", "/api/events",
                body=event_body("LC217", "ACCEPTED"), test_lines=(status(201),)),
        request("Event: LC21 Merge Two Sorted Lists WRONG_ANSWER (1)", "POST", "/api/events",
                body=event_body("LC21", "WRONG_ANSWER", 1, blocker="LINKED_LIST_CONCEPT", confidence=2),
                test_lines=(status(201),)),
        request("Event: LC21 WRONG_ANSWER (2, hint used)", "POST", "/api/events",
                body=event_body("LC21", "WRONG_ANSWER", 2, hintUsed=True, blocker="LINKED_LIST_CONCEPT"),
                test_lines=(status(201),)),
        request("Event: LC206 Reverse Linked List WRONG_ANSWER", "POST", "/api/events",
                body=event_body("LC206", "WRONG_ANSWER", 1, blocker="LINKED_LIST_CONCEPT"),
                test_lines=(status(201),)),
        request("State: after evidence", "GET", "/api/learners/{{userId}}/state",
                test_lines=(status(200),
                            "pm.test('5 events counted (duplicate ignored)', () => pm.expect(pm.response.json().totalEvents).to.eql(5));",
                            "pm.test('linked list weak, arrays strong', () => {",
                            "  const c = pm.response.json().concepts;",
                            "  pm.expect(c.LINKED_LIST.mastery).to.be.below(0.45);",
                            "  pm.expect(c.ARRAYS.mastery).to.be.above(0.6);",
                            "});")),
        request("Next action: gap detected -> remediation", "GET", "/api/learners/{{userId}}/next-action",
                test_lines=(status(200),
                            "const r = pm.response.json();",
                            "pm.test('concept gap on linked list', () => {",
                            "  pm.expect(r.gap.type).to.eql('CONCEPT_GAP');",
                            "  pm.expect(r.gap.concept).to.eql('LINKED_LIST');",
                            "});",
                            "pm.test('remediation recommended', () => {",
                            "  pm.expect(r.recommendation.type).to.eql('EASIER_PROBLEM');",
                            "  pm.expect(r.recommendation.topic).to.eql('LINKED_LIST');",
                            "  pm.expect(r.recommendation.evidence).to.not.be.empty;",
                            "});",
                            "pm.test('feedback requested', () => pm.expect(r.feedback.required).to.eql(true));",
                            "pm.collectionVariables.set('recommendedProblem', r.recommendation.problemId);",
                            "pm.collectionVariables.set('recommendationId', r.recommendation.recommendationId);")),
        request("Event: solve the recommended problem (new evidence)", "POST", "/api/events",
                body=event_body("{{recommendedProblem}}", "ACCEPTED", confidence=3),
                test_lines=(status(201),)),
        request("State: mastery recovers", "GET", "/api/learners/{{userId}}/state",
                test_lines=(status(200),
                            "pm.test('6 events', () => pm.expect(pm.response.json().totalEvents).to.eql(6));")),
        request("History (raw observations)", "GET", "/api/learners/{{userId}}/history?limit=50",
                test_lines=(status(200),
                            "pm.test('6 stored events', () => pm.expect(pm.response.json().length).to.eql(6));")),
        request("Recommendation audit trail", "GET", "/api/learners/{{userId}}/recommendations?limit=20",
                test_lines=(status(200),
                            "pm.test('decisions persisted', () => pm.expect(pm.response.json().length).to.be.at.least(2));")),
    ],
}

feedback = {
    "name": "3. Feedback",
    "item": [
        request("Submit feedback for recommendation", "POST", "/api/feedback",
                body={"userId": "{{userId}}", "problemId": "LC21", "recommendationId": "{{recommendationId}}",
                      "answers": [{"question": "What prevented you from solving the problem?",
                                   "answer": "Pointer updates when merging"},
                                  {"question": "Did you understand the prerequisite?", "answer": "Partly"}],
                      "freeText": "Recursion version was confusing"},
                test_lines=(status(201),
                            "pm.test('recorded', () => pm.expect(pm.response.json().status).to.eql('RECORDED'));")),
    ],
}

errors = {
    "name": "4. Error handling",
    "item": [
        request("400 validation error", "POST", "/api/events",
                body={"userId": "", "problemId": "LC1", "attemptNumber": 0},
                test_lines=(status(400), *ERROR_SHAPE,
                            "pm.test('field details', () => pm.expect(pm.response.json().details).to.not.be.empty);")),
        request("400 invalid enum value", "POST", "/api/events",
                body=event_body("LC1", "MAYBE"), test_lines=(status(400), *ERROR_SHAPE)),
        request("422 unknown problem", "POST", "/api/events",
                body=event_body("LC_DOES_NOT_EXIST", "ACCEPTED"), test_lines=(status(422), *ERROR_SHAPE)),
        request("404 unknown learner", "GET", "/api/learners/NO_SUCH_LEARNER/next-action",
                test_lines=(status(404), *ERROR_SHAPE)),
        request("409 duplicate problem", "POST", "/api/problems",
                body={"problemKey": "LC1", "title": "Two Sum", "staticDifficulty": "EASY"},
                test_lines=(status(409), *ERROR_SHAPE)),
        request("422 prerequisite cycle", "POST", "/api/concepts/LINKED_LIST/prerequisites",
                body={"prerequisiteCode": "GRAPHS"}, test_lines=(status(422), *ERROR_SHAPE)),
        request("405 wrong method", "GET", "/api/events", test_lines=(status(405), *ERROR_SHAPE)),
    ],
}

models = {
    "name": "5. Model status (DKT, Jev)",
    "item": [
        request("DKT status", "GET", "/api/models/dkt",
                description="DKT is off by default (KT_DKT_ENABLED=false). When enabled, trainedOn reports "
                            "whether the loaded model was trained on synthetic or real data (docs/dkt.md).",
                test_lines=(status(200),
                            "const d = pm.response.json();",
                            "pm.test('status fields', () => pm.expect(d).to.include.keys('enabled','available','status','modelPath','blendWeight','minHistory'));",
                            "pm.test('disabled means DISABLED', () => { if (!d.enabled) pm.expect(d.status).to.eql('DISABLED'); });",
                            "pm.test('loaded model is labelled', () => { if (d.trainedOn !== undefined) pm.expect(d.trainedOn).to.be.a('string'); });")),
        request("Jev status (provider + bandit arms)", "GET", "/api/models/jev",
                description="Active decision provider (KT_JEV_PROVIDER) and the LinUCB bandit's per-arm statistics (docs/jev.md).",
                test_lines=(status(200),
                            "const j = pm.response.json();",
                            "pm.test('known provider', () => {",
                            "  pm.expect(j.providers).to.have.members(['deterministic','bandit','http']);",
                            "  pm.expect(j.providers).to.include(j.provider);",
                            "});",
                            "pm.test('one bandit arm per action type', () => {",
                            "  const arms = j.bandit.arms.map(a => a.arm);",
                            "  pm.expect(arms).to.include.members(['CALIBRATION_PROBLEM','EASIER_PROBLEM','PREREQUISITE_REVIEW']);",
                            "});")),
    ],
}

EXPORT_GATE = (
    "// Disabled by default: 404 unless KT_RESEARCH_EXPORT=true, 422 while KT_PSEUDONYM_SALT is the default.",
    "pm.test('200, or 404/422 when export is off', () => pm.expect(pm.response.code).to.be.oneOf([200,404,422]));",
    "if (pm.response.code !== 200) {",
    "  pm.test('consistent error body', () => {",
    "    pm.expect(pm.response.json()).to.have.all.keys('timestamp','status','error','message','path','details');",
    "  });",
    "}",
)


def ndjson_checks(schema):
    return (
        "if (pm.response.code === 200) {",
        "  const rows = pm.response.text().split('\\n').filter(l => l.trim()).map(l => JSON.parse(l));",
        f"  pm.test('{schema} records', () => rows.forEach(r => pm.expect(r.schema).to.eql('{schema}')));",
        "  pm.test('learners are pseudonymised', () => rows.forEach(r => pm.expect(r.learnerId).to.not.eql(pm.collectionVariables.get('userId'))));",
        "}",
    )


research = {
    "name": "6. Research export (disabled by default)",
    "item": [
        request("Export catalog", "GET", "/api/research/catalog",
                description="Catalog in the data/seed/catalog_1000.json schema. Requires KT_RESEARCH_EXPORT=true "
                            "and a secret KT_PSEUDONYM_SALT (docs/privacy.md).",
                test_lines=(*EXPORT_GATE,
                            "if (pm.response.code === 200) {",
                            "  pm.test('concepts and problems', () => pm.expect(pm.response.json()).to.include.keys('concepts','problems'));",
                            "}")),
        request("Export events (kt-events-v1 NDJSON)", "GET", "/api/research/events",
                description="All events as kt-events-v1 JSON lines with keyed learner pseudonyms; the input of "
                            "model/dkt/train_dkt.py --real (see scripts/export_events.py).",
                test_lines=(*EXPORT_GATE, *ndjson_checks("kt-events-v1"))),
        request("Export rewarded decisions (kt-decisions-v1 NDJSON)", "GET", "/api/research/decisions?limit=100",
                description="Logged Jev decisions that have a reward, for offline policy evaluation.",
                test_lines=(*EXPORT_GATE, *ndjson_checks("kt-decisions-v1"))),
    ],
}

collection = {
    "info": {
        "name": "Knowledge Tracer API",
        "_postman_id": "6f6b2d8e-3c1a-4f55-9d0c-7a1e2b3c4d5e",
        "description": (
            "Research prototype API. Run folders in order (Collection Runner) - folder 2 tells one "
            "learner's story end to end. A fresh random userId is generated per run. "
            "Set apiKey in the environment only if the server runs with KT_API_KEY. See docs/api-flow.md."
        ),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "event": prerequest(
        "if (!pm.collectionVariables.get('userId')) {",
        "  pm.collectionVariables.set('userId', 'U-' + Math.random().toString(36).slice(2, 10));",
        "}",
    ),
    "variable": [
        {"key": "userId", "value": ""},
        {"key": "dupEventId", "value": ""},
        {"key": "recommendedProblem", "value": ""},
        {"key": "recommendationId", "value": ""},
        {"key": "newConcept", "value": ""},
        {"key": "newProblem", "value": ""},
    ],
    "item": [health, catalog, loop, feedback, errors, models, research],
}

environment = {
    "name": "Knowledge Tracer - local",
    "values": [
        {"key": "baseUrl", "value": "http://localhost:8080", "enabled": True},
        {"key": "apiKey", "value": "", "enabled": True},
    ],
    "_postman_variable_scope": "environment",
}

OUT.mkdir(exist_ok=True)
(OUT / "KnowledgeTracer.postman_collection.json").write_text(json.dumps(collection, indent=2) + "\n")
(OUT / "KnowledgeTracer.local.postman_environment.json").write_text(json.dumps(environment, indent=2) + "\n")
print(f"wrote {OUT / 'KnowledgeTracer.postman_collection.json'}")
print(f"wrote {OUT / 'KnowledgeTracer.local.postman_environment.json'}")

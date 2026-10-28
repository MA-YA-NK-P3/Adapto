#!/usr/bin/env python3
"""Reference Jev decision service for the backend's "http" provider (protocol kt-jev-v1).

Standard library only. Two strategies:
  heuristic (default)  transparent rules: remediate an evidenced gap, otherwise pick the
                       candidate whose predicted success is closest to the 0.65 target;
  llm (--llm)          asks an OpenAI-compatible chat-completions endpoint to choose; the
                       answer is validated and the heuristic is used if it is unusable.

The backend treats this service as untrusted: it may only return an index into the
candidate list it was sent, the PolicyValidator re-checks the choice, and any error or
timeout makes the backend use its deterministic fallback policy.

Request  (POST /decide)  {"protocol": "kt-jev-v1", "learner": {...}, "gap": {...},
                          "candidates": [{"index", "action", "problemKey", "conceptCode",
                          "difficulty", "predictedSuccess", "expectedValue", "reason"}], "rules": [...]}
Response                 {"chosenIndex": int, "confidence": float in [0,1], "rationale": str}

Run:
  python3 scripts/jev_llm_server.py --port 8090                       # heuristic
  JEV_API_KEY=secret python3 scripts/jev_llm_server.py                # require X-API-Key
  LLM_BASE_URL=https://api.openai.com/v1 LLM_API_KEY=... LLM_MODEL=gpt-4o-mini \\
      python3 scripts/jev_llm_server.py --llm                         # LLM-backed
  python3 scripts/jev_llm_server.py --self-test                       # offline checks
Backend:
  KT_JEV_PROVIDER=http  knowledge-tracer.jev.http.url=http://localhost:8090/decide
Privacy: the request contains a keyed learner pseudonym, estimates and public problem keys
only. In --llm mode that context leaves your machine; see docs/privacy.md before enabling it.
"""
import argparse
import hmac
import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL = "kt-jev-v1"
TARGET_SUCCESS = 0.65
MAX_BODY = 256 * 1024


def heuristic(req: dict) -> dict:
    candidates = req.get("candidates") or []
    if not candidates:
        raise ValueError("no candidates")
    gap = req.get("gap") or {}
    gap_type = gap.get("type")
    targets = [c for c in (gap.get("prerequisite"), gap.get("concept")) if c]

    if gap_type in ("PREREQUISITE_GAP", "CONCEPT_GAP") and targets:
        preferred = ["PREREQUISITE_REVIEW", "EASIER_PROBLEM"] if gap_type == "PREREQUISITE_GAP" \
            else ["EASIER_PROBLEM", "PREREQUISITE_REVIEW"]
        for action in preferred:
            for target in targets:
                for c in candidates:
                    if c.get("action") == action and c.get("conceptCode") == target:
                        conf = max(0.5, min(0.9, float(gap.get("confidence") or 0.6)))
                        return {"chosenIndex": c["index"], "confidence": round(conf, 3),
                                "rationale": f"{gap_type} on {target}: {action.lower().replace('_', ' ')}"}

    def distance(c):
        p = c.get("predictedSuccess")
        return abs((p if p is not None else 0.5) - TARGET_SUCCESS)

    best = min(candidates, key=lambda c: (distance(c), -(c.get("expectedValue") or 0.0), c["index"]))
    conf = max(0.4, min(0.85, 0.85 - distance(best)))
    p = best.get("predictedSuccess")
    return {"chosenIndex": best["index"], "confidence": round(conf, 3),
            "rationale": f"predicted success {p if p is not None else 'n/a'} closest to target {TARGET_SUCCESS}"}


def llm_choose(req: dict, timeout: float) -> dict:
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    system = ("You are Jev, the decision layer of an adaptive practice system. Choose exactly one "
              "candidate by its index, following the rules in the request. Reply with JSON only: "
              '{"chosenIndex": <int>, "confidence": <0..1>, "rationale": "<one sentence>"}')
    payload = {"model": model, "temperature": 0,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": json.dumps(req, separators=(",", ":"))}]}
    http_req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(),
                                      headers={"Content-Type": "application/json",
                                               "Authorization": "Bearer " + os.environ.get("LLM_API_KEY", "")})
    with urllib.request.urlopen(http_req, timeout=timeout) as resp:
        content = json.loads(resp.read())["choices"][0]["message"]["content"]
    return validate(json.loads(content), req)


def validate(answer: dict, req: dict) -> dict:
    n = len(req.get("candidates") or [])
    idx = answer.get("chosenIndex")
    if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < n:
        raise ValueError(f"invalid chosenIndex {idx!r} for {n} candidates")
    conf = answer.get("confidence", 0.5)
    conf = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else 0.5
    rationale = str(answer.get("rationale") or "LLM decision")[:500]
    return {"chosenIndex": idx, "confidence": conf, "rationale": rationale}


def decide(req: dict, use_llm: bool, timeout: float) -> dict:
    if req.get("protocol") != PROTOCOL:
        raise ValueError(f"unsupported protocol {req.get('protocol')!r}")
    if use_llm:
        try:
            out = llm_choose(req, timeout)
            out["rationale"] = "LLM: " + out["rationale"]
            return out
        except Exception as e:  # noqa: BLE001 — any LLM problem degrades to the heuristic
            out = heuristic(req)
            out["rationale"] = f"heuristic (LLM unavailable: {type(e).__name__}): " + out["rationale"]
            return out
    return heuristic(req)


def make_handler(use_llm: bool, timeout: float, api_key: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self._send(200, {"status": "UP", "protocol": PROTOCOL, "llm": use_llm})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/decide":
                return self._send(404, {"error": "not found"})
            if api_key and not hmac.compare_digest(self.headers.get("X-API-Key", ""), api_key):
                return self._send(401, {"error": "invalid API key"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._send(413 if length > MAX_BODY else 400, {"error": "bad body size"})
            try:
                req = json.loads(self.rfile.read(length))
                self._send(200, decide(req, use_llm, timeout))
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, {"error": str(e)})

        def log_message(self, fmt, *args):
            sys.stderr.write("jev %s\n" % (fmt % args))

    return Handler


SAMPLE = {
    "protocol": PROTOCOL,
    "learner": {"id": "L-0123456789abcdef0123456789abcdef", "ability": 4.2, "uncertainty": 0.8, "calibrated": True},
    "gap": {"type": "PREREQUISITE_GAP", "concept": "TREES", "prerequisite": "RECURSION", "confidence": 0.72,
            "evidence": ["TREES mastery 0.31", "RECURSION mastery 0.35"]},
    "candidates": [
        {"index": 0, "action": "SAME_LEVEL_PROBLEM", "problemKey": "LC102", "conceptCode": None,
         "difficulty": 4.9, "predictedSuccess": 0.63, "expectedValue": 0.9, "reason": "same level"},
        {"index": 1, "action": "EASIER_PROBLEM", "problemKey": "LC104", "conceptCode": "TREES",
         "difficulty": 2.6, "predictedSuccess": 0.81, "expectedValue": 0.75, "reason": "easier"},
        {"index": 2, "action": "PREREQUISITE_REVIEW", "problemKey": "LC70", "conceptCode": "RECURSION",
         "difficulty": 2.5, "predictedSuccess": 0.84, "expectedValue": 0.8, "reason": "prerequisite"},
    ],
}


def self_test():
    assert heuristic(SAMPLE)["chosenIndex"] == 2
    no_gap = dict(SAMPLE, gap={"type": "NONE"})
    assert heuristic(no_gap)["chosenIndex"] == 0
    concept = dict(SAMPLE, gap={"type": "CONCEPT_GAP", "concept": "TREES", "confidence": 0.8})
    assert heuristic(concept)["chosenIndex"] == 1
    for bad in ({"chosenIndex": 3}, {"chosenIndex": -1}, {"chosenIndex": "1"}, {"chosenIndex": True}, {}):
        try:
            validate(bad, SAMPLE)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    assert validate({"chosenIndex": 1, "confidence": 7}, SAMPLE)["confidence"] == 1.0
    try:
        decide(dict(SAMPLE, protocol="v0"), False, 1.0)
        raise AssertionError("accepted wrong protocol")
    except ValueError:
        pass
    print("self-test OK:", json.dumps(heuristic(SAMPLE)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--llm", action="store_true", help="use an OpenAI-compatible endpoint (LLM_BASE_URL)")
    ap.add_argument("--llm-timeout", type=float, default=1.5, help="seconds; keep below the backend timeout")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.llm and not os.environ.get("LLM_BASE_URL"):
        sys.exit("--llm needs LLM_BASE_URL (and usually LLM_API_KEY, LLM_MODEL)")
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(args.llm, args.llm_timeout, os.environ.get("JEV_API_KEY", "")))
    print(f"Jev {PROTOCOL} service on http://{args.host}:{args.port}/decide (llm={args.llm})")
    server.serve_forever()


if __name__ == "__main__":
    main()

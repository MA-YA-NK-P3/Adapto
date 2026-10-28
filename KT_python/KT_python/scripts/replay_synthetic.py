#!/usr/bin/env python3
"""Replay the synthetic event stream against a RUNNING Knowledge Tracer Python API.

Port of ../scripts/replay_synthetic.py (which targets the Java backend) for this
port: reads the synthetic kt-events-v1 stream (../data/synthetic/v2/events.jsonl.gz),
registers each learner (POST /api/learners) and replays the events in timestamp
order (POST /api/events), exactly as the browser extension would send them.
Every --sample-every events it samples GET /api/learners/{userId}/next-action, then
prints a JSON summary (events sent, accepted, conflicts, errors, recommendations
received).

    *** SYNTHETIC / DEVELOPMENT DATA — validates plumbing + estimator behaviour only ***

Usage (from anywhere):
    python3 scripts/replay_synthetic.py [--limit 1000] [--sample-every 25]

Environment:
    KT_BASE_URL   base URL of the running server (default http://127.0.0.1:8081)
    KT_API_KEY    when set, sent as the X-API-Key header on every request

stdlib + httpx only; pylib/ is put on sys.path the same way run_server.py does it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # KT_python/

# Make the pip --target dependencies importable no matter where the interpreter was
# launched from (pylib first, then the project itself) — same as run_server.py.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pylib"))

DEFAULT_EVENTS_FILE = ROOT.parent / "data" / "synthetic" / "v2" / "events.jsonl.gz"


def iter_events(path: Path):
    """Yield parsed kt-events-v1 rows from a .jsonl or .jsonl.gz file."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def event_payload(row: dict) -> dict:
    """Map a kt-events-v1 row onto the POST /api/events request shape (event schema
    v1): learnerId -> userId; the truth/schema/seq/correct fields stay on disk."""
    payload = {
        "userId": row["learnerId"],
        "problemId": row["problemId"],
        "eventId": row["eventId"],
        "timestamp": row["timestamp"],
        "attemptNumber": row["attemptNumber"],
        "result": row["result"],
    }
    for key in ("timeSpentSeconds", "hintUsed", "solutionViewed", "sessionId"):
        if row.get(key) is not None:
            payload[key] = row[key]
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url",
                    default=os.environ.get("KT_BASE_URL", "http://127.0.0.1:8081"))
    ap.add_argument("--api-key", default=os.environ.get("KT_API_KEY"))
    ap.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_FILE)
    ap.add_argument("--limit", type=int, default=0,
                    help="replay at most N events (0 = the whole file)")
    ap.add_argument("--sample-every", type=int, default=50,
                    help="sample next-action every N events (0 = never)")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    import httpx  # from pylib/ (see the sys.path setup above)

    rows = []
    for row in iter_events(args.events_file):
        rows.append(row)
        if args.limit and len(rows) >= args.limit:
            break
    # The file is already in generation order; sort so replay is in timestamp order
    # even for a truncated/shuffled input (seq breaks ties deterministically).
    rows.sort(key=lambda r: (r["timestamp"], r.get("seq", 0)))

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    started = time.monotonic()
    sent = accepted = duplicates = conflicts = errors = 0
    recommendations = 0
    learners_seen = set()
    error_samples = []

    with httpx.Client(base_url=args.base_url, headers=headers,
                      timeout=args.timeout) as client:
        for row in rows:
            user = row["learnerId"]
            if user not in learners_seen:
                learners_seen.add(user)
                try:
                    r = client.post("/api/learners", json={"externalRef": user})
                    if r.status_code not in (200, 201):
                        errors += 1
                        error_samples.append({"learner": user,
                                              "status": r.status_code,
                                              "body": r.text[:200]})
                except httpx.HTTPError as exc:
                    errors += 1
                    error_samples.append({"learner": user, "error": str(exc)})

            try:
                r = client.post("/api/events", json=event_payload(row))
                sent += 1
                if r.status_code == 201:
                    accepted += 1
                elif r.status_code == 200:
                    duplicates += 1
                elif r.status_code == 409:
                    conflicts += 1
                else:
                    errors += 1
                    if len(error_samples) < 5:
                        error_samples.append({"eventId": row["eventId"],
                                              "status": r.status_code,
                                              "body": r.text[:200]})
            except httpx.HTTPError as exc:
                sent += 1
                errors += 1
                if len(error_samples) < 5:
                    error_samples.append({"eventId": row["eventId"],
                                          "error": str(exc)})

            if args.sample_every and sent % args.sample_every == 0:
                try:
                    r = client.get("/api/learners/%s/next-action" % user)
                    if r.status_code == 200:
                        recommendations += 1
                except httpx.HTTPError:
                    pass  # sampling is best-effort; event replay is the proof

            if sent % 10000 == 0:
                print("... %d events replayed (%d learners)"
                      % (sent, len(learners_seen)), file=sys.stderr, flush=True)

    summary = {
        "banner": "SYNTHETIC / DEVELOPMENT DATA",
        "baseUrl": args.base_url,
        "eventsFile": str(args.events_file),
        "learners": len(learners_seen),
        "eventsSent": sent,
        "accepted": accepted,
        "duplicates": duplicates,
        "conflicts": conflicts,
        "errors": errors,
        "recommendationsReceived": recommendations,
        "elapsedSeconds": round(time.monotonic() - started, 2),
    }
    if error_samples:
        summary["errorSamples"] = error_samples
    print(json.dumps(summary, indent=2))
    if errors:
        sys.exit("replay completed with %d error(s)" % errors)


if __name__ == "__main__":
    main()

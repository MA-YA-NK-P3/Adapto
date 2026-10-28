#!/usr/bin/env python3
"""Download real interaction data from a running backend for DKT training / evaluation.

Requires the backend to run with KT_RESEARCH_EXPORT=true (and a non-default
KT_PSEUDONYM_SALT). Learner ids in the export are keyed pseudonyms; no external refs.

  python3 scripts/export_events.py --base http://localhost:8080 [--api-key KEY] [--out data/real]

Writes <out>/events.jsonl.gz (kt-events-v1), <out>/catalog.json and <out>/decisions.jsonl.gz, then:
  PYTHONPATH=model/pylib python3 model/dkt/train_dkt.py --real \\
      --events data/real/events.jsonl.gz --catalog data/real/catalog.json
"""
import argparse
import gzip
import json
import pathlib
import shutil
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model"))

from kt import events as ev  # noqa: E402


def fetch(url: str, api_key: str, timeout: float):
    req = urllib.request.Request(url, headers={"X-API-Key": api_key} if api_key else {})
    return urllib.request.urlopen(req, timeout=timeout)


def download(url, api_key, target: pathlib.Path, timeout):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    with fetch(url, api_key, timeout) as resp, gzip.open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(target)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--out", default=str(ROOT / "data" / "real"))
    ap.add_argument("--decisions-limit", type=int, default=100000)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    base = args.base.rstrip("/")
    try:
        with fetch(base + "/api/research/catalog", args.api_key, args.timeout) as resp:
            catalog = json.loads(resp.read())
        out.mkdir(parents=True, exist_ok=True)
        (out / "catalog.json").write_text(json.dumps(catalog, indent=1) + "\n")
        download(base + "/api/research/events", args.api_key, out / "events.jsonl.gz", args.timeout)
        download(f"{base}/api/research/decisions?limit={args.decisions_limit}", args.api_key,
                 out / "decisions.jsonl.gz", args.timeout)
    except urllib.error.HTTPError as e:
        hint = " (is KT_RESEARCH_EXPORT=true?)" if e.code == 404 else " (API key?)" if e.code == 401 else ""
        sys.exit(f"export failed: HTTP {e.code} {e.reason}{hint}")
    except urllib.error.URLError as e:
        sys.exit(f"backend not reachable at {base}: {e.reason}")

    n, learners, correct = 0, set(), 0
    for r in ev.read_jsonl(out / "events.jsonl.gz"):
        ev.validate(r)
        n += 1
        learners.add(r["learnerId"])
        correct += r["correct"]
    decisions = sum(1 for _ in ev.read_jsonl(out / "decisions.jsonl.gz"))
    print(f"catalog: {len(catalog['problems'])} problems, {len(catalog['concepts'])} concepts")
    print(f"events:  {n} from {len(learners)} learners (correct rate {correct / n:.3f})" if n else "events: 0")
    print(f"decisions: {decisions}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

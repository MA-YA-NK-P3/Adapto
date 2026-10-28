#!/usr/bin/env python3
"""Export the problem pool + concept graph from a running backend to data/seed/catalog.json.

The backend's Flyway seed (V3__seed_dev_data.sql) is the source of truth; this snapshot
lets the offline Python evaluation run without a server. Re-run after changing the seed.

Usage: python3 scripts/export_catalog.py [--base-url http://localhost:8080]
"""
import argparse
import json
import pathlib
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8080")
    args = ap.parse_args()

    problems = [p for p in get(args.base_url, "/api/problems") if p["problemId"].startswith("LC")
                and p["problemId"][2:].isdigit()]
    concepts = get(args.base_url, "/api/concepts")
    seeded = {p["problemId"] for p in problems}

    catalog = {
        "_note": "Snapshot of the backend seed catalog (V3__seed_dev_data.sql). Static labels are priors.",
        "concepts": [{"code": c["code"], "name": c["name"], "prerequisites": c["directPrerequisites"]}
                     for c in concepts if c["code"].isupper() and not c["code"].startswith(("TRIE_", "CA_", "CB_"))],
        "problems": [],
    }
    for p in sorted(problems, key=lambda x: int(x["problemId"][2:])):
        detail = get(args.base_url, "/api/problems/" + p["problemId"])
        catalog["problems"].append({
            "problemId": p["problemId"],
            "title": p["title"],
            "staticDifficulty": p["staticDifficulty"],
            "concepts": [t["code"] for t in detail["conceptTags"]],
            "conceptWeights": {t["code"]: t["weight"] for t in detail["conceptTags"]},
        })
    out = ROOT / "data" / "seed" / "catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog, indent=2) + "\n")
    print(f"wrote {out} ({len(catalog['concepts'])} concepts, {len(seeded)} problems)")


if __name__ == "__main__":
    main()

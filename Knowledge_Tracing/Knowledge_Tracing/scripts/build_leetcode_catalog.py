#!/usr/bin/env python3
"""Build the 1000-problem LeetCode catalog used by the backend, the simulator and DKT.

Source: public LeetCode metadata mirror (GitHub, noworneverev/leetcode-api, 4068 problems
with official difficulty, topic tags, acceptance statistics, likes and paid-only flag).
No problem statements are stored — only metadata.

Selection rule (deterministic):
  1. category "Algorithms", not paid-only, at least one topic tag that maps to a concept;
  2. the 28 curated seed problems (V3 migration) are always included and keep their
     hand-curated concept weights (data/seed/catalog.json == V3), so the database, the
     simulator and DKT all see identical weights;
  3. the remaining slots are filled by the most-liked problems (ties: lower frontend id).

Outputs (all regenerable):
  data/leetcode/leetcode_all_metadata.json          lightweight metadata of ALL problems
  data/leetcode/leetcode_problems_1000.json          selected catalog with concept mapping
  data/leetcode/leetcode_problems_1000.csv           same, spreadsheet-friendly
  data/leetcode/tag_concept_map.json                 LeetCode tag -> concept rules (shared with extension)
  data/seed/catalog_1000.json                        simulator/DKT catalog (same schema as catalog.json)
  backend/src/main/resources/db/migration/V5__leetcode_catalog_1000.sql

Usage: python3 scripts/build_leetcode_catalog.py [--raw PATH] [--size 1000]
"""
import argparse
import csv
import json
import math
import pathlib
import statistics
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW_URL = "https://raw.githubusercontent.com/noworneverev/leetcode-api/main/data/leetcode_questions.json"
RAW_DEFAULT = ROOT / "data" / "leetcode" / "raw" / "noworneverev_leetcode_questions.json"
OUT_DIR = ROOT / "data" / "leetcode"
MIGRATION = ROOT / "backend" / "src" / "main" / "resources" / "db" / "migration" / "V5__leetcode_catalog_1000.sql"

SEED_KEYS = ["LC1", "LC3", "LC15", "LC19", "LC20", "LC21", "LC23", "LC26", "LC53", "LC70", "LC88", "LC98",
             "LC102", "LC104", "LC133", "LC141", "LC198", "LC200", "LC206", "LC207", "LC209", "LC215",
             "LC217", "LC226", "LC242", "LC295", "LC322", "LC704"]

# ---------------------------------------------------------------- concept graph
# code -> (name, description, prerequisites). The first 15 exist since V3; V4 adds the rest.
CONCEPTS = {
    "ARRAYS": ("Arrays", "Index-based sequential storage and traversal", []),
    "HASHING": ("Hashing", "Hash maps/sets for O(1) lookup", ["ARRAYS"]),
    "TWO_POINTERS": ("Two Pointers", "Two-index sweep techniques", ["ARRAYS", "SORTING"]),
    "SLIDING_WINDOW": ("Sliding Window", "Contiguous window expansion/contraction", ["TWO_POINTERS"]),
    "SORTING": ("Sorting", "Ordering algorithms and sorted-invariant exploitation", []),
    "BINARY_SEARCH": ("Binary Search", "Logarithmic search on sorted/monotonic spaces", ["SORTING"]),
    "LINKED_LIST": ("Linked List", "Pointer-based sequential structures", []),
    "STACKS_QUEUES": ("Stacks & Queues", "LIFO/FIFO structures", []),
    "RECURSION": ("Recursion", "Self-referential decomposition", []),
    "TREES": ("Trees", "Hierarchical structures, DFS/BFS traversals", ["LINKED_LIST", "RECURSION"]),
    "GRAPHS": ("Graphs", "General vertex-edge structures and traversal", ["TREES"]),
    "DYNAMIC_PROGRAMMING": ("Dynamic Programming", "Overlapping subproblems + optimal substructure", ["RECURSION"]),
    "BACKTRACKING": ("Backtracking", "Search with constraint pruning", ["RECURSION"]),
    "GREEDY": ("Greedy", "Locally optimal choice strategies", []),
    "HEAP": ("Heap / Priority Queue", "Partial-order structures for top-k problems", []),
    # --- added by V4 (1000-problem catalog) ---
    "STRINGS": ("Strings", "Character sequences, parsing and string matching", ["ARRAYS"]),
    "MATH": ("Math", "Arithmetic, number theory, combinatorics and geometry", []),
    "BIT_MANIPULATION": ("Bit Manipulation", "Bitwise operators and bitmask state", ["MATH"]),
    "PREFIX_SUM": ("Prefix Sum", "Cumulative sums for O(1) range aggregates", ["ARRAYS"]),
    "MATRIX": ("Matrix", "2-D grid traversal and manipulation", ["ARRAYS"]),
    "MONOTONIC_STACK": ("Monotonic Stack / Queue", "Stacks/deques kept in sorted order", ["STACKS_QUEUES"]),
    "UNION_FIND": ("Union-Find", "Disjoint-set union for connectivity", ["GRAPHS"]),
    "TRIE": ("Trie", "Prefix trees over strings", ["TREES", "STRINGS"]),
    "SEGMENT_TREE": ("Segment Tree / Fenwick Tree", "Range-query trees and ordered sets", ["TREES", "PREFIX_SUM"]),
    "DESIGN": ("Design", "Data-structure and API design problems", ["HASHING"]),
}
NEW_CONCEPTS = list(CONCEPTS)[15:]

# ---------------------------------------------------------------- tag mapping
# Tier weights: A = core technique, B = supporting technique, C = generic container.
A, B, C = 1.0, 0.7, 0.5
TAG_RULES = {
    "Array": ("ARRAYS", C),
    "Hash Table": ("HASHING", B), "Counting": ("HASHING", B), "Hash Function": ("HASHING", B),
    "String": ("STRINGS", C), "String Matching": ("STRINGS", A), "Rolling Hash": ("STRINGS", A),
    "Knuth–Morris–Pratt Algorithm": ("STRINGS", A), "Z Algorithm": ("STRINGS", A),
    "Boyer–Moore String-Search Algorithm": ("STRINGS", A), "Suffix Array": ("STRINGS", A),
    "Math": ("MATH", C), "Number Theory": ("MATH", B), "Combinatorics": ("MATH", B), "Geometry": ("MATH", B),
    "Greatest Common Divisor": ("MATH", B), "Euclidean Algorithm": ("MATH", B),
    "Prime Factorization": ("MATH", B), "Primality Test": ("MATH", B), "Sieve Theory": ("MATH", B),
    "Prime Number Sieve": ("MATH", B), "Least Common Multiple": ("MATH", B),
    "Fermat's Little Theorem": ("MATH", B), "Probability and Statistics": ("MATH", B),
    "Dynamic Programming": ("DYNAMIC_PROGRAMMING", A), "Memoization": ("DYNAMIC_PROGRAMMING", A),
    "Knapsack Problem": ("DYNAMIC_PROGRAMMING", A), "0-1 Knapsack": ("DYNAMIC_PROGRAMMING", A),
    "Complete Knapsack": ("DYNAMIC_PROGRAMMING", A),
    "Longest Increasing Subsequence": ("DYNAMIC_PROGRAMMING", A), "DP on Trees": ("DYNAMIC_PROGRAMMING", A),
    "Sorting": ("SORTING", B), "Merge Sort": ("SORTING", B), "Quicksort": ("SORTING", B),
    "Counting Sort": ("SORTING", B), "Quickselect": ("SORTING", B), "Bucket Sort": ("SORTING", B),
    "Radix Sort": ("SORTING", B),
    "Greedy": ("GREEDY", A),
    "Binary Search": ("BINARY_SEARCH", A),
    "Bit Manipulation": ("BIT_MANIPULATION", A), "Bitmask": ("BIT_MANIPULATION", A),
    "Prefix Sum": ("PREFIX_SUM", A),
    "Matrix": ("MATRIX", B),
    "Two Pointers": ("TWO_POINTERS", A), "Floyd's Cycle Finding Algorithm": ("TWO_POINTERS", A),
    "Tree": ("TREES", A), "Binary Tree": ("TREES", A), "Binary Search Tree": ("TREES", A),
    "Binary Lifting": ("TREES", A),
    "Heap (Priority Queue)": ("HEAP", A),
    "Graph Theory": ("GRAPHS", A), "Graph": ("GRAPHS", A), "Shortest Path": ("GRAPHS", A),
    "Dijkstra's Algorithm": ("GRAPHS", A), "Topological Sort": ("GRAPHS", A),
    "Directed Acyclic Graph": ("GRAPHS", A), "Bipartite Graph": ("GRAPHS", A),
    "Minimum Spanning Tree": ("GRAPHS", A), "Eulerian Circuit": ("GRAPHS", A),
    "Strongly Connected Component": ("GRAPHS", A), "Biconnected Component": ("GRAPHS", A),
    "Stack": ("STACKS_QUEUES", A), "Queue": ("STACKS_QUEUES", A), "Bracket Sequences": ("STACKS_QUEUES", A),
    "Monotonic Stack": ("MONOTONIC_STACK", A), "Monotonic Queue": ("MONOTONIC_STACK", A),
    "Sliding Window": ("SLIDING_WINDOW", A),
    "Design": ("DESIGN", A), "Data Stream": ("DESIGN", A), "Iterator": ("DESIGN", A),
    "Backtracking": ("BACKTRACKING", A),
    "Union-Find": ("UNION_FIND", A),
    "Segment Tree": ("SEGMENT_TREE", A), "Binary Indexed Tree": ("SEGMENT_TREE", A),
    "Ordered Set": ("SEGMENT_TREE", A),
    "Linked List": ("LINKED_LIST", A), "Doubly-Linked List": ("LINKED_LIST", A),
    "Recursion": ("RECURSION", A), "Divide and Conquer": ("RECURSION", B),
    "Trie": ("TRIE", A),
}
TREE_TAGS = {"Tree", "Binary Tree", "Binary Search Tree", "DP on Trees", "Binary Lifting"}
GRAPH_CONTEXT_TAGS = {"Graph Theory", "Graph", "Matrix", "Union-Find", "Shortest Path", "Topological Sort",
                      "Directed Acyclic Graph", "Bipartite Graph", "Minimum Spanning Tree"}
MAX_CONCEPTS_PER_PROBLEM = 4

# Difficulty prior (0-10 scale): label base shifted by acceptance rate WITHIN the label.
LABEL_BASE = {"EASY": 3.0, "MEDIUM": 5.0, "HARD": 7.0}
ACCEPTANCE_SHIFT = 0.6       # prior moves 0.6 per within-label SD of logit(acceptance)
ACCEPTANCE_MAX_SHIFT = 1.2   # never leaves [base - 1.2, base + 1.2]


def map_tags(tags):
    """LeetCode topic tags -> {concept: weight}, strongest weight normalized to 1.0."""
    tagset = set(tags)
    weights = {}

    def put(code, w):
        weights[code] = max(weights.get(code, 0.0), w)

    for t in tags:
        if t in ("Depth-First Search", "Breadth-First Search"):
            if tagset & TREE_TAGS:
                put("TREES", A)
            elif tagset & GRAPH_CONTEXT_TAGS:
                put("GRAPHS", A)
            elif t == "Depth-First Search":
                put("RECURSION", B)
            else:
                put("GRAPHS", B)
        elif t in TAG_RULES:
            code, w = TAG_RULES[t]
            put(code, w)
        if t == "Memoization":
            put("RECURSION", B)
    if not weights:
        return {}
    top = max(weights.values())
    ranked = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_CONCEPTS_PER_PROBLEM]
    return {c: round(w / top, 2) for c, w in ranked}


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def load_raw(path: pathlib.Path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {RAW_URL}")
        urllib.request.urlretrieve(RAW_URL, path)
    return [x["data"]["question"] for x in json.loads(path.read_text())]


def normalize(q):
    stats = json.loads(q.get("stats") or "{}")
    acc, sub = stats.get("totalAcceptedRaw") or 0, stats.get("totalSubmissionRaw") or 0
    tags = [t["name"] for t in (q.get("topicTags") or [])]
    fid = int(q["questionFrontendId"])
    slug = q["url"].rstrip("/").rsplit("/", 1)[-1]
    return {
        "problemKey": f"LC{fid}",
        "frontendId": fid,
        "title": q["title"],
        "slug": slug,
        "url": f"https://leetcode.com/problems/{slug}/",
        "difficulty": q["difficulty"].upper(),
        "acceptanceRate": round(acc / sub, 4) if sub else None,
        "totalAccepted": acc,
        "totalSubmissions": sub,
        "likes": q.get("likes") or 0,
        "dislikes": q.get("dislikes") or 0,
        "paidOnly": bool(q.get("isPaidOnly")),
        "category": q.get("categoryTitle"),
        "topicTags": tags,
        "hintCount": len(q.get("hints") or []),
        "hasSolution": bool(q.get("hasSolution")),
    }


def acceptance_stats(problems):
    """Per-label mean/sd of logit(acceptance) over all free algorithm problems."""
    out = {}
    for label in LABEL_BASE:
        xs = [logit(p["acceptanceRate"]) for p in problems
              if p["difficulty"] == label and p["acceptanceRate"] and not p["paidOnly"]
              and p["category"] == "Algorithms"]
        out[label] = {"mean": statistics.fmean(xs), "sd": statistics.pstdev(xs), "n": len(xs)}
    return out


def prior_difficulty(p, stats):
    base = LABEL_BASE[p["difficulty"]]
    if not p["acceptanceRate"]:
        return base
    s = stats[p["difficulty"]]
    z = (logit(p["acceptanceRate"]) - s["mean"]) / s["sd"]
    shift = max(-ACCEPTANCE_MAX_SHIFT, min(ACCEPTANCE_MAX_SHIFT, -ACCEPTANCE_SHIFT * z))
    return round(min(9.5, max(0.5, base + shift)), 2)


def select(problems, size):
    eligible = [p for p in problems if p["category"] == "Algorithms" and not p["paidOnly"] and p["concepts"]]
    by_key = {p["problemKey"]: p for p in eligible}
    missing = [k for k in SEED_KEYS if k not in by_key]
    if missing:
        sys.exit(f"seed problems not eligible: {missing}")
    chosen = {k: by_key[k] for k in SEED_KEYS}
    for p in sorted(eligible, key=lambda p: (-p["likes"], p["frontendId"])):
        if len(chosen) >= size:
            break
        chosen.setdefault(p["problemKey"], p)
    return sorted(chosen.values(), key=lambda p: p["frontendId"])


def sql_str(s):
    return "'" + str(s).replace("'", "''") + "'"


def write_migration(selected):
    lines = [
        "-- V5: 1000-problem LeetCode catalog (GENERATED by scripts/build_leetcode_catalog.py — do not edit).",
        "-- Metadata only (no statements). Source: public LeetCode metadata mirror; see docs/data.md.",
        "-- The 28 V3 seed problems keep their curated concept tags; their metadata and",
        "-- acceptance-adjusted prior are refreshed. Other problems are inserted with generated tags.",
        "",
        "create temp table _lc (problem_key text, frontend_id int, title text, slug text, url text,",
        "    static_difficulty text, prior double precision, acceptance double precision, likes int,",
        "    topic_tags text);",
        "insert into _lc values",
    ]
    rows = []
    for p in selected:
        acc = "null" if p["acceptanceRate"] is None else repr(p["acceptanceRate"])
        rows.append(f"    ({sql_str(p['problemKey'])}, {p['frontendId']}, {sql_str(p['title'])}, "
                    f"{sql_str(p['slug'])}, {sql_str(p['url'])}, {sql_str(p['difficulty'])}, "
                    f"{p['priorDifficulty']}, {acc}, {p['likes']}, {sql_str(', '.join(p['topicTags'])[:1000])})")
    lines.append(",\n".join(rows) + ";")
    lines += [
        "",
        "update problems p set frontend_id = l.frontend_id, slug = l.slug, acceptance_rate = l.acceptance,",
        "    likes = l.likes, topic_tags = l.topic_tags, prior_difficulty = l.prior,",
        "    estimated_difficulty = case when p.sample_size = 0 then l.prior else p.estimated_difficulty end,",
        "    updated_at = now()",
        "from _lc l where p.problem_key = l.problem_key;",
        "",
        "insert into problems (id, problem_key, title, url, static_difficulty, estimated_difficulty,",
        "    difficulty_uncertainty, sample_size, source, frontend_id, slug, acceptance_rate, likes,",
        "    paid_only, topic_tags, prior_difficulty)",
        "select gen_random_uuid(), l.problem_key, l.title, l.url, l.static_difficulty, l.prior, 1.5, 0,",
        "    'LEETCODE', l.frontend_id, l.slug, l.acceptance, l.likes, false, l.topic_tags, l.prior",
        "from _lc l",
        "where not exists (select 1 from problems p where p.problem_key = l.problem_key);",
        "",
        "create temp table _lct (problem_key text, concept_code text, weight double precision);",
        "insert into _lct values",
    ]
    tag_rows = [f"    ({sql_str(p['problemKey'])}, {sql_str(c)}, {w})"
                for p in selected for c, w in p["concepts"].items()]
    lines.append(",\n".join(tag_rows) + ";")
    lines += [
        "",
        "insert into problem_concepts (problem_id, concept_id, weight)",
        "select p.id, c.id, t.weight",
        "from _lct t",
        "join problems p on p.problem_key = t.problem_key",
        "join concepts c on c.code = t.concept_code",
        "where not exists (select 1 from problem_concepts pc where pc.problem_id = p.id);",
        "",
        "drop table _lct;",
        "drop table _lc;",
        "",
    ]
    MIGRATION.write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(RAW_DEFAULT))
    ap.add_argument("--size", type=int, default=1000)
    args = ap.parse_args()

    problems = [normalize(q) for q in load_raw(pathlib.Path(args.raw))]
    stats = acceptance_stats(problems)
    for p in problems:
        p["concepts"] = map_tags(p["topicTags"])
        p["priorDifficulty"] = prior_difficulty(p, stats)
    selected = select(problems, args.size)
    curated = {p["problemId"]: p["conceptWeights"]
               for p in json.loads((ROOT / "data" / "seed" / "catalog.json").read_text())["problems"]}
    for p in selected:
        if p["problemKey"] in curated:
            p["generatedConcepts"] = p["concepts"]
            p["concepts"] = {c: float(w) for c, w in curated[p["problemKey"]].items()}
            p["conceptSource"] = "curated (V3)"
        else:
            p["conceptSource"] = "generated"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = {"name": "noworneverev/leetcode-api (public LeetCode metadata mirror)", "url": RAW_URL}
    (OUT_DIR / "leetcode_all_metadata.json").write_text(json.dumps(
        {"source": source, "count": len(problems), "problems": problems}, ensure_ascii=False) + "\n")
    (OUT_DIR / "leetcode_problems_1000.json").write_text(json.dumps({
        "source": source,
        "selection": "free Algorithms problems with >=1 mapped concept; 28 seed problems + most-liked",
        "acceptanceStats": stats,
        "count": len(selected),
        "problems": selected,
    }, indent=1, ensure_ascii=False) + "\n")
    fields = ["problemKey", "frontendId", "title", "slug", "difficulty", "priorDifficulty", "acceptanceRate",
              "likes", "dislikes", "totalSubmissions", "hintCount", "topicTags", "concepts", "url"]
    with (OUT_DIR / "leetcode_problems_1000.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in selected:
            row = {k: p[k] for k in fields}
            row["topicTags"] = "; ".join(p["topicTags"])
            row["concepts"] = "; ".join(f"{c}:{v}" for c, v in p["concepts"].items())
            w.writerow(row)
    (OUT_DIR / "tag_concept_map.json").write_text(json.dumps({
        "tiers": {"A": A, "B": B, "C": C},
        "rules": {t: {"concept": c, "weight": w} for t, (c, w) in sorted(TAG_RULES.items())},
        "contextual": {
            "Depth-First Search / Breadth-First Search": "TREES if a tree tag is present; GRAPHS if a graph/"
            "matrix/union-find tag is present; otherwise DFS -> RECURSION (0.7), BFS -> GRAPHS (0.7)",
            "Memoization": "also RECURSION (0.7)",
        },
        "treeTags": sorted(TREE_TAGS), "graphContextTags": sorted(GRAPH_CONTEXT_TAGS),
        "maxConceptsPerProblem": MAX_CONCEPTS_PER_PROBLEM,
    }, indent=1, ensure_ascii=False) + "\n")

    seed_catalog = {
        "_note": "GENERATED by scripts/build_leetcode_catalog.py: 1000-problem catalog for simulator + DKT.",
        "concepts": [{"code": c, "name": n, "prerequisites": pre} for c, (n, _, pre) in CONCEPTS.items()],
        "problems": [{"problemId": p["problemKey"], "title": p["title"], "staticDifficulty": p["difficulty"],
                      "priorDifficulty": p["priorDifficulty"], "acceptanceRate": p["acceptanceRate"],
                      "likes": p["likes"], "hintCount": p["hintCount"],
                      "concepts": list(p["concepts"]), "conceptWeights": p["concepts"]} for p in selected],
    }
    (ROOT / "data" / "seed" / "catalog_1000.json").write_text(json.dumps(seed_catalog, indent=1) + "\n")
    write_migration(selected)

    by_label = {l: sum(p["difficulty"] == l for p in selected) for l in LABEL_BASE}
    coverage = {c: sum(c in p["concepts"] for p in selected) for c in CONCEPTS}
    primary = {c: sum(max(p["concepts"], key=p["concepts"].get) == c for p in selected) for c in CONCEPTS}
    print(f"all problems: {len(problems)}  selected: {len(selected)}  by label: {by_label}")
    print("acceptance logit stats:", {k: {kk: round(vv, 3) for kk, vv in v.items()} for k, v in stats.items()})
    print("concept coverage (tagged / primary):")
    for c in CONCEPTS:
        print(f"  {c:20s} {coverage[c]:4d} / {primary[c]:4d}")
    print(f"wrote {MIGRATION.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

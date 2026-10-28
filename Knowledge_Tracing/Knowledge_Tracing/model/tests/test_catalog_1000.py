"""The generated 1000-problem catalog, its V5 migration and the Java prior formula stay in sync."""
import json
import math
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = ROOT / "data" / "seed" / "catalog_1000.json"
PROBLEMS = ROOT / "data" / "leetcode" / "leetcode_problems_1000.json"
TAG_MAP = ROOT / "data" / "leetcode" / "tag_concept_map.json"
V5 = ROOT / "backend" / "src" / "main" / "resources" / "db" / "migration" / "V5__leetcode_catalog_1000.sql"
V4 = ROOT / "backend" / "src" / "main" / "resources" / "db" / "migration" / "V4__catalog_metadata_and_concepts.sql"


class Catalog1000Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG.read_text())
        cls.by_id = {p["problemId"]: p for p in cls.catalog["problems"]}

    def test_shape(self):
        self.assertEqual(len(self.by_id), 1000)
        labels = {p["staticDifficulty"] for p in self.by_id.values()}
        self.assertEqual(labels, {"EASY", "MEDIUM", "HARD"})
        for p in self.by_id.values():
            self.assertTrue(0.5 <= p["priorDifficulty"] <= 9.5, p)
            self.assertTrue(0 < p["acceptanceRate"] < 1, p)
            self.assertTrue(p["conceptWeights"], p)
            self.assertTrue(all(0 < w <= 1.0 for w in p["conceptWeights"].values()), p)
            self.assertLessEqual(len(p["conceptWeights"]), 4, p)

    def test_prerequisite_graph_is_a_dag_over_known_concepts(self):
        pre = {c["code"]: c["prerequisites"] for c in self.catalog["concepts"]}
        for c, ps in pre.items():
            self.assertTrue(set(ps) <= set(pre), c)
        state = {}

        def visit(c):
            if state.get(c) == 1:
                self.fail(f"cycle through {c}")
            if state.get(c) == 2:
                return
            state[c] = 1
            for p in pre[c]:
                visit(p)
            state[c] = 2

        for c in pre:
            visit(c)

    def test_prior_formula_matches_documentation(self):
        stats = {"EASY": (3.0, 0.8929, 0.6924), "MEDIUM": (5.0, 0.2570, 0.6718), "HARD": (7.0, -0.1888, 0.6556)}
        for p in self.by_id.values():
            base, mu, sd = stats[p["staticDifficulty"]]
            a = min(max(p["acceptanceRate"], 1e-4), 1 - 1e-4)
            shift = max(-1.2, min(1.2, -0.6 * (math.log(a / (1 - a)) - mu) / sd))
            want = round(max(0.5, min(9.5, base + shift)), 2)
            # acceptanceRate is stored rounded to 4 decimals; the prior used the exact ratio
            self.assertAlmostEqual(p["priorDifficulty"], want, delta=0.011, msg=p["problemId"])

    def test_migration_contains_every_catalog_problem(self):
        sql = V5.read_text()
        keys = set(re.findall(r"^    \('(LC\d+)', \d+, ", sql, re.M))
        self.assertEqual(keys, set(self.by_id))
        tag_rows = re.findall(r"^    \('(LC\d+)', '([A-Z_]+)', ([0-9.]+)\)", sql, re.M)
        from_sql = {}
        for pid, c, w in tag_rows:
            from_sql.setdefault(pid, {})[c] = float(w)
        self.assertEqual(from_sql, {k: {c: float(w) for c, w in p["conceptWeights"].items()}
                                    for k, p in self.by_id.items()})

    def test_every_catalog_concept_exists_in_the_database(self):
        pattern = r"gen_random_uuid\(\),\s*'([A-Z_]{2,})'"
        seeded = set(re.findall(pattern, (V4.parent / "V3__seed_dev_data.sql").read_text()))
        seeded |= set(re.findall(pattern, V4.read_text()))
        self.assertEqual(len(seeded), 25)
        for c in self.catalog["concepts"]:
            self.assertIn(c["code"], seeded)

    def test_concept_weights_follow_the_tag_rules_except_curated_seeds(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_leetcode_catalog as builder
        curated = {p["problemId"]: p["conceptWeights"]
                   for p in json.loads((ROOT / "data" / "seed" / "catalog.json").read_text())["problems"]}
        rules = json.loads(TAG_MAP.read_text())["rules"]
        self.assertEqual(rules["Graph Theory"]["concept"], "GRAPHS")
        for p in json.loads(PROBLEMS.read_text())["problems"]:
            weights = self.by_id[p["problemKey"]]["conceptWeights"]
            if p["problemKey"] in curated:
                self.assertEqual(p["conceptSource"], "curated (V3)")
                self.assertEqual(weights, curated[p["problemKey"]])
            else:
                self.assertEqual(weights, builder.map_tags(p["topicTags"]), p["problemKey"])


if __name__ == "__main__":
    unittest.main()

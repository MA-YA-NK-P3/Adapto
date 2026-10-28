"""Offline Jev mirror: LinUCB/feature math matches the backend tests, the loop is deterministic."""
import pathlib
import random
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))

from jev import linucb  # noqa: E402
from jev import tutor  # noqa: E402
from kt.catalog import Catalog  # noqa: E402
from kt.simulator import World  # noqa: E402


class LinUcbTest(unittest.TestCase):
    def test_warm_start_predicts_expected_value(self):  # same numbers as backend LinUcbTest
        arm = linucb.Arm(1.0)
        x = [1, 0.6, 0.96, 0.1, 0.4, 1, 0, 0.83]
        mean, width, ucb = arm.score(x, 0.5)
        self.assertAlmostEqual(mean, 0.83, places=12)
        self.assertAlmostEqual(width, linucb.dot(x, x) ** 0.5, places=12)
        self.assertAlmostEqual(ucb, mean + 0.5 * width, places=12)

    def test_converges_to_linear_truth(self):
        rng = random.Random(1)
        truth = [0.2, 0.5, -0.1, 0.0, 0.0, 0.1, 0.3, 0.2]
        arm = linucb.Arm(1.0)
        for _ in range(3000):
            x = [1, rng.random(), rng.random(), rng.random() * 2 - 1, rng.random(), rng.randint(0, 1),
                 rng.random(), rng.random() * 1.5]
            arm.update(x, linucb.dot(truth, x) + rng.gauss(0, 0.05))
        for got, want in zip(arm.theta(), truth):
            self.assertAlmostEqual(got, want, delta=0.03)

    def test_features_match_backend_definition(self):
        ctx = {"ability": 4.5, "uncertainty": 0.6, "calibrated": True,
               "gap": {"type": "CONCEPT_GAP", "concept": "LINKED_LIST", "confidence": 0.8}}
        easier = {"action": "EASIER_PROBLEM", "conceptCode": "LINKED_LIST", "difficulty": 2.8,
                  "predictedSuccess": 0.8, "expectedValue": 0.75}
        x = linucb.features(easier, ctx)
        self.assertEqual(len(x), linucb.DIM)
        expected = [1.0, 0.8, 4 * 0.8 * 0.2, (2.8 - 4.5) / 3, 0.3, 1.0, 0.8, 0.75]
        for got, want in zip(x, expected):
            self.assertAlmostEqual(got, want, places=12)
        self.assertEqual(linucb.features(dict(easier, conceptCode=None), ctx)[6], 0.0)


class TutorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog.load(ROOT / "data" / "seed" / "catalog_1000.json")

    def test_reward_formula_matches_backend_default(self):
        self.assertEqual(tutor.QUALITY_WEIGHT, 0.0)
        self.assertAlmostEqual(tutor.reward(True, 5.0, 5.25), 0.75)
        self.assertAlmostEqual(tutor.reward(True, 5.0, 5.0), 0.5)
        self.assertAlmostEqual(tutor.reward(False, 5.0, 4.0), 0.0)

    def test_retries_do_not_move_ability(self):
        obs = tutor.Observed(self.catalog, {p["problemId"]: p["priorDifficulty"] for p in self.catalog.problems})
        obs.record("LC1", False)
        theta = obs.ability
        obs.record("LC1", True)
        self.assertEqual(obs.ability, theta)
        self.assertEqual(len(obs.history), 2)

    def test_candidates_are_bounded_and_deterministic_rules_remediate(self):
        diff = {p["problemId"]: p["priorDifficulty"] for p in self.catalog.problems}
        obs = tutor.Observed(self.catalog, diff)
        for pid in ("LC21", "LC206", "LC141"):
            obs.record(pid, False)
        gap = tutor.detect_gap(obs)
        self.assertIn(gap["type"], ("CONCEPT_GAP", "PREREQUISITE_GAP"))
        cands = tutor.CandidateGenerator(self.catalog, diff).generate(obs, gap)
        self.assertTrue(all(c["problemKey"] in diff for c in cands))
        ctx = {"ability": obs.ability, "uncertainty": obs.uncertainty, "calibrated": obs.calibrated, "gap": gap}
        choice = tutor.Deterministic().choose(cands, ctx, random.Random(0))
        self.assertIn(choice["action"], ("EASIER_PROBLEM", "PREREQUISITE_REVIEW"))

    def test_simulation_is_reproducible(self):
        a = tutor.run(World(self.catalog, seed=5), tutor.Bandit(0.5), 8, 10, 3)
        b = tutor.run(World(self.catalog, seed=5), tutor.Bandit(0.5), 8, 10, 3)
        self.assertEqual(a, b)
        self.assertEqual(len(a["rewardCurve"]), 80)
        self.assertTrue(all(0.0 <= r <= 1.0 for r in a["rewardCurve"]))


if __name__ == "__main__":
    unittest.main()

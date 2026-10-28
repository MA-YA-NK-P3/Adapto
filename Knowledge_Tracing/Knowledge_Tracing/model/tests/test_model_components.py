import random
import unittest

from kt import dkt
from kt.catalog import Catalog
from kt.mastery import ConceptMastery, MasteryState
from kt.policies import Observed, SystemPolicy, all_policies
from kt.synthetic import SyntheticWorld


class MasteryTest(unittest.TestCase):
    def test_prior_is_half(self):
        self.assertEqual(ConceptMastery().mastery, 0.5)

    def test_failures_lower_mastery_and_trigger_weakest(self):
        s = MasteryState()
        s.update({"LINKED_LIST": 1.0}, False)
        self.assertIsNone(s.weakest(), "a single observation must not declare a gap")
        s.update({"LINKED_LIST": 1.0}, False)
        self.assertEqual(s.weakest(), "LINKED_LIST")

    def test_uncertainty_shrinks(self):
        m = ConceptMastery()
        u0 = m.uncertainty
        for _ in range(5):
            m.update(True)
        self.assertLess(m.uncertainty, u0)


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()

    def test_seed_catalog_shape(self):
        self.assertEqual(len(self.catalog.concepts), 15)
        self.assertEqual(len(self.catalog.problems), 28)

    def test_transitive_prerequisites(self):
        self.assertIn("RECURSION", self.catalog.all_prerequisites("GRAPHS"))
        self.assertEqual(self.catalog.all_prerequisites("ARRAYS"), [])

    def test_every_problem_has_a_known_primary_concept(self):
        for pid, tags in self.catalog.tags.items():
            self.assertTrue(tags, pid)


class SyntheticWorldTest(unittest.TestCase):
    def test_deterministic_for_same_seed(self):
        c = Catalog.load()
        a, b = SyntheticWorld(c, seed=1), SyntheticWorld(c, seed=1)
        self.assertEqual(a.true_difficulty, b.true_difficulty)
        la, lb = a.make_learner("x", 5.0), b.make_learner("x", 5.0)
        self.assertEqual(la.skill, lb.skill)

    def test_stronger_learner_succeeds_more_often(self):
        c = Catalog.load()
        w = SyntheticWorld(c, seed=2)
        weak = w.make_learner("w", 2.0, weak_concept=None)
        strong = w.make_learner("s", 8.0, weak_concept=None)
        for pid in c.labels:
            self.assertLess(w.p_correct(weak, pid), w.p_correct(strong, pid))

    def test_practice_does_not_reduce_skill(self):
        c = Catalog.load()
        w = SyntheticWorld(c, seed=3)
        learner = w.make_learner("l", 5.0)
        before = dict(learner.skill)
        for pid in list(c.labels)[:10]:
            w.attempt(learner, pid)
        self.assertTrue(all(learner.skill[k] >= before[k] for k in before))


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()
        self.difficulty = {p: 5.0 for p in self.catalog.labels}

    def test_policies_never_recommend_solved_problems(self):
        rng = random.Random(0)
        for policy in all_policies():
            obs = Observed(self.catalog, self.difficulty)
            for pid in list(self.catalog.labels)[:20]:
                obs.record(pid, True)
            for _ in range(5):
                choice = policy.choose(obs, rng)
                self.assertNotIn(choice, obs.solved, policy.name)

    def test_concept_gap_routes_to_that_concept(self):
        obs = Observed(self.catalog, self.difficulty)
        obs.record("LC21", False)
        obs.record("LC206", False)
        choice = SystemPolicy(use_graph=False).choose(obs, random.Random(0))
        self.assertIn("LINKED_LIST", self.catalog.tags[choice])

    def test_prerequisite_gap_routes_to_prerequisite(self):
        obs = Observed(self.catalog, self.difficulty)
        obs.record("LC206", False)  # one LINKED_LIST failure: evidence for a prerequisite, not a gap on its own
        for pid in self.catalog.problems_for("TREES")[:3]:
            obs.record(pid, False)
        weak = obs.mastery.weakest()
        self.assertEqual(weak, "TREES")
        pre = SystemPolicy()._weak_prerequisite(obs, weak)
        self.assertIn(pre, ("LINKED_LIST", "RECURSION"))
        choice = SystemPolicy().choose(obs, random.Random(0))
        self.assertIn(pre, self.catalog.tags[choice])
        no_graph = SystemPolicy(use_graph=False).choose(obs, random.Random(0))
        self.assertIn("TREES", self.catalog.tags[no_graph])

    def test_exhausted_pool_returns_none(self):
        obs = Observed(self.catalog, self.difficulty)
        for pid in self.catalog.labels:
            obs.record(pid, True)
        for policy in all_policies():
            self.assertIsNone(policy.choose(obs, random.Random(0)), policy.name)


class DktTest(unittest.TestCase):
    def test_small_synthetic_dataset_is_reported_unavailable(self):
        c = Catalog.load()
        ds = dkt.build_dataset([[("LC1", True), ("LC21", False)]], c.tags)
        report = dkt.availability(ds, synthetic=True)
        self.assertEqual(report["status"], "UNAVAILABLE")
        self.assertTrue(any("SYNTHETIC" in r for r in report["reasons"]))
        self.assertEqual(ds.one_hot_width(), 2 * len(ds.skill_index))


if __name__ == "__main__":
    unittest.main()

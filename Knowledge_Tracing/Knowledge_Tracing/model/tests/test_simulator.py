"""Synthetic simulator v2: determinism, schema validity and the documented generative assumptions."""
import pathlib
import statistics
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))

from kt import events as ev  # noqa: E402
from kt.catalog import Catalog  # noqa: E402
from kt.simulator import World, sigmoid  # noqa: E402

CATALOG_1000 = ROOT / "data" / "seed" / "catalog_1000.json"


class SimulatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog.load(CATALOG_1000)

    def _run(self, seed, n=40):
        world = World(self.catalog, seed=seed)
        return world, list(world.generate(n))

    def test_catalog_has_1000_free_problems_all_tagged(self):
        self.assertEqual(len(self.catalog.problems), 1000)
        self.assertEqual(len(self.catalog.concepts), 25)
        self.assertTrue(all(self.catalog.tags[p["problemId"]] for p in self.catalog.problems))
        self.assertFalse(any(p.get("paidOnly") for p in self.catalog.problems))

    def test_same_seed_same_dataset(self):
        _, a = self._run(7, 15)
        _, b = self._run(7, 15)
        _, c = self._run(8, 15)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_events_are_valid_and_chronological(self):
        _, learners = self._run(11)
        for events, truth in learners:
            self.assertEqual(truth["events"], len(events))
            self.assertTrue(1 <= len(truth["weakConcepts"]) <= 3)
            ts = [e["timestamp"] for e in events]
            self.assertEqual(ts, sorted(ts))
            attempts = {}
            for seq, e in enumerate(events):
                ev.validate(dict(e, seq=seq))
                attempts[e["problemId"]] = attempts.get(e["problemId"], 0) + 1
                self.assertEqual(e["attemptNumber"], attempts[e["problemId"]])
                self.assertTrue(0.0 <= e["truth"]["pCorrect"] <= 1.0)

    def test_weak_concepts_really_are_weaker(self):
        _, learners = self._run(13, 120)
        weak, other = [], []
        for _, truth in learners:
            for c, m in truth["initialMastery"].items():
                (weak if c in truth["weakConcepts"] else other).append(m)
        self.assertLess(statistics.mean(weak) + 0.15, statistics.mean(other))

    def test_truth_probability_is_calibrated(self):
        _, learners = self._run(17, 200)
        ps, ys = [], []
        for events, _ in learners:
            for e in events:
                if e["result"] != "ABANDONED":
                    ps.append(e["truth"]["pCorrect"])
                    ys.append(e["correct"])
        self.assertGreater(len(ps), 3000)
        self.assertAlmostEqual(statistics.mean(ps), statistics.mean(ys), delta=0.02)

    def test_harder_items_are_solved_less_often(self):
        world = World(self.catalog, seed=3)
        learner = world.make_learner("probe")
        easy = min(world.problems, key=lambda q: world.true_b[q])
        hard = max(world.problems, key=lambda q: world.true_b[q])
        self.assertGreater(world.p_correct(learner, easy, 0.0), world.p_correct(learner, hard, 0.0))
        self.assertGreater(world.p_correct(learner, hard, 0.0, hint=True), world.p_correct(learner, hard, 0.0))

    def test_forgetting_decays_learned_skill(self):
        world = World(self.catalog, seed=5)
        learner = world.make_learner("forget")
        pid = world.by_concept["ARRAYS"][0]
        before = learner.skill("ARRAYS", 0.0)
        world.learn(learner, pid, 0.0, 0.5, True, False)
        right_after = learner.skill("ARRAYS", 0.0)
        much_later = learner.skill("ARRAYS", 365.0)
        self.assertGreater(right_after, before)
        self.assertLess(much_later, right_after)
        self.assertGreaterEqual(much_later, before - 1e-9)
        self.assertEqual(sigmoid(0.0), 0.5)


class EventsFormatTest(unittest.TestCase):
    def test_roundtrip_plain_and_gzip(self):
        recs = [{"schema": ev.SCHEMA, "learnerId": "L1", "seq": i, "problemId": "LC1",
                 "timestamp": f"2026-01-01T00:0{i}:00Z", "attemptNumber": i + 1,
                 "result": "ACCEPTED" if i else "WRONG_ANSWER", "correct": 1 if i else 0} for i in range(3)]
        with tempfile.TemporaryDirectory(dir=ROOT / "model") as d:
            for name in ("e.jsonl", "e.jsonl.gz"):
                path = pathlib.Path(d) / name
                self.assertEqual(ev.write_jsonl(path, reversed(recs)), 3)
                seqs = ev.load_sequences(path)
                self.assertEqual([e["seq"] for e in seqs["L1"]], [0, 1, 2])
                self.assertEqual(ev.load_sequences(path, known_problems={"LC2"}), {})

    def test_validation_rejects_inconsistent_records(self):
        good = {"learnerId": "L", "seq": 0, "problemId": "LC1", "timestamp": "t", "attemptNumber": 1,
                "result": "ACCEPTED", "correct": 1}
        ev.validate(good)
        with self.assertRaises(ValueError):
            ev.validate(dict(good, correct=0))
        with self.assertRaises(ValueError):
            ev.validate(dict(good, result="MAYBE"))
        with self.assertRaises(ValueError):
            ev.validate({k: v for k, v in good.items() if k != "seq"})

    def test_split_is_deterministic_disjoint_and_proportional(self):
        ids = [f"SYN-{i:05d}" for i in range(4000)]
        tr, va, te = ev.split_learners(ids)
        self.assertEqual((tr, va, te), ev.split_learners(list(reversed(ids))))
        self.assertEqual(len(set(tr) | set(va) | set(te)), 4000)
        self.assertAlmostEqual(len(tr) / 4000, 0.70, delta=0.03)
        self.assertAlmostEqual(len(te) / 4000, 0.15, delta=0.03)
        self.assertEqual(ev.event_id("L", 3), ev.event_id("L", 3))


if __name__ == "__main__":
    unittest.main()

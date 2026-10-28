"""kt-dkt-v1 contract: the stdlib reference, the NumPy evaluator and (if installed) PyTorch agree.

The same fixture is replayed by backend DktModelTest, so Python and Java are pinned together.
"""
import json
import math
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model"))
sys.path.insert(0, str(ROOT / "model" / "dkt"))

from features import FORMAT, Encoder, ReferenceDkt, assisted, scaled_difficulty  # noqa: E402

FIXTURES = ROOT / "backend" / "src" / "test" / "resources" / "dkt"


def _w_input(w, correct, diff, a):
    y = 1.0 if correct else 0.0
    return [v * y for v in w] + [v * (1.0 - y) for v in w] + [diff, float(a)]


class ReferenceParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = json.loads((FIXTURES / "fixture-model.json").read_text())
        cls.expected = json.loads((FIXTURES / "fixture-expected.json").read_text())

    def test_fixture_is_current_format(self):
        self.assertEqual(self.model["format"], FORMAT)
        self.assertEqual(len(self.model["concepts"]), 25)

    def test_reference_reproduces_fixture(self):
        ref = ReferenceDkt(self.model)
        state = ref.initial_state()
        for step in self.expected["steps"]:
            p = ref.predict(state, step["weights"], step["scaledDifficulty"], step["attempted"],
                            step["problemKey"])
            self.assertAlmostEqual(p, step["predictionBefore"], places=12)
            state = ref.step(state, _w_input(step["weights"], step["correct"], step["scaledDifficulty"],
                                             step["assisted"]))
            for got, want in zip(ref.mastery(state), step["masteryAfter"]):
                self.assertAlmostEqual(got, want, places=12)

    def test_numpy_evaluator_matches_reference(self):
        try:
            import numpy  # noqa: F401
            sys.path.insert(0, str(ROOT / "model" / "evaluation"))
            from evaluate_kt import NumpyDkt
        except ImportError:
            self.skipTest("numpy not installed")
        ref, fast = ReferenceDkt(self.model), NumpyDkt(self.model)
        s_ref, s_fast = ref.initial_state(), fast.initial_state()
        for step in self.expected["steps"]:
            x = _w_input(step["weights"], step["correct"], step["scaledDifficulty"], step["assisted"])
            s_ref, s_fast = ref.step(s_ref, x), fast.step(s_fast, x)
            for a, b in zip(ref.concept_logits(s_ref), fast.concept_logits(s_fast)):
                self.assertAlmostEqual(a, float(b), places=10)

    def test_torch_forward_matches_reference(self):
        try:
            import torch
            from dkt_model import DKT
        except ImportError:
            self.skipTest("torch not installed")
        k, h = len(self.model["concepts"]), self.model["hiddenSize"]
        net = DKT(k, 1, h, dropout=0.0).double().eval()
        w = self.model["weights"]
        with torch.no_grad():
            net.lstm.weight_ih_l0.copy_(torch.tensor(w["lstm.weight_ih"], dtype=torch.float64))
            net.lstm.weight_hh_l0.copy_(torch.tensor(w["lstm.weight_hh"], dtype=torch.float64))
            net.lstm.bias_ih_l0.copy_(torch.tensor(w["lstm.bias_ih"], dtype=torch.float64))
            net.lstm.bias_hh_l0.copy_(torch.tensor(w["lstm.bias_hh"], dtype=torch.float64))
            net.out.weight.copy_(torch.tensor(w["out.weight"], dtype=torch.float64))
            net.out.bias.copy_(torch.tensor(w["out.bias"], dtype=torch.float64))
        xs = [_w_input(s["weights"], s["correct"], s["scaledDifficulty"], s["assisted"])
              for s in self.expected["steps"]]
        with torch.no_grad():
            hidden, _ = net.lstm(torch.tensor([xs], dtype=torch.float64))
            logits = net.out(hidden)[0]
        ref = ReferenceDkt(self.model)
        state = ref.initial_state()
        for t, x in enumerate(xs):
            state = ref.step(state, x)
            for a, b in zip(ref.concept_logits(state), logits[t].tolist()):
                self.assertAlmostEqual(a, b, places=10)


class EncoderTest(unittest.TestCase):
    def test_encoding_layout(self):
        enc = Encoder(["A", "B", "C"], [{"problemId": "P", "conceptWeights": {"B": 1.0, "C": 0.5, "Z": 1.0},
                                         "priorDifficulty": 7.0}])
        self.assertEqual(enc.input_size, 8)
        self.assertEqual(enc.input_vector("P", 1, 0), [0, 1, 0.5, 0, 0, 0, 1.0, 0.0])
        self.assertEqual(enc.input_vector("P", 0, 1), [0, 0, 0, 0, 1, 0.5, 1.0, 1.0])
        self.assertEqual(scaled_difficulty(5.0), 0.0)
        self.assertEqual(assisted({"hintUsed": False, "solutionViewed": True}), 1)

    def test_unseen_item_has_no_offset(self):
        model = json.loads((FIXTURES / "fixture-model.json").read_text())
        ref = ReferenceDkt(model)
        state = ref.initial_state()
        w = [1.0] + [0.0] * 24
        base = ref.predict(state, w, 0.0, False, "LC999999")
        y0 = ref.concept_logits(state)[0]
        expected = 1 / (1 + math.exp(-(y0 + ref.bias)))
        self.assertAlmostEqual(base, expected, places=12)


if __name__ == "__main__":
    unittest.main()

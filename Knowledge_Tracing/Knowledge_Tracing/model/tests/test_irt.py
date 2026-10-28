import math
import random
import unittest

from kt import irt


class RaschModelTest(unittest.TestCase):
    def test_probability_is_half_when_ability_equals_difficulty(self):
        self.assertAlmostEqual(irt.p_success(5.0, 5.0), 0.5)

    def test_probability_monotone_in_ability_and_difficulty(self):
        self.assertLess(irt.p_success(4.0, 5.0), irt.p_success(6.0, 5.0))
        self.assertGreater(irt.p_success(5.0, 3.0), irt.p_success(5.0, 7.0))

    def test_information_peaks_at_ability(self):
        self.assertAlmostEqual(irt.information(5.0, 5.0), 0.25)
        self.assertLess(irt.information(5.0, 8.0), 0.25)

    def test_sigmoid_is_numerically_stable(self):
        self.assertEqual(irt.sigmoid(-1000), 0.0)
        self.assertEqual(irt.sigmoid(1000), 1.0)

    def test_clamp_handles_nan_and_bounds(self):
        self.assertEqual(irt.clamp(float("nan")), 5.0)
        self.assertEqual(irt.clamp(-3), 0.0)
        self.assertEqual(irt.clamp(42), 10.0)

    def test_no_responses_returns_prior(self):
        theta, unc = irt.estimate_ability([])
        self.assertAlmostEqual(theta, 5.0)
        self.assertAlmostEqual(unc, 2.0)

    def test_success_raises_and_failure_lowers_ability(self):
        self.assertGreater(irt.estimate_ability([(5.0, True)])[0], 5.0)
        self.assertLess(irt.estimate_ability([(5.0, False)])[0], 5.0)

    def test_all_correct_stays_bounded(self):
        theta, _ = irt.estimate_ability([(9.5, True)] * 200)
        self.assertLessEqual(theta, 10.0)

    def test_uncertainty_shrinks_with_evidence(self):
        u1 = irt.estimate_ability([(5.0, True), (5.0, False)])[1]
        u2 = irt.estimate_ability([(5.0, True), (5.0, False)] * 10)[1]
        self.assertLess(u2, u1)

    def test_recovers_known_ability(self):
        rng = random.Random(3)
        true_theta = 7.0
        responses = []
        for _ in range(400):
            b = rng.uniform(2, 9)
            responses.append((b, rng.random() < irt.p_success(true_theta, b)))
        theta, _ = irt.estimate_ability(responses)
        self.assertAlmostEqual(theta, true_theta, delta=0.4)

    def test_difficulty_rises_when_able_learners_fail(self):
        b, _ = irt.estimate_difficulty([(8.0, False)] * 5, prior_mean=5.0)
        self.assertGreater(b, 5.0)

    def test_calibration_recovers_item_order(self):
        rng = random.Random(5)
        true_b = {"E": 3.0, "M": 5.0, "H": 7.5}
        labels = {"E": "MEDIUM", "M": "MEDIUM", "H": "MEDIUM"}  # labels deliberately uninformative
        seqs = []
        for _ in range(300):
            theta = rng.gauss(5, 1.5)
            seqs.append([(p, rng.random() < irt.p_success(theta, b)) for p, b in true_b.items()])
        cal = irt.calibrate_items(seqs, labels)
        self.assertLess(cal["E"], cal["M"])
        self.assertLess(cal["M"], cal["H"])
        self.assertTrue(all(0.0 <= v <= 10.0 and not math.isnan(v) for v in cal.values()))


if __name__ == "__main__":
    unittest.main()

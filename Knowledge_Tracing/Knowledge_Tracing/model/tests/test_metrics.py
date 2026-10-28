import math
import unittest

from kt import metrics


class MetricsTest(unittest.TestCase):
    def test_perfect_ranking_has_auc_one(self):
        self.assertEqual(metrics.auc([False, False, True, True], [0.1, 0.2, 0.8, 0.9]), 1.0)

    def test_inverted_ranking_has_auc_zero(self):
        self.assertEqual(metrics.auc([True, True, False, False], [0.1, 0.2, 0.8, 0.9]), 0.0)

    def test_constant_predictions_have_auc_half(self):
        self.assertEqual(metrics.auc([True, False, True, False], [0.5] * 4), 0.5)

    def test_auc_undefined_for_single_class(self):
        self.assertTrue(math.isnan(metrics.auc([True, True], [0.2, 0.7])))

    def test_known_auc_value(self):
        # pairs (pos,neg): (0.35>0.1) (0.35<0.4) (0.8>0.1) (0.8>0.4) -> 3/4
        self.assertAlmostEqual(metrics.auc([False, False, True, True], [0.1, 0.4, 0.35, 0.8]), 0.75)

    def test_log_loss_of_coin_flip(self):
        self.assertAlmostEqual(metrics.log_loss([True, False], [0.5, 0.5]), math.log(2))

    def test_log_loss_is_finite_for_confident_mistakes(self):
        self.assertTrue(math.isfinite(metrics.log_loss([True], [0.0])))

    def test_accuracy_and_brier(self):
        self.assertEqual(metrics.accuracy([True, False], [0.9, 0.2]), 1.0)
        self.assertAlmostEqual(metrics.brier([True, False], [0.9, 0.2]), (0.01 + 0.04) / 2)

    def test_mae_rmse(self):
        self.assertEqual(metrics.mae([1, 2], [2, 4]), 1.5)
        self.assertAlmostEqual(metrics.rmse([1, 2], [2, 4]), math.sqrt(2.5))

    def test_perfectly_calibrated_has_zero_ece(self):
        y = [True] * 3 + [False] * 7
        self.assertAlmostEqual(metrics.expected_calibration_error(y, [0.3] * 10), 0.0)

    def test_spearman(self):
        self.assertAlmostEqual(metrics.spearman([1, 2, 3], [10, 20, 30]), 1.0)
        self.assertAlmostEqual(metrics.spearman([1, 2, 3], [3, 2, 1]), -1.0)


if __name__ == "__main__":
    unittest.main()

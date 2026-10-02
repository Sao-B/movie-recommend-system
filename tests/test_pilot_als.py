import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.pilot.als import fit_spark, init_factors, predict_one, solve_bias, solve_factor, train_rmse


class AlsCoreTests(unittest.TestCase):
    def test_solve_factor_matches_ridge_lstsq(self):
        rng = np.random.RandomState(0)
        q = rng.normal(size=(6, 3))
        target = rng.normal(size=6)
        lam, n_obs = 0.1, 6
        got = solve_factor(q, target, lam, n_obs)
        # 岭回归的增广最小二乘等价形式
        aug_q = np.vstack([q, np.sqrt(lam * n_obs) * np.eye(3)])
        aug_t = np.concatenate([target, np.zeros(3)])
        expected = np.linalg.lstsq(aug_q, aug_t, rcond=None)[0]
        np.testing.assert_allclose(got, expected, rtol=1e-9, atol=1e-9)

    def test_solve_bias_closed_form(self):
        self.assertAlmostEqual(
            solve_bias(np.array([1.0, 2.0, 3.0]), lam=0.5, n_obs=3),
            6.0 / (3 * 1.5), places=9)

    def test_predict_one(self):
        got = predict_one(3.5, 0.1, -0.2, np.array([1.0, 0.0]), np.array([0.5, 2.0]))
        self.assertAlmostEqual(got, 3.9, places=9)

    def test_init_deterministic(self):
        a = init_factors([1, 2], rank=4, seed=42)
        b = init_factors([1, 2], rank=4, seed=42)
        np.testing.assert_array_equal(a[1], b[1])
        self.assertFalse(np.allclose(a[1], a[2]))  # 不同 id 初值不同

    def test_init_is_seed_sensitive(self):
        # 上面的 test_init_deterministic 只锁「同种子一致 + 不同 id 不同」，
        # 杀不死「忽略 seed、只用 id 派生」的变异体（该变异体两条断言都过）。
        # 这里补上跨种子差异：seed 必须真正参与初值派生。
        self.assertFalse(np.allclose(init_factors([1], rank=4, seed=42)[1],
                                     init_factors([1], rank=4, seed=7)[1]))

    def test_fit_spark_rejects_duplicate_pairs(self):
        class _NoSpark(object):
            @property
            def sparkContext(self):
                raise AssertionError('sparkContext 不应被触碰')
        rows = [(1, 1, 5.0, 1), (1, 1, 3.0, 2)]
        with self.assertRaises(ValueError):
            fit_spark(_NoSpark(), rows, rank=2, reg=0.1, iters=1)

    def test_train_rmse_matches_hand_computed(self):
        model = {'mu': 1.0,
                 'bu': {1: 0.5}, 'bi': {2: -0.25},
                 'P': {1: np.array([2.0, 0.0])}, 'Q': {2: np.array([0.5, 1.0])}}
        rows = [(1, 2, 2.0, 1)]
        # pred = 1 + 0.5 - 0.25 + 1.0 = 2.25 -> rmse = 0.25
        self.assertAlmostEqual(train_rmse(rows, model['mu'], model['bu'], model['bi'],
                                          model['P'], model['Q']), 0.25, places=9)


if __name__ == '__main__':
    unittest.main()

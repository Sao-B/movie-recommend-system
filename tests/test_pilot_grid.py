import unittest
from unittest.mock import patch
import numpy as np
import tempfile
from types import SimpleNamespace

from experiments.pilot.data import temporal_split, temporal_split_three_way
from experiments.pilot.als import objective, train_rmse, fit_spark
from experiments.pilot.metrics import recommendable_positive_ratio, prediction_bounds


class LocalRDD:
    def __init__(self, values):
        self.values = list(values)

    def map(self, fn):
        return LocalRDD(map(fn, self.values))

    def groupByKey(self):
        groups = {}
        for key, value in self.values:
            groups.setdefault(key, []).append(value)
        return LocalRDD(groups.items())

    def mapPartitions(self, fn):
        return LocalRDD(fn(iter(self.values)))

    def collect(self):
        return self.values


class LocalContext:
    def parallelize(self, values, num_slices):
        self.num_slices = num_slices
        return LocalRDD(values)

    def broadcast(self, value):
        return SimpleNamespace(value=value)


class GridContractTests(unittest.TestCase):
    def test_cli_defaults_retain_legacy_two_way(self):
        from experiments.pilot.run_pilot import main
        with patch('sys.argv', ['run_pilot.py', '--input', 'fixture', '--out', 'unused']):
            with patch('experiments.pilot.run_pilot.legacy_main') as legacy:
                main()
        self.assertEqual(legacy.call_args[0][0].split, 'two-way')
        self.assertEqual(legacy.call_args[0][0].models, 'all')

    def test_split_exact_ties_use_original_index(self):
        rows = [(1, 7, float(i), 100) for i in range(10)]
        train, validation, test = temporal_split_three_way(rows)
        self.assertEqual(train, rows[:7])
        self.assertEqual(validation, rows[7:8])
        self.assertEqual(test, rows[8:])
        self.assertEqual(temporal_split_three_way([]), ([], [], []))

    def test_empty_validation_does_not_evaluate_reserved_test(self):
        from experiments.pilot.run_pilot import run_experiment
        with tempfile.TemporaryDirectory() as out:
            args = SimpleNamespace(split='three-way', val_frac=.2, test_frac=.2,
                                   models='als', top_n=20, seed=42, rank=2,
                                   reg=.1, iters=1, num_slices=2, out=out, input='fixture')
            result = run_experiment(SimpleNamespace(sparkContext=LocalContext()),
                                    [(1, 1, 3., 1), (1, 2, 5., 2)], args)
            metrics = result['models']['ALS']
            self.assertEqual(result['protocol']['test_rows'], 1)
            self.assertEqual(result['protocol']['validation_rows'], 0)
            self.assertEqual(metrics['users_scored'], 0)
            self.assertIsNone(metrics['rmse'])
            self.assertIsNone(metrics['validation_prediction_bounds']['rate'])
            self.assertIsNone(metrics['top_prediction_bounds']['rate'])

    def test_real_solver_callback_matches_return_and_decreases_objective(self):
        rows = [(1, 1, 5., 1), (1, 2, 1., 2), (2, 1, 1., 1), (2, 2, 5., 2)]
        spark = SimpleNamespace(sparkContext=LocalContext())
        recorded = []
        def on_iter(iteration, model, loss):
            recorded.append((iteration, loss, objective(rows, model, .1)))
        model, losses = fit_spark(spark, rows, rank=2, reg=.1, iters=4, num_slices=3, on_iter=on_iter)
        self.assertEqual([x[0] for x in recorded], [1, 2, 3, 4])
        self.assertEqual([x[1] for x in recorded], losses)
        self.assertAlmostEqual(recorded[-1][2], objective(rows, model, .1))
        self.assertTrue(all(b[2] <= a[2] + 1e-8 for a, b in zip(recorded, recorded[1:])))
        self.assertEqual(spark.sparkContext.num_slices, 3)

    def test_als_three_way_uses_only_train_and_known_validation(self):
        import csv
        from experiments.pilot.run_pilot import run_experiment
        rows = [(1, 1, 3., 1), (1, 9, 5., 2), (1, 10, 5., 3),
                (2, 2, 3., 1), (2, 1, 4., 2), (2, 11, 5., 3)]
        with tempfile.TemporaryDirectory() as out:
            args = SimpleNamespace(split='three-way', val_frac=.2, test_frac=.2,
                                   models='als', top_n=20, seed=42, rank=2,
                                   reg=.1, iters=4, num_slices=2, out=out, input='fixture')
            result = run_experiment(SimpleNamespace(sparkContext=LocalContext()), rows, args)
            metrics = result['models']['ALS']
            self.assertEqual(metrics['validation_prediction_bounds']['count'], 1)
            self.assertEqual(metrics['top_prediction_bounds']['count'], 2)
            self.assertEqual(metrics['const_rmse'], 1.)
            self.assertEqual(len(result['objective_values']), 4)
            with open(out + '/validation_predictions.csv', newline='') as f:
                predictions = list(csv.DictReader(f))
            self.assertEqual([(r['user'], r['movie_id']) for r in predictions], [('2', '1')])
            self.assertAlmostEqual(metrics['rmse'], abs(float(predictions[0]['raw_prediction']) - 4.))

    def test_objective_anomaly_persists_failure_and_aborts(self):
        import json
        from experiments.pilot.run_pilot import run_experiment
        rows = [(1, 1, 3., 1), (1, 9, 5., 2), (1, 10, 5., 3)]
        with tempfile.TemporaryDirectory() as out:
            args = SimpleNamespace(split='three-way', val_frac=.2, test_frac=.2,
                                   models='als', top_n=20, seed=42, rank=2,
                                   reg=.1, iters=4, num_slices=2, out=out, input='fixture')
            with patch('experiments.pilot.run_pilot.objective', side_effect=[1., 2.]):
                with self.assertRaisesRegex(RuntimeError, 'objective increased'):
                    run_experiment(SimpleNamespace(sparkContext=LocalContext()), rows, args)
            with open(out + '/summary.json') as f:
                self.assertEqual(json.load(f)['status'], 'objective_anomaly')

    def test_mostpop_does_not_train_and_preserves_unrecommendable_positive(self):
        from experiments.pilot.run_pilot import run_experiment
        # User 1 validates unseen item 9; train universe only contains items 1,2.
        rows = [(1, 1, 3., 1), (1, 9, 5., 2), (1, 10, 5., 3),
                (2, 2, 3., 1), (2, 1, 4., 2), (2, 11, 5., 3)]
        with tempfile.TemporaryDirectory() as out:
            args = SimpleNamespace(split='three-way', val_frac=.2, test_frac=.2,
                                   models='mostpop', top_n=20, seed=42, rank=2,
                                   reg=.1, iters=3, num_slices=2, out=out, input='fixture')
            with patch('experiments.pilot.run_pilot.fit_spark', side_effect=AssertionError('must not fit')):
                result = run_experiment(None, rows, args)
            self.assertEqual(result['protocol']['test_rows'], 2)
            self.assertEqual(result['protocol']['effective_prediction_rows'], 1)
            self.assertEqual(result['models']['MostPop']['users_scored'], 2)
            self.assertEqual(result['models']['MostPop']['recall@20'], .5)
            self.assertEqual(result['models']['MostPop']['recommendable_positive_ratio'], .5)
            self.assertEqual(result['models']['MostPop']['top_prediction_bounds']['count'], 0)

    def test_objective_trace_checks_increases_and_heuristic(self):
        from experiments.pilot.run_pilot import objective_diagnostics
        self.assertEqual(objective_diagnostics([100, 99.9, 99.8, 99.7])['status'], 'small_recent_changes')
        self.assertEqual(objective_diagnostics([100, 101])['status'], 'objective_anomaly')
        self.assertEqual(objective_diagnostics([100, 90])['status'], 'still_changing')

    def test_split_boundaries_and_old_test_contract(self):
        for n, expected in [(1, (1, 0, 0)), (2, (1, 0, 1)),
                            (3, (1, 1, 1)), (10, (7, 1, 2)), (100, (64, 16, 20))]:
            rows = [(1, i, 4.0, i // 3) for i in reversed(range(n))]
            parts = temporal_split_three_way(rows)
            self.assertEqual(tuple(map(len, parts)), expected)
            self.assertEqual(parts[2], temporal_split(rows)[1])
            self.assertEqual(sorted(sum(parts, [])), sorted(rows))
            for part in parts:
                self.assertEqual(part, [r for r in rows if r in part])

    def test_objective_hand_calculation_observation_weights(self):
        model = dict(mu=1., bu={1: .5}, bi={2: -.25, 3: 0.},
                     P={1: np.array([2., 0.])},
                     Q={2: np.array([.5, 1.]), 3: np.array([0., 0.])})
        rows = [(1, 2, 2., 1), (1, 3, 2., 2)]
        # SSE=.25^2+(-.5)^2; user twice, each item once.
        self.assertAlmostEqual(objective(rows, model, .1), .3125 + .1 * (2 * 4.25 + 1.3125))
        self.assertAlmostEqual(objective(rows, model, 0), len(rows) * train_rmse(rows, **model) ** 2)

    def test_positive_ratio_retains_cold_and_deduplicates(self):
        self.assertEqual(recommendable_positive_ratio([1, 1, 2, 9], [1, 2, 3]), 2 / 3)
        self.assertIsNone(recommendable_positive_ratio([], [1]))
        self.assertEqual(recommendable_positive_ratio([9], [1]), 0)

    def test_bounds_actual_short_recommendations_and_finite(self):
        result = prediction_bounds([.49, .5, 5., 5.1])
        self.assertEqual(result, dict(count=4, out_of_range_count=2, rate=.5, minimum=.49, maximum=5.1))
        self.assertIsNone(prediction_bounds([])['rate'])
        for value in [float('nan'), float('inf'), -float('inf')]:
            with self.assertRaises(ValueError):
                prediction_bounds([value])


if __name__ == '__main__':
    unittest.main()

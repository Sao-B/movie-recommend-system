import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.pilot.run_grid import resource_peaks, experiment_commands, execute


class GridRunnerTests(unittest.TestCase):
    def test_source_change_during_child_invalidates_current_and_blocks_rest(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('experiments.pilot.run_grid.source_hashes', side_effect=[{'a': 'old'}, {'a': 'old'}, {'a': 'new'}]):
                with patch('experiments.pilot.run_grid.subprocess.call', return_value=0):
                    runs = execute(str(Path(folder) / 'new-run'), 'fixture')
            self.assertEqual(runs[0]['status'], 'failed')
            self.assertIn('Source changed', runs[0]['reason'])
            self.assertTrue(all(run['status'] == 'blocked' for run in runs[1:]))

    def test_truncated_child_summary_records_failure_and_continues(self):
        def terminated_child(command, **kwargs):
            out = Path(command[command.index('--out') + 1])
            out.mkdir()
            (out / 'summary.json').write_text('{"status":')
            return 137
        with tempfile.TemporaryDirectory() as folder:
            output = str(Path(folder) / 'new-run')
            with patch('experiments.pilot.run_grid.subprocess.call', side_effect=terminated_child):
                runs = execute(output, 'fixture')
            self.assertEqual(len(runs), 10)
            self.assertTrue(all(run['status'] == 'failed' for run in runs))
            self.assertTrue(all(run['exit_code'] == 137 for run in runs))
            self.assertTrue((Path(output) / 'summary_table.csv').is_file())

    def test_peak_rss_is_simultaneous_sum_not_sum_of_separate_peaks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'resources.csv'
            path.write_text('ts,swap_used_mb,java_rss_mb,python_rss_mb\n'
                            '1,20,100,20\n2,30,20,100\n# exit=0 ts=3\n')
            result = resource_peaks(str(path))
        self.assertEqual(result['peak_user_java_python_rss_mb'], 120)
        self.assertEqual(result['peak_system_swap_mb'], 30)

    def test_missing_resource_samples_are_unknown(self):
        self.assertEqual(resource_peaks('does-not-exist.csv'),
                         {'peak_user_java_python_rss_mb': None, 'peak_system_swap_mb': None})

    def test_all_ten_commands_have_fixed_protocol(self):
        runs = experiment_commands('/code', '/out', 'hdfs:///movielens/pilot/ratings.csv', 'spark-submit')
        self.assertEqual(len(runs), 10)
        self.assertEqual(len({r['name'] for r in runs}), 10)
        self.assertEqual(runs[0]['model'], 'MostPop')
        self.assertEqual({(r['rank'], r['reg']) for r in runs[1:]},
                         {(r, l) for r in (10, 30, 50) for l in (.01, .1, 1.)})
        for run in runs:
            command = run['command']
            for key, expected in [('--iters', '10'), ('--top-n', '20'), ('--seed', '42'),
                                  ('--num-slices', '16'), ('--split', 'three-way'),
                                  ('--val-frac', '0.2')]:
                self.assertEqual(command[command.index(key) + 1], expected)


if __name__ == '__main__':
    unittest.main()

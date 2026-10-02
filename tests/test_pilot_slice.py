import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.make_pilot_slice import count_users, sample_users, filter_rows


class PilotSliceTests(unittest.TestCase):
    def _write_csv(self, path, rows):
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['userId', 'movieId', 'rating', 'timestamp'])
            w.writerows(rows)

    def test_sample_deterministic(self):
        counts = {'1': 5, '2': 3, '3': 9, '4': 1}
        a = sample_users(counts, 2, seed=42)
        b = sample_users(counts, 2, seed=42)
        self.assertEqual(a, b)

    def test_filter_rows_keeps_only_sampled(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'in.csv')
            dst = os.path.join(d, 'out.csv')
            self._write_csv(src, [
                ['1', '10', '4.0', '100'],
                ['2', '11', '3.0', '101'],
                ['1', '12', '5.0', '102'],
            ])
            n = filter_rows(src, dst, {'1'})
            self.assertEqual(n, 2)
            with open(dst, encoding='utf-8') as f:
                lines = f.read().strip().splitlines()
            self.assertEqual(len(lines), 3)  # header + 2 rows
            self.assertIn('10', lines[1])
            self.assertIn('12', lines[2])

    def test_count_users(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'in.csv')
            self._write_csv(src, [
                ['1', '10', '4.0', '100'],
                ['1', '11', '3.0', '101'],
                ['2', '12', '5.0', '102'],
            ])
            self.assertEqual(count_users(src), {'1': 2, '2': 1})


if __name__ == '__main__':
    unittest.main()

# -*- coding: utf-8 -*-
"""本轮（全量规模留出评测）的口径、边界与对拍用例。

这个文件的存在理由：本轮的核心计算在实现过程中**被实测证伪过四次**，
每一条都已写成锚点用例，防止后来者改回错误写法。四处的详情见
`.vmwork/holdout_eval_verified_contract.md`。

    T1 切分协议         —— 80/20 取整与 n<2 边界
    T2 逐用户对拍       —— 与 confirm.py 的真实实现逐用户比 Top-20（ID + 顺序）
    T3 共现 oracle      —— c_ij = n_like(i) ⟺ i 的每个好评用户都好评 j
    T4 命中口径         —— 候选池**按方法分别**施加；行内定位正确
    T5 判定与诚实性     —— 三条判定路径与"自查 ≠ 独立核验"
    T6 回归             —— 既有 ItemCF 模块不受本轮影响
"""
import csv
import math
import os
import sys
import unittest

import numpy as np
from scipy import sparse

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf import confirm as C          # noqa: E402
from experiments.itemcf import heldout_eval as H     # noqa: E402
from experiments.pilot.data import temporal_split    # noqa: E402


def fixture_dir(prefix):
    """在 tests/ 下建唯一临时目录（沿用既有 itemcf 测试的做法）。"""
    import time
    root = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, '_itemcf_holdout_tmp',
                        '%s-%d' % (prefix, int(time.time() * 1000)))
    os.makedirs(path)
    return path


def write_ratings(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['userId', 'movieId', 'rating', 'timestamp'])
        for row in rows:
            writer.writerow(row)
    return path


def build_fixture(seed=20261005, n_users=120, n_items=200, min_rows=20,
                  max_rows=61):
    """合成评分：每用户按时间递增，评分混合好坏。

    ``max_rows`` 会被收敛到 ``n_items``（不能对同一用户重复采样同一电影）。
    """
    rng = np.random.RandomState(seed)
    rows = []
    for user in range(1, n_users + 1):
        n = min(int(rng.randint(min_rows, max_rows)), n_items)
        movies = rng.choice(np.arange(1, n_items + 1), size=n, replace=False)
        movies.sort()
        base = 1000000 + user * 10000
        for k, movie in enumerate(movies):
            rating = float(rng.choice([1.0, 2.0, 3.0, 3.5, 4.0, 4.5, 5.0],
                                      p=[.05, .07, .13, .10, .25, .25, .15]))
            rows.append((int(user), int(movie), rating, base + k))
    rows.sort(key=lambda row: (row[0], row[3]))
    return rows


class LocateInRowsTests(unittest.TestCase):
    """T4a：CSR 行内定位。**两种错误写法都实测出错**，这里逐个钉死。

    - 错法 1：对整条 ``indices`` 做 searchsorted（跨行回绕）
    - 错法 2：给每行加 ``indptr`` 偏移做全局单调（空行导致相邻行键并列）
    """

    def _brute(self, indices, indptr, rows, cols):
        out = []
        for r, c in zip(rows.tolist(), cols.tolist()):
            begin, end = int(indptr[r]), int(indptr[r + 1])
            seg = indices[begin:end]
            at = int(np.searchsorted(seg, c))
            out.append(begin + at if at < seg.shape[0] and seg[at] == c else -1)
        return np.asarray(out, dtype=np.int64)

    def test_finds_all_hits_in_first_row(self):
        """错法 1 会在这里返回全 -1（行 0 的跨行回绕）。"""
        indices = np.array([1, 4, 0, 2, 5], dtype=np.int64)
        indptr = np.array([0, 2, 5, 5], dtype=np.int64)
        rows = np.array([0, 0, 1, 1, 1, 2], dtype=np.int64)
        cols = np.array([1, 4, 0, 2, 5, 3], dtype=np.int64)
        got = H.locate_in_rows(indices, indptr, rows, cols)
        np.testing.assert_array_equal(got, [0, 1, 2, 3, 4, -1])
        np.testing.assert_array_equal(got, self._brute(indices, indptr, rows,
                                                       cols))

    def test_does_not_leak_across_rows_with_gaps(self):
        """错法 2 会在这里把"属于行 0 的列"判成行 1 的命中。"""
        indices = np.array([13, 18, 21, 22, 34], dtype=np.int64)
        indptr = np.array([0, 2, 5], dtype=np.int64)
        rows = np.array([0, 0, 0, 1, 1], dtype=np.int64)
        cols = np.array([13, 99, 21, 21, 34], dtype=np.int64)
        got = H.locate_in_rows(indices, indptr, rows, cols)
        np.testing.assert_array_equal(got, [0, -1, -1, 2, 4])
        np.testing.assert_array_equal(got, self._brute(indices, indptr, rows,
                                                       cols))

    def test_handles_empty_rows(self):
        indices = np.array([5, 7], dtype=np.int64)
        indptr = np.array([0, 2, 2, 2], dtype=np.int64)
        rows = np.array([0, 1, 2, 0], dtype=np.int64)
        cols = np.array([5, 5, 5, 7], dtype=np.int64)
        got = H.locate_in_rows(indices, indptr, rows, cols)
        np.testing.assert_array_equal(got, [0, -1, -1, 1])

    def test_random_agreement_with_brute_force(self):
        rng = np.random.RandomState(7)
        for _trial in range(25):
            n_rows = int(rng.randint(1, 25))
            n_cols = int(rng.randint(1, 20))
            indptr = [0]
            indices = []
            for _row in range(n_rows):
                k = int(rng.randint(0, 5))
                k = min(k, n_cols)
                if k:
                    indices.extend(np.sort(
                        rng.choice(n_cols, size=k, replace=False)).tolist())
                indptr.append(len(indices))
            indices = np.asarray(indices, dtype=np.int64)
            indptr = np.asarray(indptr, dtype=np.int64)
            rows = rng.randint(0, n_rows, size=40).astype(np.int64)
            cols = rng.randint(0, n_cols * 2, size=40).astype(np.int64)
            np.testing.assert_array_equal(
                H.locate_in_rows(indices, indptr, rows, cols),
                self._brute(indices, indptr, rows, cols))


class CooccurrenceTests(unittest.TestCase):
    """T3：共现表的 oracle —— ``c_ij = n_like(i)`` ⟺ i 的每个好评用户都好评 j。"""

    @staticmethod
    def _rows_to_csr(rows, n_items):
        users = sorted(set(r[0] for r in rows))
        ui = dict((u, p) for p, u in enumerate(users))
        r_list, c_list = [], []
        for u, i, rating, _ts in rows:
            if rating >= H.POSITIVE_THRESHOLD:
                r_list.append(ui[u])
                c_list.append(i - 1)
        shape = (len(users), n_items)
        return sparse.csr_matrix(
            (np.ones(len(r_list), dtype=np.int64),
             (np.asarray(r_list, dtype=np.int64),
              np.asarray(c_list, dtype=np.int64))), shape=shape, dtype=np.int64)

    def test_cooccurrence_matches_python_sets(self):
        """与**独立**的 Python 集合实现比对整张 C（``C`` 存的是**相似度**）。

        ``C[j, i] = c_ji / sqrt(n_like(j)·n_like(i))``，与
        ``core.build_neighbors`` / ``confirm`` 的口径一致（它们累加的也是相似度）。

        随机数据下"某个电影的每个好评用户都好评了另一个电影"很少发生，
        所以这里**刻意构造**嵌套结构（用户 1 好评 {1}；用户 2 好评 {1,2}；……），
        让 oracle 真正开火——第一版用例在随机数据上是**空洞的**（``checked=0``）。
        """
        n_items = 8
        rows = []
        for user in range(1, n_items + 2):
            for movie in range(1, min(user, n_items) + 1):
                rows.append((user, movie, 5.0, 1000 + user * 10 + movie))
        A = self._rows_to_csr(rows, n_items)
        item_ids = np.arange(1, n_items + 1, dtype=np.int64)
        # 这个 fixture 里 A 覆盖全部用户，所以 A 的列和**就是**全局 n_like。
        # （生产路径上二者不同，见 test_requires_global_n_like。）
        global_like = np.asarray(A.sum(axis=0)).ravel().astype(np.int64)
        Cmat, stats = H.build_cooccurrence(A, item_ids, max_k=100,
                                           block_size=4, n_like=global_like)
        dense = np.asarray(Cmat.todense())
        n_like = np.asarray(A.sum(axis=0)).ravel().astype(float)
        likes = {}
        for user, movie, rating, _ts in rows:
            if rating >= H.POSITIVE_THRESHOLD:
                likes.setdefault(user, set()).add(movie - 1)

        # --- oracle 1：每个好评 i 的用户都好评 j ⟹ c_ij == n_like(i) ---------
        checked = 0
        for i_pos in range(n_items):
            i_users = set(u for u, s in likes.items() if i_pos in s)
            if not i_users:
                continue
            for j_pos in range(n_items):
                if i_pos == j_pos:
                    self.assertEqual(dense[i_pos, j_pos], 0.0,
                                     'c_ii must be zero (no self loops)')
                    continue
                j_users = set(u for u, s in likes.items() if j_pos in s)
                if i_users <= j_users:
                    similarity = dense[i_pos, j_pos]
                    recovered = similarity * np.sqrt(n_like[i_pos]
                                                     * n_like[j_pos])
                    self.assertAlmostEqual(
                        recovered, float(n_like[i_pos]), places=6,
                        msg='C[%d,%d]=%.9f implies c_ij=%.6f but every user '
                            'who liked item %d also liked item %d, so c_ij '
                            'must be n_like=%d'
                            % (i_pos, j_pos, similarity, recovered, i_pos,
                               j_pos, int(n_like[i_pos])))
                    checked += 1
        self.assertGreater(checked, 0, 'oracle never fired; test is vacuous')

        # --- oracle 2：对称性（共现本身是对称的）---------------------------
        self.assertTrue(np.allclose(dense, dense.T),
                        'co-occurrence must be symmetric')
        # --- oracle 3：每个来源至多 max_k 条 -------------------------------
        counts = np.diff(Cmat.indptr)
        self.assertLessEqual(int(counts.max()), H.MAX_K)

    def test_similarity_normalisation_uses_supplied_n_like(self):
        """相似度用**传入的** ``n_like`` 归一化（``c/sqrt(n_i·n_j)``）。"""
        n_items = 6
        rows = []
        for user in range(1, 7):
            for movie in range(1, min(user, n_items) + 1):
                rows.append((user, movie, 5.0, 1000 + user * 10 + movie))
        A = self._rows_to_csr(rows, n_items)
        item_ids = np.arange(1, n_items + 1, dtype=np.int64)
        n_like = np.asarray(A.sum(axis=0)).ravel().astype(np.int64)
        Cmat, _ = H.build_cooccurrence(A, item_ids, max_k=100, block_size=2,
                                       n_like=n_like)
        dense = np.asarray(Cmat.todense())
        for i in range(n_items):
            for j in range(n_items):
                if dense[i, j] == 0:
                    continue
                expected = dense[i, j] * np.sqrt(float(n_like[i])
                                                 * float(n_like[j]))
                self.assertAlmostEqual(expected, round(expected), places=6,
                                       msg='similarity at (%d,%d) is not '
                                           'c/sqrt(n_like_i * n_like_j)'
                                           % (i, j))

    def test_requires_global_n_like_and_rejects_none(self):
        """**真实犯过的错**：用 ``A`` 的列和当分母，相似度被系统性放大。

        ``A`` 只含评测用户（生产路径上约 2,000 行），它的列和远小于全库好评人数。
        所以本函数把 ``n_like`` 设为**必填**；传 ``None`` 必须直接报错，
        而不是悄悄退化成错误口径。
        """
        A = sparse.csr_matrix(np.asarray([[1, 1, 0], [0, 1, 1]],
                                         dtype=np.int64))
        item_ids = np.asarray([1, 2, 3], dtype=np.int64)
        with self.assertRaises(H.CorrectnessError):
            H.build_cooccurrence(A, item_ids, max_k=100, block_size=2,
                                 n_like=None)
        with self.assertRaises(H.CorrectnessError):
            H.build_cooccurrence(A, item_ids, max_k=100, block_size=2,
                                 n_like=np.asarray([1, 2], dtype=np.int64))

    def test_inflated_normaliser_is_detected(self):
        """把 ``A`` 的列和当分母会得到**不同**的相似度——锁死这个差异。

        正确：``c01/sqrt(global_n0 * global_n1)``；错误：``c01/sqrt(local0*local1)``。
        两者数值不同，所以"用错分母"是可测的（这也是独立核验器抓到的那个错）。
        """
        # 全库有 10 个用户，但只有 2 个出现在 A 里
        A = sparse.csr_matrix(np.asarray([[1, 1, 0], [0, 1, 1]],
                                         dtype=np.int64))
        item_ids = np.asarray([1, 2, 3], dtype=np.int64)
        global_like = np.asarray([10, 20, 10], dtype=np.int64)
        Cmat, _ = H.build_cooccurrence(A, item_ids, max_k=100, block_size=2,
                                       n_like=global_like)
        dense = np.asarray(Cmat.todense())
        c01 = 1                      # 两个用户都好评了 item0 与 item1
        want = c01 / np.sqrt(10.0 * 20.0)
        wrong = c01 / np.sqrt(1.0 * 2.0)      # A 的列和
        self.assertAlmostEqual(dense[0, 1], want, places=12)
        self.assertNotAlmostEqual(dense[0, 1], wrong, places=6)


class SplitProtocolTests(unittest.TestCase):
    """T1：切分协议。"""

    def test_matches_temporal_split_exactly(self):
        """与 ``experiments.pilot.data.temporal_split`` 逐用户同口径。

        训练段写**全部用户**；留出段只写**评测用户**（其余丢弃）。所以这里
        把 ``eval_users`` 设成全量用户，使两段都写出来后再比对。
        """
        import io
        rows = build_fixture(seed=3, n_users=40, n_items=40)
        expected_train, expected_test = temporal_split(list(rows), 0.2)
        all_users = set(r[0] for r in rows)
        session = H.RatingSession.__new__(H.RatingSession)
        session.test_frac = 0.2
        session.keep_all_train = True
        session.eval_users = all_users
        session.counts = {'users_empty_train': 0, 'users_with_train': 0,
                          'users_with_holdout': 0, 'users_empty_holdout': 0,
                          'train_rows': 0, 'holdout_rows': 0,
                          'dropped_holdout_rows': 0}
        session.empty_train_users = []
        out_train, out_test = [], []
        for user in sorted(all_users):
            group = [r for r in rows if r[0] == user]
            train_buffer, holdout_buffer = io.StringIO(), io.StringIO()
            session._flush_user(user, group, csv.writer(train_buffer),
                                csv.writer(holdout_buffer))
            out_train.extend(csv.reader(io.StringIO(train_buffer.getvalue())))
            out_test.extend(csv.reader(io.StringIO(holdout_buffer.getvalue())))
        self.assertEqual(len(out_train), len(expected_train))
        self.assertEqual(len(out_test), len(expected_test))
        self.assertEqual(
            sorted(tuple(str(v) for v in r[:3]) for r in out_train),
            sorted(tuple(str(v) for v in r[:3]) for r in expected_train))
        self.assertEqual(
            sorted(tuple(str(v) for v in r[:3]) for r in out_test),
            sorted(tuple(str(v) for v in r[:3]) for r in expected_test))

    def test_holdout_only_keeps_eval_users(self):
        """留出段只保留评测用户；其余用户的留出段被丢弃（不计入 holdout_rows）。"""
        import io
        rows = build_fixture(seed=4, n_users=30, n_items=30)
        all_users = sorted(set(r[0] for r in rows))
        keep = set(all_users[:10])
        session = H.RatingSession.__new__(H.RatingSession)
        session.test_frac = 0.2
        session.keep_all_train = True
        session.eval_users = keep
        session.counts = {'users_empty_train': 0, 'users_with_train': 0,
                          'users_with_holdout': 0, 'users_empty_holdout': 0,
                          'train_rows': 0, 'holdout_rows': 0,
                          'dropped_holdout_rows': 0}
        session.empty_train_users = []
        train_rows = 0
        dropped = 0
        for user in all_users:
            group = [r for r in rows if r[0] == user]
            train_buffer, holdout_buffer = io.StringIO(), io.StringIO()
            session._flush_user(user, group, csv.writer(train_buffer),
                                csv.writer(holdout_buffer))
            train_rows += len(list(csv.reader(
                io.StringIO(train_buffer.getvalue()))))
        # 训练段覆盖**全部**用户（全量规模的关键）
        self.assertEqual(train_rows, session.counts['train_rows'])
        self.assertGreater(train_rows, 0)
        # 留出段只含评测用户：以 temporal_split 为基准直接算期望行数
        want_holdout = 0
        for user in keep:
            group = [r for r in rows if r[0] == user]
            _train, holdout = temporal_split(list(group), 0.2)
            want_holdout += len(holdout)
        self.assertEqual(session.counts['holdout_rows'], want_holdout)
        self.assertGreater(session.counts['dropped_holdout_rows'], 0)

    def test_single_rating_user_goes_entirely_to_train(self):
        """**实测更正**：``n == 1 → train 1 / test 0``（不是 train 0 / test 1）。

        ``temporal_split`` 在 ``n < 2`` 时短路（``data.py`` L18-20），
        所以单评分用户整体进训练段、且永远不可评测。
        """
        rows = [(7, 1, 5.0, 100)]
        train, test = temporal_split(list(rows), 0.2)
        self.assertEqual(len(train), 1)
        self.assertEqual(len(test), 0)

    def test_split_rounding_table(self):
        """n = 1..12 的取整表（与实测一致）。"""
        expected = {1: (1, 0), 2: (1, 1), 3: (2, 1), 4: (3, 1), 5: (4, 1),
                    6: (5, 1), 7: (6, 1), 8: (7, 1), 9: (8, 1), 10: (8, 2),
                    11: (9, 2), 12: (10, 2)}
        for n, (want_train, want_test) in expected.items():
            rows = [(1, m, 5.0, 1000 + m) for m in range(1, n + 1)]
            train, test = temporal_split(list(rows), 0.2)
            self.assertEqual((len(train), len(test)), (want_train, want_test),
                             'n=%d' % n)

    def test_no_leakage_between_segments(self):
        rows = build_fixture(seed=5, n_users=30, n_items=30)
        train, test = temporal_split(list(rows), 0.2)
        train_pairs = set((r[0], r[1]) for r in train)
        test_pairs = set((r[0], r[1]) for r in test)
        self.assertEqual(train_pairs & test_pairs, set())
        self.assertEqual(len(train) + len(test), len(rows))


class HitEquivalenceTests(unittest.TestCase):
    """T2：与 ``confirm.py`` 的真实实现逐用户对拍（**最重要的一条**）。

    这是本轮唯一能证明"新的矩阵路径没有改变数学"的证据。
    覆盖两个 regime：不受 ``max_k`` 截断、以及**几乎全部来源都被截断**。
    """

    def _compare(self, seed, n_users, n_items, min_rows=6, max_rows=None):
        rows = build_fixture(seed=seed, n_users=n_users, n_items=n_items,
                             min_rows=min_rows,
                             max_rows=max_rows or min_rows * 3)
        item_ids, _ = C.build_index([rows])
        item_ids_arr = np.asarray(item_ids, dtype=np.int64)
        users = sorted(set(r[0] for r in rows))
        ktp = dict((int(m), p) for p, m in enumerate(item_ids))
        ui = dict((u, p) for p, u in enumerate(users))
        shape = (len(users), len(item_ids))
        seen, history = {}, {}
        n_all, n_like = {}, {}
        for u, i, r, _ts in rows:
            seen.setdefault(u, set()).add(i)
            n_all[i] = n_all.get(i, 0) + 1
            if r >= H.POSITIVE_THRESHOLD:
                n_like[i] = n_like.get(i, 0) + 1
                history.setdefault(u, set()).add(i)
        order_all = sorted(item_ids, key=lambda i: (-n_all.get(i, 0), i))
        order_like = sorted(item_ids, key=lambda i: (-n_like.get(i, 0), i))

        graph, _ = C.build_graph_csr(iter([rows]), item_ids, beta=H.BETA,
                                     max_k=H.MAX_K, block_size=32)
        r_list, c_list = [], []
        for u, i, r, _ts in rows:
            if r >= H.POSITIVE_THRESHOLD:
                r_list.append(ui[u])
                c_list.append(ktp[i])
        A = sparse.csr_matrix((np.ones(len(r_list), dtype=np.int64),
                               (np.asarray(r_list), np.asarray(c_list))),
                              shape=shape, dtype=np.int64)
        # 该 fixture 里 A 覆盖全部用户，且全部行都进训练段，所以 A 的列和 == 全局 n_like。
        global_like = np.asarray(A.sum(axis=0)).ravel().astype(np.int64)
        Cmat, _ = H.build_cooccurrence(A, item_ids, H.MAX_K, 64,
                                       n_like=global_like)
        support = (A @ Cmat).tocsr()

        for p, u in enumerate(users):
            # 基准：confirm 的逐用户打分
            hist = np.asarray(sorted(history.get(u, set())), dtype=np.int64)
            ref = C.score_itemcf_user(hist, graph, seen[u], H.TOP_N)
            ref_ids = [row['movie_id'] for row in ref]
            # 待测：support 上排除 seen 后按 (-score, movieId) 取前 20
            start, stop = support.indptr[p], support.indptr[p + 1]
            cols = np.asarray(support.indices[start:stop], dtype=np.int64)
            vals = np.asarray(support.data[start:stop], dtype=np.float64)
            seen_items = set(ktp[i] for i in seen[u])
            keep = np.asarray([int(c) not in seen_items for c in cols.tolist()],
                              dtype=bool)
            cols = cols[keep]
            vals = vals[keep]
            order = np.lexsort((item_ids_arr[cols], -vals))
            got_ids = [int(item_ids_arr[pos]) for pos in cols[order[:H.TOP_N]]]
            self.assertEqual(
                got_ids, ref_ids,
                'seed=%d user=%d: matrix path != confirm.py\n got=%r\n ref=%r'
                % (seed, u, got_ids[:8], ref_ids[:8]))
            # 人气基线的候选也必须与 confirm 的 top_n_excluding 一致。
            # **域必须一致**：`top_n_popularity(order, seen)` 里 `order` 与
            # `seen` 必须同域（都用位置，或都用 movieId）。生产路径两处都用
            # **位置下标**；这里额外把 movieId 域的 confirm 结果转成位置再比。
            seen_pos_set = set(ktp[i] for i in seen[u])
            order_all_pos, order_like_pos = H.popularity_orders(
                np.asarray([n_all.get(i, 0) for i in item_ids], dtype=np.int64),
                np.asarray([n_like.get(i, 0) for i in item_ids],
                           dtype=np.int64),
                len(item_ids))
            for label, order_pos, order_ids in (
                    ('MostPop', order_all_pos, order_all),
                    ('LikePop', order_like_pos, order_like)):
                got_positions = H.top_n_popularity(order_pos, seen_pos_set,
                                                   H.TOP_N)
                got_movie_ids = [int(item_ids_arr[pos])
                                 for pos in got_positions]
                want_movie_ids = C.top_n_excluding(order_ids, seen[u],
                                                   H.TOP_N)
                self.assertEqual(
                    got_movie_ids, want_movie_ids,
                    '%s candidate pool differs from confirm for user %d\n'
                    ' got=%r\n want=%r'
                    % (label, u, got_movie_ids[:8], want_movie_ids[:8]))

    def test_equivalence_without_truncation(self):
        self._compare(seed=1, n_users=80, n_items=60)

    def test_equivalence_under_heavy_truncation(self):
        """item 数远超 max_k 时几乎每个来源都被截断——截断必须被施加。"""
        self._compare(seed=2, n_users=120, n_items=400, min_rows=20,
                      max_rows=61)

    def test_equivalence_when_eval_users_are_a_subset(self):
        """**生产路径的真实形状**：``A`` 只含一部分用户，但 ``n_like`` 是全库的。

        这一条覆盖了"分母必须用全量 ``n_like``"的错法：
        若把 ``A`` 的列和当分母，相似度被放大，**Top-20 排序不变**（每行乘同一
        因子）但产物里的相似度数值不同——所以只看指标是抓不到的，
        必须像独立核验器那样比对**相似度本身**。这里直接断言相似度。
        """
        rows = build_fixture(seed=9, n_users=150, n_items=200, min_rows=20,
                             max_rows=40)
        item_ids, _ = C.build_index([rows])
        item_ids_arr = np.asarray(item_ids, dtype=np.int64)
        ktp = dict((int(m), p) for p, m in enumerate(item_ids))
        # 全量 n_like（全体用户）
        full_like = np.zeros(len(item_ids), dtype=np.int64)
        for _u, i, r, _ts in rows:
            if r >= H.POSITIVE_THRESHOLD:
                full_like[ktp[i]] += 1
        # 只用前 60 个用户当"评测用户"
        eval_users = sorted(set(r[0] for r in rows))[:60]
        eval_set = set(eval_users)
        ui = dict((u, p) for p, u in enumerate(eval_users))
        r_list, c_list = [], []
        for u, i, r, _ts in rows:
            if u in eval_set and r >= H.POSITIVE_THRESHOLD:
                r_list.append(ui[u])
                c_list.append(ktp[i])
        A = sparse.csr_matrix((np.ones(len(r_list), dtype=np.int64),
                               (np.asarray(r_list), np.asarray(c_list))),
                              shape=(len(eval_users), len(item_ids)),
                              dtype=np.int64)
        local_like = np.asarray(A.sum(axis=0)).ravel().astype(np.int64)
        # 只挑一个"局部与全局计数不同"的来源，确保这条用例不是空洞的
        differing = np.flatnonzero((local_like != full_like) & (local_like > 0)
                                   & (full_like > 0))
        self.assertGreater(differing.shape[0], 0,
                           'fixture is degenerate: local == global counts')
        source_pos = int(differing[0])
        order = [item_ids_arr[p] for p in range(len(item_ids))]
        C_good, _ = H.build_cooccurrence(A, item_ids, H.MAX_K, 64,
                                         n_like=full_like)
        C_bad, _ = H.build_cooccurrence(A, item_ids, H.MAX_K, 64,
                                        n_like=local_like)
        start, stop = C_good.indptr[source_pos], C_good.indptr[source_pos + 1]
        good = C_good.data[start:stop]
        bad_start, bad_stop = C_bad.indptr[source_pos], C_bad.indptr[source_pos + 1]
        bad = C_bad.data[bad_start:bad_stop]
        self.assertGreater(good.shape[0], 0, 'source has no neighbours')
        self.assertEqual(good.shape[0], bad.shape[0])
        # 相似度**不同**（这正是错法的可观测后果）
        self.assertFalse(np.allclose(good, bad),
                         'using local n_like produced identical similarities; '
                         'the fixture does not exercise the bug')
        # 且每条边的比值恰好由**两个端点**的（全库/局部）计数决定：
        #   s_good / s_bad = sqrt( (l_src·l_tgt) / (g_src·g_tgt) )
        # （局部计数更小 ⇒ 分母更小 ⇒ 相似度被放大，所以 good/bad < 1。）
        target_pos = int(C_good.indices[start])
        ratio = good[0] / bad[0]
        expected = math.sqrt(
            (float(local_like[source_pos]) * float(local_like[target_pos]))
            / (float(full_like[source_pos]) * float(full_like[target_pos])))
        self.assertAlmostEqual(ratio, expected, places=9)
        self.assertLess(ratio, 1.0,
                        'using local counts must INFLATE the similarity')


class CandidatePoolTests(unittest.TestCase):
    """T4b：**候选池必须按方法分别施加**（曾经把人气池套给 ItemCF，命中归零）。"""

    def test_popularity_orders_match_confirm(self):
        item_ids = np.asarray([10, 20, 30, 40], dtype=np.int64)
        n_all = np.asarray([3, 5, 5, 1], dtype=np.int64)
        n_like = np.asarray([1, 3, 3, 0], dtype=np.int64)
        order_all, order_like = H.popularity_orders(n_all, n_like, 4)
        self.assertEqual([int(item_ids[p]) for p in order_all], [20, 30, 10, 40])
        self.assertEqual([int(item_ids[p]) for p in order_like], [20, 30, 10, 40])

    def test_top_n_popularity_excludes_seen(self):
        order = [0, 1, 2, 3, 4]
        self.assertEqual(H.top_n_popularity(order, set([0, 2]), 2), [1, 3])
        self.assertEqual(H.top_n_popularity(order, set(), 3), [0, 1, 2])

    def test_candidate_mask_reflects_duplicate_popularity(self):
        """同一用户池内重复出现的电影在 ``data`` 里累加（表示"占两个名额"）。

        ``build_candidate_mask`` 的输入是**每个方法各自**的候选列表；
        调用方把 MostPop 与 LikePop 的前 20 **分别**传进来时不会重复。
        这里直接喂一个带重复的列表，验证累加语义本身。
        """
        item_ids = [10, 20]
        ktp = {10: 0, 20: 1}
        mask = H.build_candidate_mask([[10, 20, 10]], (1, 2), ktp, item_ids)
        self.assertEqual(mask.toarray().tolist(), [[2, 1]])

    def test_candidate_mask_dedupes_within_a_single_method(self):
        """单个方法的候选列表按"集合"语义去重（同一电影不会占两个名额）。"""
        item_ids = [10, 20]
        ktp = {10: 0, 20: 1}
        mask = H.build_candidate_mask([[10, 20], [10]], (2, 2), ktp, item_ids)
        self.assertEqual(mask.toarray().tolist(), [[1, 1], [1, 0]])

    def test_none_spec_yields_empty_row(self):
        mask = H.build_candidate_mask([None], (1, 2), {10: 0, 20: 1}, [10, 20])
        self.assertEqual(mask.nnz, 0)


class VerdictTests(unittest.TestCase):
    """T5：判定路径与诚实性。"""

    @staticmethod
    def _metrics(itemcf_ndcg, itemcf_recall, base_ndcg=0.08, base_recall=0.09):
        return {
            'ItemCF': {'ndcg@20': itemcf_ndcg, 'recall@20': itemcf_recall},
            'MostPop': {'ndcg@20': base_ndcg, 'recall@20': base_recall},
            'LikePop': {'ndcg@20': base_ndcg + 0.001,
                        'recall@20': base_recall + 0.001},
        }

    @staticmethod
    def _boot(ndcg_lo, recall_lo):
        return {
            'ItemCF_vs_LikePop': {
                'ndcg@20': {'ci95': [ndcg_lo, 0.09], 'excludes_zero': ndcg_lo > 0},
                'recall@20': {'ci95': [recall_lo, 0.09],
                              'excludes_zero': recall_lo > 0}},
            'ItemCF_vs_MostPop': {
                'ndcg@20': {'ci95': [ndcg_lo, 0.09], 'excludes_zero': ndcg_lo > 0},
                'recall@20': {'ci95': [recall_lo, 0.09],
                              'excludes_zero': recall_lo > 0}},
        }

    def test_supported_when_both_intervals_exclude_zero(self):
        metrics = self._metrics(0.13, 0.14)
        self.assertEqual(H.verdict_of(metrics, self._boot(0.03, 0.02)),
                         H.VERDICT_SUPPORTED)

    def test_crosses_zero_when_ndcg_interval_includes_zero(self):
        """**本轮 300 用户试跑的真实形状**：点估计领先但 NDCG 区间跨零。"""
        metrics = self._metrics(0.0985, 0.1201)
        self.assertEqual(H.verdict_of(metrics, self._boot(-0.0003, 0.0071)),
                         H.VERDICT_CROSSES_ZERO)

    def test_not_reproduced_when_beaten_by_baseline(self):
        metrics = self._metrics(0.05, 0.06)
        self.assertEqual(H.verdict_of(metrics, self._boot(0.03, 0.02)),
                         H.VERDICT_NOT_REPRODUCED)

    def test_failure_when_metrics_missing(self):
        self.assertEqual(H.verdict_of({}, {}), H.VERDICT_FAILURE)

    def test_detail_exposes_every_condition(self):
        metrics = self._metrics(0.0985, 0.1201)
        detail = H.verdict_detail(metrics, self._boot(-0.0003, 0.0071))
        self.assertEqual(detail['stronger_baseline'], 'LikePop')
        self.assertTrue(detail['baselines']['LikePop']['ndcg_beats'])
        self.assertFalse(detail['baselines']['LikePop']['ndcg@20_excludes_zero'])
        self.assertIn('stronger_ndcg_ci95', detail)


class HonestyTests(unittest.TestCase):
    """T5b：**"自查"不得被说成"独立核验"**（上一轮的真实缺陷 H2）。"""

    def test_self_check_is_not_labelled_as_independent(self):
        payload = H.build_self_check(
            {'ItemCF': {'ndcg@20': 0.1}}, {}, [], [],
            {'evaluable_users': 1, 'skipped_no_positive': 0,
             'support_nnz': 5, 'cooccurrence': {'edges': 7}}, [1])
        self.assertEqual(payload['status'], 'self_check_only')
        note = payload['note'].lower()
        self.assertIn('not independent verification', note)
        self.assertIn('verify_holdout_eval', note)

    def test_conclusion_report_carries_the_mandatory_caveats(self):
        metrics = {'ItemCF': {'ndcg@20': .1, 'recall@20': .1, 'users_scored': 5,
                              'full_list_rate': 1.0, 'catalog_coverage': .01},
                   'MostPop': {'ndcg@20': .08, 'recall@20': .09,
                               'users_scored': 5, 'full_list_rate': 1.0,
                               'catalog_coverage': .001},
                   'LikePop': {'ndcg@20': .081, 'recall@20': .091,
                               'users_scored': 5, 'full_list_rate': 1.0,
                               'catalog_coverage': .001}}
        text = H.render_conclusion(metrics, {}, 'some_verdict',
                                   {'run_id': 'r1', 'config': {},
                                    'extra': {}})
        self.assertIn('不是严格盲测', text)
        self.assertIn('不可直接比较', text)
        self.assertIn('some_verdict', text)

    def test_conclusion_separates_self_check_from_independent_verification(self):
        """**上一轮缺陷 H2 的回归**：不得把"自查"说成"已通过独立核验"。

        `conclusion.md` 由算指标的同一个模块渲染，所以它**没有资格**声称
        独立核验通过；必须显式指向 `verification_independent.json`。
        """
        metrics = {'ItemCF': {'ndcg@20': .1, 'recall@20': .1, 'users_scored': 5,
                              'full_list_rate': 1.0, 'catalog_coverage': .01},
                   'MostPop': {'ndcg@20': .08, 'recall@20': .09,
                               'users_scored': 5, 'full_list_rate': 1.0,
                               'catalog_coverage': .001},
                   'LikePop': {'ndcg@20': .081, 'recall@20': .091,
                               'users_scored': 5, 'full_list_rate': 1.0,
                               'catalog_coverage': .001}}
        text = H.render_conclusion(metrics, {}, 'v',
                                   {'run_id': 'r1', 'config': {},
                                    'extra': {'evaluable_users': 4}})
        self.assertIn('self_check_only', text)
        self.assertIn('verification_independent.json', text)
        # 必须明确否认"本文档声称独立核验"
        self.assertIn('不声称', text)
        # 必须说明"不是全体用户都被评测"
        self.assertIn('不是"全体用户都被评测"', text)

    def test_self_check_payload_never_claims_pass(self):
        payload = H.build_self_check(
            {'ItemCF': {'ndcg@20': 0.1}}, {}, [], [],
            {'evaluable_users': 1, 'skipped_no_positive': 0,
             'support_nnz': 5, 'cooccurrence': {'edges': 7}}, [1])
        self.assertNotIn('pass', payload)
        self.assertEqual(payload['status'], 'self_check_only')


class HashSelectionTests(unittest.TestCase):
    """T1b：名单冻结的可复现性与独立性。"""

    def test_selection_is_deterministic_and_salt_sensitive(self):
        users = list(range(1, 500))
        first = H.select_top_by_hash(users, 'salt-a:', 10)
        again = H.select_top_by_hash(users, 'salt-a:', 10)
        other = H.select_top_by_hash(users, 'salt-b:', 10)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 10)
        self.assertEqual(len(set(first)), 10)

    def test_user_key_uses_canonical_decimal_form(self):
        """键按"无前导零十进制"参与哈希——写入的是字面量 ``%d``，不是原字符串。"""
        import hashlib
        expected = (hashlib.sha256('s:8'.encode('utf-8')).hexdigest(), 8)
        self.assertEqual(H.user_key('s:', 8), expected)
        # 规范形式：'s:8'（而不是 's:08'）
        self.assertNotEqual(H.user_key('s:', 8)[0],
                            hashlib.sha256('s:08'.encode('utf-8')).hexdigest())

    def test_user_list_sha256_is_order_sensitive(self):
        self.assertNotEqual(H.user_list_sha256([1, 2]), H.user_list_sha256([2, 1]))


class RegressionTests(unittest.TestCase):
    """T6：本轮的改动不得影响既有模块。"""

    def test_full_train_source_hash_list_is_unchanged(self):
        """``full_train`` 的签名仍只覆盖 6 个既有模块（本轮不新增依赖）。"""
        from experiments.itemcf import full_train as F
        hashes = F.source_hashes_of_modules()
        self.assertEqual(sorted(hashes), sorted([
            'conclusion_writer.py', 'core.py', 'full_train.py', 'model_io.py',
            'resource_guard.py', 'verify_full_model.py']))

    def test_holdout_modules_are_python36_compatible(self):
        """本轮的所有新模块必须能在 VM 的 Python 3.6 上跑。"""
        banned = []
        for name in ('heldout_eval.py', 'holdout_common.py',
                     'rate_limited_guard.py'):
            path = os.path.join(PROJECT_ROOT, 'experiments', 'itemcf', name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding='utf-8') as handle:
                for number, line in enumerate(handle, 1):
                    stripped = line.strip()
                    if stripped.startswith('#'):
                        continue
                    if 'f"' in line or "f'" in line:
                        if '= f"' in line or '= f\'' in line or \
                                '(f"' in line or '(f\'' in line:
                            banned.append('%s:%d f-string' % (name, number))
                    if '@dataclass' in line:
                        banned.append('%s:%d dataclass' % (name, number))
                    if ':=' in line and 'dict(' not in line:
                        banned.append('%s:%d walrus' % (name, number))
                    if 'from __future__ import annotations' in line:
                        banned.append('%s:%d future-annotations'
                                      % (name, number))
        self.assertEqual(banned, [], 'Python 3.6 incompatible constructs: %r'
                         % (banned,))

    def test_blas_threads_are_set_to_one(self):
        env = H.set_blas_threads_to_one()
        for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                     'MKL_NUM_THREADS'):
            self.assertEqual(env[name], '1')


if __name__ == '__main__':
    unittest.main()

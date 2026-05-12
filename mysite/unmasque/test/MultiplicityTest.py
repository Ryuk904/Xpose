"""Unit tests for the pure logic of Algorithm 1 (MultiplicityDetect).

These tests cover the cardinality-fingerprint degree estimator without needing a
database connection.  The end-to-end probing (snapshot / inflate / Q_H execution
/ rollback) is exercised by the integration tests once a TPC-H instance with
self-join queries is available; see ``docs`` / the EQC+SJ benchmark suite.
"""
import unittest

from ..src.core.multiplicity import _finite_difference_degree, _almost_equal, DEFAULT_MAX_MULT


def _pow_seq(B, k, upto):
    return [B * (n ** k) for n in range(1, upto + 1)]


class MyTestCase(unittest.TestCase):
    KMAX = DEFAULT_MAX_MULT          # 4
    UPTO = DEFAULT_MAX_MULT + 2      # we sample f(1) .. f(kmax+2)

    # --- the easy, exact n**k cases (equi-join self-joins) -----------------
    def test_mult_one_linear(self):
        # A single, non-self-joined table: |Q_H| grows linearly with |T|.
        for B in (1, 3, 7):
            self.assertEqual(_finite_difference_degree(_pow_seq(B, 1, self.UPTO), self.KMAX), 1)

    def test_mult_two(self):
        for B in (1, 2, 5, 13):
            self.assertEqual(_finite_difference_degree(_pow_seq(B, 2, self.UPTO), self.KMAX), 2)

    def test_mult_three(self):
        self.assertEqual(_finite_difference_degree(_pow_seq(1, 3, self.UPTO), self.KMAX), 3)
        self.assertEqual(_finite_difference_degree(_pow_seq(4, 3, self.UPTO), self.KMAX), 3)

    def test_mult_four(self):
        self.assertEqual(_finite_difference_degree(_pow_seq(1, 4, self.UPTO), self.KMAX), 4)

    def test_above_kmax_is_clamped(self):
        # A 5-way self-join is reported as 4 under assumption SJ-A1 (kmax=4).
        self.assertEqual(_finite_difference_degree(_pow_seq(1, 5, self.UPTO + 2), self.KMAX), self.KMAX)

    # --- the C(n,k) shape (non-strict cross-alias inequalities) ------------
    def test_combinatorial_growth_keeps_degree(self):
        # |Q_H| ~ C(n+1, 2) * c  -- still a degree-2 polynomial in n.
        c = 9
        seq = [c * ((n + 1) * n // 2) for n in range(1, self.UPTO + 1)]   # 9,27,54,90,...
        self.assertEqual(_finite_difference_degree(seq, self.KMAX), 2)

    def test_polynomial_mixture(self):
        # 5 n^2 + 3 n + 11  -- leading term dominates the difference table.
        seq = [5 * n * n + 3 * n + 11 for n in range(1, self.UPTO + 1)]
        self.assertEqual(_finite_difference_degree(seq, self.KMAX), 2)

    # --- degenerate cases --------------------------------------------------
    def test_constant_plateau(self):
        # GROUP BY / DISTINCT suppression flattens the cardinality.
        self.assertEqual(_finite_difference_degree([6, 6, 6, 6, 6, 6], self.KMAX), 0)

    def test_almost_equal(self):
        self.assertTrue(_almost_equal(1.0, 1.0))
        self.assertTrue(_almost_equal(1e9, 1e9 + 1e-3))   # within relative eps
        self.assertFalse(_almost_equal(4.0, 5.0))

    def test_single_point(self):
        self.assertEqual(_finite_difference_degree([42], self.KMAX), 0)


if __name__ == '__main__':
    unittest.main()

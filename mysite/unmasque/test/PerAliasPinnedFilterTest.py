"""Unit tests for the pure logic of the per-(alias, attribute) discriminator probe
(report section F -- :mod:`per_alias_pinned_filter`).

Covers ``recover_bound_via_fit_probe`` -- the per-alias FIT/UNFIT binary search
that, once an inter-alias chain has pinned an alias to a row, reads off that
alias's filter bound on another column.  The DB-coupled part (discriminating the
chain column, materialising the k rows, the rolled-back transaction) is exercised
by the integration tests once a TPC-H instance with self-join queries is available
(MultiInstancePipelineTest.py).
"""
import datetime
import unittest

from ..src.core.per_alias_pinned_filter import recover_bound_via_fit_probe


class MyTestCase(unittest.TestCase):

    # --- upper bound: FIT for v <= u, UNFIT above -------------------------
    def test_upper_bound_int(self):
        # alias' interval on c is (-inf, 40]; start inside it at 15.
        fit = lambda v: v <= 40
        self.assertEqual(recover_bound_via_fit_probe(fit, 15, 'integer', +1), 40)

    def test_upper_bound_none_when_unbounded(self):
        fit = lambda v: True                       # FIT everywhere -> no upper bound
        self.assertIsNone(recover_bound_via_fit_probe(fit, 15, 'integer', +1))

    def test_lower_bound_int(self):
        # interval [10, +inf); start at 15.
        fit = lambda v: v >= 10
        self.assertEqual(recover_bound_via_fit_probe(fit, 15, 'integer', -1), 10)

    def test_lower_bound_none_when_unbounded(self):
        fit = lambda v: True
        self.assertIsNone(recover_bound_via_fit_probe(fit, 15, 'integer', -1))

    def test_both_sides_bounded_interval(self):
        fit = lambda v: 10 <= v <= 40
        self.assertEqual(recover_bound_via_fit_probe(fit, 25, 'integer', +1), 40)
        self.assertEqual(recover_bound_via_fit_probe(fit, 25, 'integer', -1), 10)

    # --- float / numeric --------------------------------------------------
    def test_upper_bound_numeric(self):
        # the numeric domain endpoint is ~2.1e9, so a 40-step bisection pins this to
        # ~milli precision (not 1e-6) -- enough to be a useful per-alias bound.
        fit = lambda v: v <= 3.5
        got = recover_bound_via_fit_probe(fit, 1.0, 'numeric', +1)
        self.assertIsNotNone(got)
        self.assertLess(abs(got - 3.5), 0.01)

    # --- date -------------------------------------------------------------
    def test_upper_bound_date(self):
        cutoff = datetime.date(1998, 9, 2)
        fit = lambda v: v <= cutoff
        got = recover_bound_via_fit_probe(fit, datetime.date(1995, 1, 1), 'date', +1)
        self.assertIsNotNone(got)
        self.assertLessEqual(abs((got - cutoff).days), 1)

    def test_lower_bound_date(self):
        cutoff = datetime.date(1993, 1, 1)
        fit = lambda v: v >= cutoff
        got = recover_bound_via_fit_probe(fit, datetime.date(1995, 1, 1), 'date', -1)
        self.assertIsNotNone(got)
        self.assertLessEqual(abs((got - cutoff).days), 1)

    # --- degenerate / guard cases ----------------------------------------
    def test_probe_raising_is_safe(self):
        def boom(v):
            raise RuntimeError("inserting the domain endpoint overflowed")
        self.assertIsNone(recover_bound_via_fit_probe(boom, 15, 'integer', +1))

    def test_start_at_or_beyond_endpoint(self):
        # start value already at the domain endpoint -> nothing to search on that side.
        from ..src.core.per_alias_filter import domain_endpoint
        hi = domain_endpoint('integer', +1)
        self.assertIsNone(recover_bound_via_fit_probe(lambda v: v <= hi, hi, 'integer', +1))

    def test_k3_style_three_aliases_each_own_bound(self):
        # simulate: alias 1 -> c <= 100, alias 2 -> c <= 200, alias 3 -> c <= 300
        for u in (100, 200, 300):
            fit = lambda v, _u=u: v <= _u
            self.assertEqual(recover_bound_via_fit_probe(fit, 50, 'integer', +1), u)


if __name__ == '__main__':
    unittest.main()

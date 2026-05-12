"""Unit tests for the pure logic of Algorithm 4 (per-alias filter extraction).

Covers the step-function break-point search and the int/float/date value
arithmetic it relies on, with mock probe oracles.  No database connection needed;
the end-to-end behaviour (the two-row {A, A-with-c=V} probe against a live
executable, the outward domain probe, the rolled-back transaction) is exercised
by the integration tests once a TPC-H instance with self-join queries is available.
"""
import datetime
import unittest

from ..src.core.per_alias_filter import (find_step_breakpoints, _midpoint, _adjacent, _add,
                                         _alias_mult, _is_int, _is_float, _is_date, _orderable)


class MyTestCase(unittest.TestCase):

    # --- type predicates / arithmetic -------------------------------------
    def test_type_predicates(self):
        self.assertTrue(_is_int('integer') and _is_int('bigint') and _is_int('smallint'))
        self.assertTrue(_is_float('numeric') and _is_float('double precision'))
        self.assertTrue(_is_date('date') and _is_date('timestamp without time zone'))
        self.assertFalse(_is_date('interval'))
        self.assertTrue(_orderable('integer') and _orderable('numeric') and _orderable('date'))
        self.assertFalse(_orderable('character varying'))

    def test_add(self):
        self.assertEqual(_add(10, 5, 'integer'), 15)
        self.assertEqual(_add(1.5, 2.0, 'numeric'), 3.5)
        self.assertEqual(_add(datetime.date(2020, 1, 1), 10, 'date'), datetime.date(2020, 1, 11))

    def test_midpoint_and_adjacent(self):
        self.assertEqual(_midpoint(0, 10, 'integer'), 5)
        self.assertTrue(_adjacent(4, 5, 'integer'))
        self.assertFalse(_adjacent(4, 6, 'integer'))
        d0, d1 = datetime.date(2020, 1, 1), datetime.date(2020, 1, 11)
        self.assertEqual(_midpoint(d0, d1, 'date'), datetime.date(2020, 1, 6))
        self.assertTrue(_adjacent(d0, datetime.date(2020, 1, 2), 'date'))

    # --- break-point search -- returns (low_v, high_v, from_card, to_card) -----
    def test_three_distinct_upper_bounds(self):
        # f(V) = 8 if V<=10, 4 if V<=20, 2 if V<=30, 1 otherwise  (k=3)
        def f(V):
            return 8 if V <= 10 else (4 if V <= 20 else (2 if V <= 30 else 1))
        bps = find_step_breakpoints(f, 5, 100, f(5), f(100), 'integer', [40])
        self.assertEqual([(b[0], b[1]) for b in bps], [(10, 11), (20, 21), (30, 31)])
        self.assertEqual([(b[2], b[3]) for b in bps], [(8, 4), (4, 2), (2, 1)])  # each jump = 1 alias

    def test_uniform_no_filter(self):
        self.assertEqual(find_step_breakpoints(lambda V: 8, 5, 100, 8, 8, 'integer', [40]), [])

    def test_single_upper_bound(self):
        def g(V):
            return 8 if V <= 42 else 4
        bps = find_step_breakpoints(g, 5, 100, g(5), g(100), 'integer', [40])
        self.assertEqual([(b[0], b[1]) for b in bps], [(42, 43)])

    def test_uniform_bound_jump_is_full(self):
        # k=2: f drops 4 -> 1 at v=42  (both aliases bounded there)
        def g(V):
            return 4 if V <= 42 else 1
        bps = find_step_breakpoints(g, 5, 100, g(5), g(100), 'integer', [40])
        self.assertEqual([(b[0], b[2], b[3]) for b in bps], [(42, 4, 1)])

    def test_lower_bound_side(self):
        # the lower bound is the *high* endpoint of the transition
        def h(V):
            return 8 if V >= 10 else 4
        bps = find_step_breakpoints(h, -100, 50, h(-100), h(50), 'integer', [40])
        self.assertEqual([(b[0], b[1]) for b in bps], [(9, 10)])

    def test_float_bound(self):
        def ff(V):
            return 6 if V <= 3.5 else 2
        bps = find_step_breakpoints(ff, 0.0, 10.0, ff(0.0), ff(10.0), 'numeric', [60])
        self.assertTrue(abs(bps[0][0] - 3.5) < 1e-3)

    def test_date_bound(self):
        d0 = datetime.date(1992, 1, 1)
        cutoff = datetime.date(1998, 9, 2)

        def dd(V):
            return 4 if V <= cutoff else 1
        bps = find_step_breakpoints(dd, d0, datetime.date(2010, 1, 1), 4, 1, 'date', [60])
        self.assertLessEqual(abs((bps[0][0] - cutoff).days), 1)

    def test_budget_exhaustion_is_safe(self):
        # tiny budget: should return *something* (possibly approximate), not loop
        def f(V):
            return 2 if V <= 50 else 1
        bps = find_step_breakpoints(f, 0, 1000, 2, 1, 'integer', [3])
        self.assertTrue(all(isinstance(b[0], int) for b in bps))

    # --- per-alias multiplicity from the cardinality jump --------------------
    def test_alias_mult(self):
        self.assertEqual(_alias_mult(8, 4, 4), 1)    # /2  -> 1 alias
        self.assertEqual(_alias_mult(4, 1, 2), 2)    # /4  -> 2 aliases
        self.assertEqual(_alias_mult(8, 1, 3), 3)    # /8  -> 3 aliases
        self.assertEqual(_alias_mult(8, 1, 2), 2)    # /8 but clamped to k=2
        self.assertEqual(_alias_mult(5, 5, 3), 1)    # no drop -> 1
        self.assertEqual(_alias_mult(5, 0, 3), 1)    # degenerate -> 1


if __name__ == '__main__':
    unittest.main()

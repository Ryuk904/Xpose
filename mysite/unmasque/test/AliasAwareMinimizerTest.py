"""Unit tests for the pure logic of Algorithm 2 (alias-aware k-coloured halving).

Covers the colour partitioning and the halving routine with mock FIT oracles --
no database connection needed.  The end-to-end behaviour (rebuilding a relation
from a candidate pool, the FIT-guided shrink against a live executable, and the
rolled-back transaction) is exercised by the integration tests once a TPC-H
instance with self-join queries is available.
"""
import unittest

from ..src.core.alias_aware_minimizer import split_into_k_blocks, _flatten, kcolour_halve


class MyTestCase(unittest.TestCase):

    # --- partitioning ------------------------------------------------------
    def test_split_blocks_even(self):
        self.assertEqual(split_into_k_blocks([0, 1, 2, 3, 4, 5], 3), [[0, 1], [2, 3], [4, 5]])

    def test_split_blocks_uneven(self):
        self.assertEqual(split_into_k_blocks([0, 1, 2, 3, 4, 5, 6], 3), [[0, 1, 2], [3, 4], [5, 6]])

    def test_split_blocks_fewer_items_than_k(self):
        self.assertEqual(split_into_k_blocks([0, 1], 4), [[0], [1], [], []])

    def test_split_then_flatten_roundtrips(self):
        for n in range(0, 13):
            for k in range(1, 6):
                items = list(range(n))
                self.assertEqual(_flatten(split_into_k_blocks(items, k)), items)
                self.assertEqual(len(split_into_k_blocks(items, k)), k)

    # --- the halving routine ----------------------------------------------
    def test_k1_reduces_to_one(self):
        # mult == 1: any single row keeps the query FIT.
        self.assertEqual(kcolour_halve(list(range(50)), 1, lambda cs: len(cs) >= 1), [49])

    def test_two_specific_witness_rows(self):
        # Q_H FIT iff both row 7 and row 42 are present.
        out = kcolour_halve(list(range(100)), 2, lambda cs: {7, 42}.issubset(set(cs)))
        self.assertEqual(sorted(out), [7, 42])

    def test_idempotent_self_join_keeps_floor(self):
        # Q_H FIT iff row 7 is present (one row is enough -- the diagonal),
        # but mult == 2, so the floor of 2 must be respected.
        out = kcolour_halve(list(range(100)), 2, lambda cs: 7 in set(cs))
        self.assertEqual(len(out), 2)
        self.assertIn(7, out)

    def test_three_way_chain(self):
        out = kcolour_halve(list(range(100)), 3, lambda cs: {10, 20, 30}.issubset(set(cs)))
        self.assertEqual(sorted(out), [10, 20, 30])

    def test_query_needs_more_rows_than_k(self):
        # mult lower bound is 2 but Q_H genuinely needs 3 specific rows -- the
        # routine must not strand the query in an UNFIT state.
        out = kcolour_halve(list(range(100)), 2, lambda cs: {5, 55, 95}.issubset(set(cs)))
        self.assertEqual(sorted(out), [5, 55, 95])

    def test_never_below_k_even_if_one_row_suffices(self):
        out = kcolour_halve(list(range(20)), 3, lambda cs: len(cs) >= 1)
        self.assertEqual(len(out), 3)

    def test_already_at_k(self):
        self.assertEqual(sorted(kcolour_halve([2, 5], 2, lambda cs: len(set(cs)) >= 2)), [2, 5])


if __name__ == '__main__':
    unittest.main()

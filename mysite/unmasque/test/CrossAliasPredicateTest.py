"""Unit tests for the pure logic of Algorithm 3 (cross-alias predicate extraction).

Covers the discriminator-window construction (`spread_values`), the window lookup,
and the inference of `t_p.c REL t_q.c` predicates from the output of Q_H on the
discriminated D_min.  No database connection needed; the actual probing
(re-materialising the relation, the FIT-guided discriminator search, the
rolled-back transaction) is exercised by the integration tests once a TPC-H
instance with self-join queries is available.
"""
import datetime
import unittest

from ..src.core.cross_alias_predicate import (spread_values, _window_index,
                                              infer_inter_alias_predicates,
                                              attribute_output_columns)


class MyTestCase(unittest.TestCase):

    # --- spread_values -----------------------------------------------------
    def test_spread_equal_ints(self):
        self.assertEqual(spread_values([5, 5], 2, 'integer'), [5, 6])
        self.assertEqual(spread_values([5, 5, 5], 3, 'bigint'), [5, 6, 7])

    def test_spread_int_range(self):
        self.assertEqual(spread_values([10, 40], 3, 'integer'), [10, 25, 40])

    def test_spread_int_range_too_narrow(self):
        self.assertIsNone(spread_values([10, 11], 3, 'integer'))

    def test_spread_float(self):
        self.assertEqual(spread_values([1.0, 4.0], 3, 'numeric'), [1.0, 2.5, 4.0])

    def test_spread_dates(self):
        d0, d1 = datetime.date(2020, 1, 1), datetime.date(2020, 1, 11)
        self.assertEqual(spread_values([d0, d1], 3, 'date'),
                         [datetime.date(2020, 1, 1), datetime.date(2020, 1, 6), datetime.date(2020, 1, 11)])

    def test_spread_text_unsupported(self):
        self.assertIsNone(spread_values(['a', 'b'], 2, 'character varying'))

    # --- window lookup -----------------------------------------------------
    def test_window_index(self):
        self.assertEqual(_window_index(25, [10, 25, 40]), 2)
        self.assertEqual(_window_index('25', [10, 25, 40]), 2)   # string fallback
        self.assertIsNone(_window_index(99, [10, 25, 40]))

    # --- inference ---------------------------------------------------------
    def test_infer_less_than(self):
        # SELECT t1.x, t2.x FROM R t1, R t2 WHERE t1.x < t2.x ; windows x=[10,20]
        out = [('t1_x', 't2_x'), (10, 20)]
        self.assertEqual(infer_inter_alias_predicates(out, {'x': [10, 20]}), [('x', 0, 1, '<')])

    def test_infer_greater_than(self):
        out = [('t1_x', 't2_x'), (20, 10)]
        self.assertEqual(infer_inter_alias_predicates(out, {'x': [10, 20]}), [('x', 0, 1, '>')])

    def test_infer_equi(self):
        # t1.x = t2.x ; both diagonal combos appear
        out = [('t1_x', 't2_x'), (10, 10), (20, 20)]
        self.assertEqual(infer_inter_alias_predicates(out, {'x': [10, 20]}), [('x', 0, 1, '=')])

    def test_infer_no_relation(self):
        # no cross-alias predicate -> all four combos
        out = [('t1_x', 't2_x'), (10, 10), (10, 20), (20, 10), (20, 20)]
        self.assertEqual(infer_inter_alias_predicates(out, {'x': [10, 20]}), [])

    def test_infer_three_way_chain(self):
        out = [('a', 'b', 'c'), (5, 15, 25)]
        self.assertEqual(infer_inter_alias_predicates(out, {'x': [5, 15, 25]}),
                         [('x', 0, 1, '<'), ('x', 0, 2, '<'), ('x', 1, 2, '<')])

    def test_infer_ignores_columns_with_one_slot(self):
        # only one output slot exposes the discriminated column -> nothing inferable
        out = [('t1_x', 'other'), (10, 99), (20, 99)]
        self.assertEqual(infer_inter_alias_predicates(out, {'x': [10, 20]}), [])

    def test_infer_empty_output(self):
        self.assertEqual(infer_inter_alias_predicates([], {'x': [10, 20]}), [])
        self.assertEqual(infer_inter_alias_predicates([('a',)], {'x': [10, 20]}), [])

    # --- output-column alias attribution (the projection alias-lift) -------
    def test_attribution_two_way_chain(self):
        # SELECT t1.x, t2.x with t1.x < t2.x ; windows x=[10,20] ; only combo (10,20) survives
        self.assertEqual(attribute_output_columns([('a', 'b'), (10, 20)], {'x': [10, 20]}),
                         {0: (1, 'x'), 1: (2, 'x')})

    def test_attribution_skips_varying_columns(self):
        # t1.x = t2.x : both output cols vary across rows -> nothing pinned
        self.assertEqual(attribute_output_columns([('a', 'b'), (10, 10), (20, 20)], {'x': [10, 20]}), {})

    def test_attribution_constant_columns_only(self):
        out = [('a', 'b', 'c'), (10, 30, 99), (10, 30, 99)]
        self.assertEqual(attribute_output_columns(out, {'x': [10, 20], 'y': [30, 40]}),
                         {0: (1, 'x'), 1: (1, 'y')})

    def test_attribution_drops_ambiguous_value(self):
        # value 10 matches both x's window 1 and y's window 1 -> ambiguous -> omitted
        self.assertEqual(attribute_output_columns([('a',), (10,)], {'x': [10, 20], 'y': [10, 30]}), {})

    def test_attribution_empty(self):
        self.assertEqual(attribute_output_columns([], {'x': [1, 2]}), {})


if __name__ == '__main__':
    unittest.main()

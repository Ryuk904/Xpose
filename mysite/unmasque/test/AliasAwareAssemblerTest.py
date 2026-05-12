"""Unit tests for the pure logic of the alias-aware query assembler.

Covers clause splitting, alias-qualification, slot ordering, and end-to-end
candidate-query construction.  No database connection needed; the pipeline
wrapper (`AliasAwareAssembler`, which fetches column lists) is exercised by the
integration tests once a TPC-H instance with self-join queries is available.
"""
import unittest

from ..src.core.alias_aware_assembler import (_split_clauses, _qualify_text, _qualify_select,
                                              _rewrite_select, _qualify_clause, _AMBIGUOUS,
                                              _topo_order_slots, _result_multiset, _strip_col_atoms,
                                              build_alias_aware_query)


_ALIAS = (lambda t, i: f"{t}_a{i}")


class MyTestCase(unittest.TestCase):

    # --- clause splitting --------------------------------------------------
    def test_split_basic(self):
        c = _split_clauses("Select l_partkey, avg(l_quantity) From lineitem "
                           "Where lineitem.l_shipdate <= '1998-09-02' "
                           "Group By l_partkey Order By l_partkey Limit 100;")
        self.assertEqual(c['select'].strip(), 'l_partkey, avg(l_quantity)')
        self.assertEqual(c['from'].strip(), 'lineitem')
        self.assertIn('l_shipdate', c['where'])
        self.assertEqual(c['group by'].strip(), 'l_partkey')
        self.assertEqual(c['order by'].strip(), 'l_partkey')
        self.assertEqual(c['limit'].strip(), '100')

    def test_split_rejects_union_and_subquery(self):
        self.assertIsNone(_split_clauses("(select * from a) union all (select * from b);"))
        self.assertIsNone(_split_clauses("select x from a where x in (select y from b);"))
        self.assertIsNone(_split_clauses(None))
        self.assertIsNone(_split_clauses(""))

    # --- qualification -----------------------------------------------------
    def test_qualify_text_qualified_and_bare(self):
        self.assertEqual(
            _qualify_text("l_partkey = orders.o_orderkey and lineitem.l_quantity > 5",
                          "lineitem", "lineitem_a1", ["l_partkey", "l_orderkey", "l_quantity"]),
            "lineitem_a1.l_partkey = orders.o_orderkey and lineitem_a1.l_quantity > 5")

    def test_qualify_text_leaves_string_literals(self):
        self.assertEqual(
            _qualify_text("lineitem.l_comment like '%l_partkey%'",
                          "lineitem", "lineitem_a1", ["l_comment", "l_partkey"]),
            "lineitem_a1.l_comment like '%l_partkey%'")

    def test_qualify_select_keeps_alias_names(self):
        out = _qualify_select("l_partkey, avg(l_quantity) as l_quantity",
                              "lineitem", "lineitem_a1", ["l_partkey", "l_quantity"])
        # the column inside avg() is qualified; the AS-name is not
        self.assertIn("avg(lineitem_a1.l_quantity)", out)
        self.assertTrue(out.rstrip().endswith("as l_quantity"))
        self.assertIn("lineitem_a1.l_partkey", out)

    # --- slot ordering -----------------------------------------------------
    def test_topo_two_way(self):
        self.assertEqual(_topo_order_slots([{'kind': 'inter', 'col': 'x', 'op': '<', 'slots': (0, 1)}]),
                         [0, 1])

    def test_topo_three_way_chain(self):
        preds = [{'kind': 'inter', 'col': 'x', 'op': '<', 'slots': (0, 1)},
                 {'kind': 'inter', 'col': 'x', 'op': '<', 'slots': (0, 2)},
                 {'kind': 'inter', 'col': 'x', 'op': '<', 'slots': (1, 2)}]
        self.assertEqual(_topo_order_slots(preds), [0, 1, 2])

    def test_topo_equi_merges(self):
        self.assertEqual(len(_topo_order_slots([{'kind': 'inter', 'col': 'x', 'op': '=', 'slots': (0, 1)}])), 1)

    # --- end to end --------------------------------------------------------
    def test_assemble_selfjoin_with_equi_and_inequality(self):
        legacy = "Select partsupp.ps_partkey, partsupp.ps_supplycost From partsupp Where partsupp.ps_availqty >= 1;"
        cand, notes = build_alias_aware_query(
            legacy, {'partsupp': 2},
            {'partsupp': [{'kind': 'inter', 'col': 'ps_supplycost', 'op': '<', 'slots': (1, 2)}]},
            {}, {'partsupp': ['ps_partkey', 'ps_suppkey', 'ps_availqty', 'ps_supplycost', 'ps_comment']},
            {'partsupp': ['ps_partkey']})
        self.assertIn("partsupp AS partsupp_a1, partsupp AS partsupp_a2", cand)
        self.assertIn("partsupp_a1.ps_partkey", cand)
        self.assertIn("partsupp_a1.ps_partkey = partsupp_a2.ps_partkey", cand)        # coupled -> equi chain
        self.assertIn("partsupp_a1.ps_supplycost < partsupp_a2.ps_supplycost", cand)  # inter pred
        self.assertIn("partsupp_a1.ps_availqty >= 1", cand)                           # legacy filter rebound
        self.assertTrue(any("syntactic reconstruction" in n for n in notes))

    def test_assemble_per_alias_filters(self):
        # t1.x <= 10, t2.x <= 20
        cand, _ = build_alias_aware_query(
            "Select emp.name From emp Where emp.x <= 10;",
            {'emp': 2}, {}, {'emp': {'x': {'upper': [10, 20], 'upper_multiset': [10, 20],
                                           'lower': [], 'lower_multiset': []}}},
            {'emp': ['id', 'name', 'x']}, {})
        self.assertIn("emp_a1.x <= 10", cand)        # legacy tightest, rebound to a1
        self.assertIn("emp_a2.x <= 20", cand)        # looser bound -> a2

    def test_assemble_per_alias_filters_uniform(self):
        # both aliases bounded at the same value: multiset = [10, 10] (k=2).
        cand, _ = build_alias_aware_query(
            "Select emp.name From emp Where emp.x <= 10;",
            {'emp': 2}, {}, {'emp': {'x': {'upper': [10], 'upper_multiset': [10, 10],
                                           'lower': [], 'lower_multiset': []}}},
            {'emp': ['id', 'name', 'x']}, {})
        nc = " ".join(cand.split())
        self.assertIn("emp_a1.x <= 10", nc)
        self.assertIn("emp_a2.x <= 10", nc)          # the fix: a2 is bounded too

    def test_assemble_per_alias_filters_one_alias_bounded(self):
        # only t1 has the bound (multiset = [10], one entry); t2 is free.
        cand, _ = build_alias_aware_query(
            "Select emp.name From emp Where emp.x <= 10;",
            {'emp': 2}, {}, {'emp': {'x': {'upper': [10], 'upper_multiset': [10],
                                           'lower': [], 'lower_multiset': []}}},
            {'emp': ['id', 'name', 'x']}, {})
        nc = " ".join(cand.split())
        self.assertIn("emp_a1.x <= 10", nc)
        self.assertNotIn("emp_a2.x <= 10", nc)       # a2 stays unbounded

    def test_assemble_per_alias_filters_three_aliases_shared(self):
        # k=3, two aliases bounded at 5 and one at 9: multiset = [5, 5, 9].
        cand, _ = build_alias_aware_query(
            "Select emp.name From emp Where emp.x <= 5;",
            {'emp': 3}, {}, {'emp': {'x': {'upper': [5, 9], 'upper_multiset': [5, 5, 9],
                                           'lower': [], 'lower_multiset': []}}},
            {'emp': ['id', 'name', 'x']}, {})
        nc = " ".join(cand.split())
        self.assertIn("emp_a1.x <= 5", nc)           # legacy tightest -> a1
        self.assertIn("emp_a2.x <= 5", nc)           # second copy of the tightest -> a2
        self.assertIn("emp_a3.x <= 9", nc)           # the looser one -> a3

    def test_result_multiset(self):
        self.assertEqual(_result_multiset([('a', 'b'), (2, 'y'), (1, 'x'), (2, 'y')]),
                         [('1', 'x'), ('2', 'y'), ('2', 'y')])           # multiset, sorted, stringified
        self.assertEqual(_result_multiset([('header_only',)]), [])
        self.assertIsNone(_result_multiset("error string"))
        self.assertIsNone(_result_multiset([]))

    # --- per-(alias, attribute) probe outputs + verifier-guided variants ----
    def test_strip_col_atoms(self):
        self.assertEqual(_strip_col_atoms("emp_a1.x <= 10 and emp_a1.y > 5", "emp_a1.x"), "emp_a1.y > 5")
        self.assertEqual(_strip_col_atoms("emp_a1.y > 5 and emp_a1.x <= 10", "emp_a1.x"), "emp_a1.y > 5")
        self.assertEqual(_strip_col_atoms("emp_a1.x <= 10", "emp_a1.x"), "")
        self.assertEqual(_strip_col_atoms("emp_a1.x between 1 and 9 and emp_a1.y > 5", "emp_a1.x"),
                         "emp_a1.y > 5")
        self.assertEqual(_strip_col_atoms("a.b = 'x' and emp_a1.x = 'foo' and a.c > 1", "emp_a1.x"),
                         "a.b = 'x' and a.c > 1")
        self.assertEqual(_strip_col_atoms("", "emp_a1.x"), "")

    def test_assemble_with_pinned_filters(self):
        # the probe attributed: a1's upper bound on x is 10, a2's is 20.
        cand, notes = build_alias_aware_query(
            "Select emp.name From emp Where emp.x <= 10;", {'emp': 2},
            {'emp': [{'kind': 'inter', 'col': 'd', 'op': '<', 'slots': (0, 1)}]},
            {'emp': {'x': {'upper': [10, 20], 'upper_multiset': [10, 20], 'lower': [], 'lower_multiset': []}}},
            {'emp': ['id', 'name', 'x', 'd']}, {}, None,
            pinned_filters={'emp': {1: {'x': {'upper': 10, 'lower': None}},
                                    2: {'x': {'upper': 20, 'lower': None}}}})
        nc = " ".join(cand.split())
        self.assertIn("emp_a1.x <= 10", nc)
        self.assertIn("emp_a2.x <= 20", nc)
        self.assertIn("emp_a1.d < emp_a2.d", nc)
        self.assertTrue(any("attributed by the discriminator probe" in n for n in notes))

    def test_assemble_with_pinned_filters_swapped(self):
        # the OTHER attribution: a1's upper is 20, a2's is 10 -- the legacy a1.x<=10 atom
        # (too tight for a1) must be stripped.
        cand, _ = build_alias_aware_query(
            "Select emp.name From emp Where emp.x <= 10;", {'emp': 2},
            {'emp': [{'kind': 'inter', 'col': 'd', 'op': '<', 'slots': (0, 1)}]},
            {'emp': {'x': {'upper': [10, 20], 'upper_multiset': [10, 20], 'lower': [], 'lower_multiset': []}}},
            {'emp': ['id', 'name', 'x', 'd']}, {}, None,
            pinned_filters={'emp': {1: {'x': {'upper': 20, 'lower': None}},
                                    2: {'x': {'upper': 10, 'lower': None}}}})
        nc = " ".join(cand.split())
        self.assertIn("emp_a1.x <= 20", nc)
        self.assertIn("emp_a2.x <= 10", nc)
        self.assertNotIn("emp_a1.x <= 10", nc)

    def test_assemble_reverse_tails(self):
        # k=3, upper multiset [5,7,9]: default tail -> a2<=7, a3<=9; reversed -> a2<=9, a3<=7.
        cand, _ = build_alias_aware_query(
            "Select emp.name From emp Where emp.x <= 5;", {'emp': 3}, {},
            {'emp': {'x': {'upper': [5, 7, 9], 'upper_multiset': [5, 7, 9], 'lower': [], 'lower_multiset': []}}},
            {'emp': ['id', 'name', 'x']}, {}, None, reverse_tails=True)
        nc = " ".join(cand.split())
        self.assertIn("emp_a2.x <= 9", nc)
        self.assertIn("emp_a3.x <= 7", nc)

    # --- projection alias-lift --------------------------------------------
    def test_rewrite_select_with_attribution(self):
        mi = {'lineitem': 2}
        cols = {'lineitem': ['l_orderkey', 'l_partkey', 'l_shipdate', 'l_quantity']}
        new_sel, col_attr = _rewrite_select(
            "l_shipdate, lineitem.l_shipdate", mi, cols,
            {0: ('lineitem', 1, 'l_shipdate'), 1: ('lineitem', 2, 'l_shipdate')}, _ALIAS)
        self.assertEqual(new_sel, "lineitem_a1.l_shipdate, lineitem_a2.l_shipdate")
        self.assertEqual(col_attr, {('lineitem', 'l_shipdate'): _AMBIGUOUS})

    def test_rewrite_select_aggregate_arg_attributed(self):
        mi = {'lineitem': 2}
        cols = {'lineitem': ['l_partkey', 'l_quantity', 'l_shipdate']}
        # avg(l_quantity) attributed to alias 2 -> avg(lineitem_a2.l_quantity)
        new_sel, col_attr = _rewrite_select(
            "l_partkey, avg(l_quantity)", mi, cols,
            {0: ('lineitem', 1, 'l_partkey'), 1: ('lineitem', 2, 'l_quantity')}, _ALIAS)
        self.assertEqual(new_sel, "lineitem_a1.l_partkey, avg(lineitem_a2.l_quantity)")
        self.assertEqual(col_attr, {('lineitem', 'l_partkey'): 'lineitem_a1'})  # only plain colrefs
        # ... with an AS name
        self.assertEqual(_rewrite_select("avg(lineitem.l_quantity) as q", mi, cols,
                                         {0: ('lineitem', 2, 'l_quantity')}, _ALIAS)[0],
                         "avg(lineitem_a2.l_quantity) as q")

    def test_rewrite_select_count_never_uses_attribution(self):
        mi, cols = {'lineitem': 2}, {'lineitem': ['l_partkey']}
        self.assertEqual(_rewrite_select("count(l_partkey)", mi, cols,
                                         {0: ('lineitem', 2, 'l_partkey')}, _ALIAS)[0],
                         "count(lineitem_a1.l_partkey)")
        self.assertEqual(_rewrite_select("Count(*), l_partkey", mi, cols,
                                         {1: ('lineitem', 1, 'l_partkey')}, _ALIAS)[0],
                         "Count(*), lineitem_a1.l_partkey")

    def test_rewrite_select_composite_and_mismatched_agg_fall_back(self):
        mi, cols = {'lineitem': 2}, {'lineitem': ['l_partkey', 'l_quantity', 'l_shipdate']}
        # aggregate over a different column than the attribution -> a1 fallback
        self.assertEqual(_rewrite_select("sum(l_quantity)", mi, cols,
                                         {0: ('lineitem', 2, 'l_shipdate')}, _ALIAS)[0],
                         "sum(lineitem_a1.l_quantity)")
        # composite expression -> qualify all column refs to a1
        self.assertEqual(_rewrite_select("l_quantity + l_partkey", mi, cols,
                                         {0: ('lineitem', 2, 'l_quantity')}, _ALIAS)[0],
                         "lineitem_a1.l_quantity + lineitem_a1.l_partkey")

    def test_rewrite_select_no_attribution_all_a1(self):
        new_sel, col_attr = _rewrite_select("l_partkey, l_shipdate", {'lineitem': 2},
                                            {'lineitem': ['l_partkey', 'l_shipdate']}, {}, _ALIAS)
        self.assertEqual(new_sel, "lineitem_a1.l_partkey, lineitem_a1.l_shipdate")
        self.assertEqual(col_attr, {})

    def test_qualify_clause_uses_projection_alias(self):
        mi, cols = {'lineitem': 2}, {'lineitem': ['l_partkey', 'l_quantity']}
        self.assertEqual(_qualify_clause("l_partkey", mi, cols,
                                         {('lineitem', 'l_partkey'): 'lineitem_a2'}, _ALIAS),
                         "lineitem_a2.l_partkey")
        self.assertEqual(_qualify_clause("l_partkey DESC", mi, cols,
                                         {('lineitem', 'l_partkey'): 'lineitem_a2'}, _ALIAS),
                         "lineitem_a2.l_partkey DESC")
        # ambiguous projection -> fall back to a1
        self.assertEqual(_qualify_clause("l_partkey", mi, cols,
                                         {('lineitem', 'l_partkey'): _AMBIGUOUS}, _ALIAS),
                         "lineitem_a1.l_partkey")

    def test_assemble_projection_aliased(self):
        # SELECT t1.l_shipdate, t2.l_shipdate FROM lineitem t1, t2 WHERE t1.l_shipdate < t2.l_shipdate
        legacy = ("Select l_shipdate, lineitem.l_shipdate From lineitem "
                  "Where lineitem.l_quantity > 5 Group By l_shipdate Order By l_shipdate;")
        cand, _ = build_alias_aware_query(
            legacy, {'lineitem': 2},
            {'lineitem': [{'kind': 'inter', 'col': 'l_shipdate', 'op': '<', 'slots': (0, 1)}]},
            {}, {'lineitem': ['l_orderkey', 'l_partkey', 'l_shipdate', 'l_quantity']},
            {}, {0: ('lineitem', 1, 'l_shipdate'), 1: ('lineitem', 2, 'l_shipdate')})
        nc = " ".join(cand.split())
        self.assertIn("Select lineitem_a1.l_shipdate, lineitem_a2.l_shipdate", nc)
        self.assertIn("lineitem AS lineitem_a1, lineitem AS lineitem_a2", nc)
        self.assertIn("lineitem_a1.l_quantity > 5", nc)
        self.assertIn("lineitem_a1.l_shipdate < lineitem_a2.l_shipdate", nc)
        # l_shipdate is projected from both aliases -> GROUP/ORDER BY ref left on a1
        self.assertIn("Group By lineitem_a1.l_shipdate", nc)

    def test_assemble_no_multi_instance(self):
        cand, notes = build_alias_aware_query("Select x From t;", {'t': 1}, {}, {}, {}, {})
        self.assertIsNone(cand)

    def test_assemble_rejects_union(self):
        cand, notes = build_alias_aware_query("(select * from a) union (select * from b);",
                                              {'a': 2}, {}, {}, {'a': ['x']}, {})
        self.assertIsNone(cand)


if __name__ == '__main__':
    unittest.main()

"""WI-06 - COUNT(*) / COUNT(col) / COUNT(DISTINCT col) refinement in Aggregation.

The pipeline reconstructs every count column as ``('', COUNT_STAR)`` because a
COUNT has no value-dependency (projection cannot find its source column). WI-06
adds ``Aggregation._refine_counts``: on the single-witness instance D1 it runs
controlled multiplicity probes (enabler S2) to split that blanket label into the
real construct -

  * step 1 (distinctness): exact-duplicate one contributing witness row. A
    non-distinct count rises 1->2; ``COUNT(DISTINCT col)`` sees a repeated value
    and stays 1.
  * step 2a (distinct): insert a witness-copy with a *fresh distinct* value in a
    candidate column; only the truly distinct-counted column lifts the count.
  * step 2b (non-distinct companion): null-inject a candidate column; only the
    counted column's NULL is skipped (count unchanged), with a survival guard.

These tests drive the *real* ``Aggregation`` methods (bound to a duck-typed
``self`` via ``types.MethodType``) against a synthetic oracle that computes the
count from an in-memory row set - no DB, so the classification logic is isolated
from the plumbing. They also pin the QSG render of the ``COUNT_DISTINCT`` op.
"""

import types
import unittest

from mysite.unmasque.src.core.aggregation import Aggregation, _max_int_in_result_col
from mysite.unmasque.src.util.constants import COUNT, COUNT_STAR, COUNT_DISTINCT
from mysite.unmasque.src.util.ConnectionFactory import ConnectionHelperFactory
from mysite.unmasque.src.util.QueryStringGenerator import QueryStringGenerator


class _DummyLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeTable:
    """In-memory model of one GROUP's rows. ``agg_type`` selects the hidden
    aggregate the oracle computes: ``'star'`` -> COUNT(*), ``'count'`` ->
    COUNT(agg_col) (skips NULL), ``'distinct'`` -> COUNT(DISTINCT agg_col).

    ``drop_cols`` models a column whose mutation makes the crafted row fail Qh
    (a key/filter that rejects the new value): any inserted row that differs
    from the witness on such a column is silently dropped - this is what the
    companion's survival guard must defend against."""

    def __init__(self, cols, witness, agg_type, agg_col=None, drop_cols=(), single_valued=()):
        self.cols = list(cols)
        self.witness = dict(witness)
        self.agg_type = agg_type
        self.agg_col = agg_col
        self.drop_cols = set(drop_cols)
        self.single_valued = set(single_valued)
        self.rows = [dict(self.witness)]

    def reset(self):
        self.rows = [dict(self.witness)]

    def count(self):
        if self.agg_type == 'star':
            return len(self.rows)
        if self.agg_type == 'count':
            return sum(1 for r in self.rows if r.get(self.agg_col) is not None)
        vals = {r.get(self.agg_col) for r in self.rows if r.get(self.agg_col) is not None}
        return len(vals)


class _FakeApp:
    def __init__(self, table):
        self.table = table
        self.done = True

    def doJob(self, query):
        self.done = True
        # header + one group row: (group-key, count). The count column is never
        # NULL, so the result row always survives the null-free filter.
        return [('g', 'count'), ('grp', str(self.table.count()))]

    def get_all_nullfree_rows(self, res):
        return list(res[1:])


class _FakeRowProbe:
    """Exact duplicate-by-ctid + revert, against the in-memory table."""

    def __init__(self, table):
        self.table = table
        self.dup_calls = 0
        self.del_calls = 0

    def list_ctids(self, fqn):
        return ['(0,1)']

    def duplicate_rows(self, fqn, ctids=None):
        self.dup_calls += 1
        self.table.rows.append(dict(self.table.rows[0]))  # exact copy of the witness
        return ['(0,dup)']

    def delete_rows(self, fqn, ctids):
        self.del_calls += 1
        self.table.rows.pop()


def _make_agg(table, *, group_col='g', join_graph=None, filtered=()):
    """Duck-typed Aggregation carrying exactly what ``_refine_counts`` consults.
    Result has two projected columns: [group_col, <count>] -> count is at ri=1."""
    app = _FakeApp(table)
    probe = _FakeRowProbe(table)
    cols = table.cols

    def get_different_s_val(col, tab, prev):
        if col in table.single_valued:
            return prev                       # '=' filter: no fresh value craftable
        if isinstance(prev, int):
            return prev + 1
        return str(prev) + "_x"

    def insert(att_order, attribs, rows, tab, insert_logger=True):
        for tup in rows:
            rowd = dict(zip(attribs, tup))
            changed = [c for c in attribs if rowd.get(c) != table.witness.get(c)]
            if any(c in table.drop_cols for c in changed):
                continue                      # crafted row fails Qh -> dropped
            table.rows.append(rowd)

    agg = types.SimpleNamespace(
        global_aggregated_attributes=[(group_col, ''), ('', COUNT_STAR)],
        global_projected_attributes=[group_col, ''],
        global_join_graph=join_graph or [],
        core_relations=['t'],
        global_all_attribs={'t': list(cols)},
        global_groupby_attributes=[group_col],
        filter_attrib_dict={('t', c): ('lb', 'ub') for c in filtered},
        connectionHelper=types.SimpleNamespace(config=types.SimpleNamespace(detect_count_distinct=True)),
        logger=_DummyLogger(),
        app=app,
        _row_probe=probe,
        _table=table,
        do_init=table.reset,
        get_fully_qualified_table_name=lambda t: f"unmasque.{t}",
        get_dmin_val=lambda a, t: table.witness[a],
        get_different_s_val=get_different_s_val,
        insert_attrib_vals_into_table=insert,
    )
    for name in ('_refine_counts', '_count_arg_candidates', '_distinctness_probe',
                 '_identify_distinct_column', '_identify_nonnull_count_column',
                 '_probe_count_with_col'):
        setattr(agg, name, types.MethodType(getattr(Aggregation, name), agg))
    return agg


class RefineCountsTest(unittest.TestCase):

    # ---- headline: COUNT(DISTINCT col) ----
    def test_count_distinct_is_detected_with_column(self):
        t = _FakeTable(['g', 'x', 'y'], {'g': 'F', 'x': 10, 'y': 20}, 'distinct', agg_col='x')
        agg = _make_agg(t)
        agg._refine_counts("Qh")
        self.assertEqual(('x', COUNT_DISTINCT), agg.global_aggregated_attributes[1])
        # D1 left clean (final do_init); the distinctness probe reverted its dup.
        self.assertEqual(1, len(t.rows))
        self.assertEqual(agg._row_probe.dup_calls, agg._row_probe.del_calls)

    def test_count_distinct_picks_the_right_column_not_a_neighbour(self):
        # distinct over y, with x present as a decoy candidate.
        t = _FakeTable(['g', 'x', 'y'], {'g': 'F', 'x': 10, 'y': 20}, 'distinct', agg_col='y')
        agg = _make_agg(t)
        agg._refine_counts("Qh")
        self.assertEqual(('y', COUNT_DISTINCT), agg.global_aggregated_attributes[1])

    # ---- control: COUNT(*) must stay COUNT(*) ----
    def test_count_star_stays_count_star(self):
        t = _FakeTable(['g', 'x', 'y'], {'g': 'F', 'x': 10, 'y': 20}, 'star')
        agg = _make_agg(t)
        agg._refine_counts("Qh")
        self.assertEqual(('', COUNT_STAR), agg.global_aggregated_attributes[1])

    # ---- companion: COUNT(col) on a nullable column becomes (col, COUNT) ----
    def test_nonnull_count_column_is_detected(self):
        t = _FakeTable(['g', 'x', 'y'], {'g': 'F', 'x': 10, 'y': 20}, 'count', agg_col='x')
        agg = _make_agg(t)
        agg._refine_counts("Qh")
        self.assertEqual(('x', COUNT), agg.global_aggregated_attributes[1])

    # ---- soundness: DISTINCT over a join key cannot be identified -> COUNT(*) ----
    def test_distinct_over_join_key_is_unidentified_and_left_count_star(self):
        t = _FakeTable(['g', 'x', 'y'], {'g': 'F', 'x': 10, 'y': 20}, 'distinct', agg_col='x')
        agg = _make_agg(t, join_graph=[['x', 'other_x']])   # x excluded from candidates
        agg._refine_counts("Qh")
        self.assertEqual(('', COUNT_STAR), agg.global_aggregated_attributes[1])

    # ---- soundness: DISTINCT over an '='-filtered (single-valued) column -> COUNT(*) ----
    def test_distinct_over_single_valued_filter_is_unidentified(self):
        t = _FakeTable(['g', 'x', 'y'], {'g': 'F', 'x': 10, 'y': 20}, 'distinct', agg_col='x',
                       single_valued=['x'])
        agg = _make_agg(t, filtered=['x'])
        agg._refine_counts("Qh")
        self.assertEqual(('', COUNT_STAR), agg.global_aggregated_attributes[1])

    # ---- companion survival guard: a column whose crafted row never survives is NOT
    #      mistaken for COUNT(col) just because its null-inject left the count flat ----
    def test_companion_survival_guard_blocks_false_positive(self):
        t = _FakeTable(['g', 'x', 'z'], {'g': 'F', 'x': 10, 'z': 30}, 'star', drop_cols=['z'])
        agg = _make_agg(t)
        agg._refine_counts("Qh")
        # z's null-inject leaves count flat (row dropped) but its fresh value also
        # leaves it flat -> guard fails -> stays COUNT(*).
        self.assertEqual(('', COUNT_STAR), agg.global_aggregated_attributes[1])

    # ---- guard: no count columns -> no probing, no change ----
    def test_no_count_columns_is_noop(self):
        t = _FakeTable(['g', 'x'], {'g': 'F', 'x': 10}, 'star')
        agg = _make_agg(t)
        agg.global_aggregated_attributes = [('g', ''), ('x', 'Sum')]   # no COUNT_STAR slot
        agg._refine_counts("Qh")
        self.assertEqual([('g', ''), ('x', 'Sum')], agg.global_aggregated_attributes)
        self.assertEqual(0, agg._row_probe.dup_calls)


class CandidateAndProbeTest(unittest.TestCase):

    def test_candidates_exclude_groupby_and_join_keys(self):
        t = _FakeTable(['g', 'x', 'y', 'jk'], {'g': 'F', 'x': 1, 'y': 2, 'jk': 3}, 'star')
        agg = _make_agg(t, join_graph=[['jk', 'other']])
        cands = agg._count_arg_candidates()
        self.assertIn(('t', 'x'), cands)
        self.assertIn(('t', 'y'), cands)
        self.assertNotIn(('t', 'g'), cands)    # group-by key
        self.assertNotIn(('t', 'jk'), cands)   # join key

    def test_distinctness_probe_reverts_the_duplicate(self):
        t = _FakeTable(['g', 'x'], {'g': 'F', 'x': 10}, 'star')
        agg = _make_agg(t)
        after = agg._distinctness_probe("Qh", 1)
        self.assertEqual(2, after)             # COUNT(*) rose 1 -> 2 under the dup
        self.assertEqual(1, len(t.rows))       # ... and the dup was reverted


class MaxIntInResultColTest(unittest.TestCase):

    def test_reads_the_count(self):
        self.assertEqual(2, _max_int_in_result_col([('F', '2')], 1))

    def test_max_over_rows(self):
        self.assertEqual(5, _max_int_in_result_col([('F', '5'), ('G', '3')], 1))

    def test_non_integer_is_none(self):
        self.assertIsNone(_max_int_in_result_col([('F', 'abc')], 1))

    def test_out_of_range_index_is_none(self):
        self.assertIsNone(_max_int_in_result_col([('F',)], 1))

    def test_strips_whitespace(self):
        self.assertEqual(7, _max_int_in_result_col([('F', ' 7 ')], 1))


class CountDistinctRenderTest(unittest.TestCase):
    """QSG must render the COUNT_DISTINCT op as 'Count(distinct <col>)', pulling
    the column from the aggregate tuple (the projected attribute is empty)."""

    @classmethod
    def setUpClass(cls):
        cls.conn = ConnectionHelperFactory().createConnectionHelper()

    def _render_select(self, projected, aggregated, names):
        qsg = QueryStringGenerator(self.conn)
        qsg.get_datatype = lambda ta: 'int'
        wc = qsg._workingCopy
        wc.global_projected_attributes = list(projected)
        wc.global_aggregated_attributes = list(aggregated)
        wc.projection_names = list(names)
        qsg._QueryStringGenerator__generate_select_clause()
        return wc.select_op

    def test_count_distinct_renders_with_column(self):
        out = self._render_select(
            projected=['n_name', ''],
            aggregated=[('n_name', ''), ('c_custkey', COUNT_DISTINCT)],
            names=['n_name', 'num_cust'],
        )
        self.assertIn('Count(distinct c_custkey) as num_cust', out)

    def test_count_distinct_alongside_star_and_column_count(self):
        out = self._render_select(
            projected=['o_orderstatus', '', '', ''],
            aggregated=[('o_orderstatus', ''), ('o_custkey', COUNT_DISTINCT),
                        ('', COUNT_STAR), ('o_orderkey', COUNT)],
            names=['o_orderstatus', 'd', 'total', 'c'],
        )
        self.assertEqual(
            'o_orderstatus, Count(distinct o_custkey) as d, Count(*) as total, Count(o_orderkey) as c',
            out,
        )


if __name__ == '__main__':
    unittest.main()

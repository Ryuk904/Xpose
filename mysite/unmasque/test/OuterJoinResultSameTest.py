"""WI-03 — robust outer-join candidate equivalence (Comparator EXCEPT ALL).

``OuterJoin.__remove_semantically_nonEq_queries`` decides whether a generated
outer-join candidate ``Q_E`` is semantically equivalent to the hidden query
``Qh`` by mutating the join key / a filter column on D1 and asking, for each
mutation, "do Qh and Q_E still produce the same result?" via
``__are_the_results_same``.

Before WI-03 that question was answered by an *ordered, positional* Python
row-by-row equality after a length check. SQL results are bags, so this is
fragile: two genuinely equivalent results can compare unequal merely because the
rows came back in a different order or with duplicates in different positions
(outer joins emit NULL-extended rows and ORDER BY ties are nondeterministic).

WI-03 replaces it with the proven Re/Rh diff primitive (cf.
``Comparator.run_diff_queries`` / ``is_match``): the two results are bag-equal
iff ``(Qh EXCEPT ALL Q_E)`` and ``(Q_E EXCEPT ALL Qh)`` are *both* empty, run
in-stage via ``app.doJob`` so the caller's current mutation is in effect.

These tests drive the *real* ``OuterJoin.__are_the_results_same`` /
``__bag_diff_count`` (bound to a duck-typed ``self``) with a fake ``app`` that
faithfully implements EXCEPT ALL over synthetic row bags — no DB. The fake lets
us feed the exact reordered / duplicated row sets the old positional check would
mis-classify and confirm the bag check gets them right.
"""

import types
import unittest
from collections import Counter

from mysite.unmasque.src.core.outer_join import OuterJoin

QH = "QH_RESULT"   # marker standing in for the hidden query Qh
QE = "QE_RESULT"   # marker standing in for the candidate Q_E (poss_q)


class _DummyLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeDiffApp:
    """Stands in for Postgres' EXCEPT ALL over the *mutated* D1.

    ``bags`` maps each registered query marker to the multiset of rows it
    produces. A diff query of the form
    ``select count(*) from ((<A>) except all (<B>)) as T;`` is answered with the
    true bag-difference count |A EXCEPT ALL B| = sum_r max(0, A[r] - B[r]).
    The operand order is read off the marker positions in the SQL text, exactly
    as Postgres would evaluate the left/right operands.
    """

    def __init__(self, bags, raise_on_marker=None):
        self.bags = {m: [tuple(r) for r in rows] for m, rows in bags.items()}
        self.raise_on_marker = raise_on_marker  # force a SQL failure if present
        self.calls = []

    def doJob(self, sql):
        self.calls.append(sql)
        if self.raise_on_marker and self.raise_on_marker in sql:
            raise RuntimeError("simulated SQL failure")
        present = sorted((sql.index(m), m) for m in self.bags if m in sql)
        if len(present) < 2:
            return [("count",)]  # not a recognised two-operand diff
        left, right = present[0][1], present[1][1]
        lb, rb = Counter(self.bags[left]), Counter(self.bags[right])
        diff = sum(max(0, lb[r] - rb[r]) for r in lb)
        return [("count",), (str(diff),)]


def _make_oj(bags, raise_on_marker=None):
    app = _FakeDiffApp(bags, raise_on_marker=raise_on_marker)
    self_ns = types.SimpleNamespace(app=app, logger=_DummyLogger())
    # Bind the real (name-mangled) helper so __are_the_results_same can call it.
    self_ns._OuterJoin__bag_diff_count = types.MethodType(
        OuterJoin._OuterJoin__bag_diff_count, self_ns)
    return self_ns, app


def _same(self_ns, poss_q=QE, query=QH, same=True):
    return OuterJoin._OuterJoin__are_the_results_same(self_ns, poss_q, query, same)


class AreTheResultsSameTest(unittest.TestCase):

    # ---- the bug WI-03 fixes: reordering / duplicates ----
    def test_same_rows_different_order_is_equal(self):
        # Old positional check: row 0 of Qh != row 0 of Q_E -> WRONG "not same".
        oj, _ = _make_oj({
            QH: [('a', 1), ('b', 2), ('c', 3)],
            QE: [('c', 3), ('a', 1), ('b', 2)],
        })
        self.assertTrue(_same(oj))

    def test_same_multiset_with_duplicates_reordered_is_equal(self):
        oj, _ = _make_oj({
            QH: [('x', 1), ('x', 1), ('y', 2)],
            QE: [('x', 1), ('y', 2), ('x', 1)],
        })
        self.assertTrue(_same(oj))

    def test_null_extended_rows_bag_equal_regardless_of_order(self):
        # Outer joins emit NULL-extended rows; EXCEPT ALL treats NULL=NULL.
        oj, _ = _make_oj({
            QH: [('a', None), (None, 'b'), ('a', None)],
            QE: [('a', None), ('a', None), (None, 'b')],
        })
        self.assertTrue(_same(oj))

    # ---- genuine differences must still be rejected ----
    def test_different_duplicate_counts_is_not_equal(self):
        # Same support, different multiplicity: a real bag difference.
        oj, _ = _make_oj({
            QH: [('x', 1), ('x', 1), ('y', 2)],
            QE: [('x', 1), ('y', 2), ('y', 2)],
        })
        self.assertFalse(_same(oj))

    def test_disjoint_rows_is_not_equal(self):
        oj, _ = _make_oj({QH: [('a', 1)], QE: [('b', 2)]})
        self.assertFalse(_same(oj))

    def test_subset_is_not_equal(self):
        oj, _ = _make_oj({QH: [('a', 1), ('b', 2)], QE: [('a', 1)]})
        self.assertFalse(_same(oj))

    def test_empty_vs_empty_is_equal(self):
        oj, _ = _make_oj({QH: [], QE: []})
        self.assertTrue(_same(oj))

    def test_empty_vs_nonempty_is_not_equal(self):
        oj, _ = _make_oj({QH: [], QE: [('a', 1)]})
        self.assertFalse(_same(oj))

    # ---- accumulator / short-circuit semantics ----
    def test_already_not_same_short_circuits_without_querying(self):
        # The caller threads `same` across edges; once False it stays False and
        # we must not waste DB round-trips re-confirming it.
        oj, app = _make_oj({QH: [('a', 1)], QE: [('a', 1)]})
        self.assertFalse(_same(oj, same=False))
        self.assertEqual(0, len(app.calls))

    def test_equal_runs_exactly_two_diff_queries(self):
        oj, app = _make_oj({QH: [('a', 1)], QE: [('a', 1)]})
        self.assertTrue(_same(oj))
        self.assertEqual(2, len(app.calls))  # both directions

    # ---- soundness: fail closed when the diff cannot be evaluated ----
    def test_diff_failure_is_treated_as_not_same(self):
        # Even though the bags are equal, a failed diff must NOT be accepted as
        # equivalent (never emit an outer-join variant we could not verify).
        oj, _ = _make_oj({QH: [('a', 1)], QE: [('a', 1)]}, raise_on_marker=QH)
        self.assertFalse(_same(oj))


class BagDiffCountTest(unittest.TestCase):

    def _probe(self, bags, left, right):
        _, app = _make_oj(bags)
        self_ns = types.SimpleNamespace(app=app, logger=_DummyLogger())
        return OuterJoin._OuterJoin__bag_diff_count(self_ns, left, right), app

    def test_counts_surplus_left_rows(self):
        n, _ = self._probe(
            {QH: [('a', 1), ('a', 1), ('b', 2)], QE: [('a', 1)]}, QH, QE)
        self.assertEqual(2, n)  # one surplus ('a',1) + the ('b',2)

    def test_equal_bags_diff_zero(self):
        n, _ = self._probe({QH: [('a', 1)], QE: [('a', 1)]}, QH, QE)
        self.assertEqual(0, n)

    def test_builds_parenthesised_except_all_and_strips_semicolons(self):
        n, app = self._probe({QH: [('a', 1)], QE: [('a', 1)]}, QH + ";", QE + " ;\n")
        self.assertEqual(0, n)
        sql = app.calls[0]
        self.assertIn("except all", sql.lower())
        self.assertIn(f"(({QH})", sql)          # left operand parenthesised, no ';'
        self.assertIn(f"({QE})", sql)
        self.assertNotIn(";;", sql)

    def test_empty_operand_returns_none(self):
        n, app = self._probe({QH: [('a', 1)]}, "", QH)
        self.assertIsNone(n)
        self.assertEqual(0, len(app.calls))  # never reached the DB

    def test_sql_failure_returns_none(self):
        _, app = _make_oj({QH: [('a', 1)], QE: [('a', 1)]}, raise_on_marker=QH)
        self_ns = types.SimpleNamespace(app=app, logger=_DummyLogger())
        self.assertIsNone(OuterJoin._OuterJoin__bag_diff_count(self_ns, QH, QE))

    def test_degenerate_result_returns_none(self):
        # app returns only a header (no count row) -> unparseable -> None.
        app = types.SimpleNamespace(doJob=lambda sql: [("count",)])
        self_ns = types.SimpleNamespace(app=app, logger=_DummyLogger())
        self.assertIsNone(OuterJoin._OuterJoin__bag_diff_count(self_ns, "A", "B"))


if __name__ == '__main__':
    unittest.main()

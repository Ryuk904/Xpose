"""WI-05 — const-1 vs COUNT()==1 disambiguation in the GroupBy stage.

The value heuristic in ``GroupBy.doExtractJob`` marks a projected column as the
literal constant ``1`` when every group it observed showed the string ``'1'``.
That is unsound: a genuine ``COUNT(*)`` whose value happens to be 1 in every
probed group looks identical by value. ``_confirm_const1_columns`` resolves each
such candidate with a controlled duplicate-row probe (enabler S2): on the
single-group witness instance it duplicates one contributing row — a COUNT rises
1 -> 2, the literal 1 stays 1.

These tests drive the *real* ``GroupBy._confirm_const1_columns`` /
``GroupBy._max_int_in_col`` with a synthetic oracle (no DB), so they isolate the
classification logic from the DB plumbing. The oracle deliberately models the
case the old heuristic gets wrong: a COUNT that reads 1 on the witness instance.
"""

import types
import unittest

from mysite.unmasque.src.core.groupby_clause import GroupBy


class _DummyLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeApp:
    """Result oracle. Each projected column is 'const1' (literal 1, invariant to
    multiplicity) or 'count' (value == number of contributing rows). On the
    witness instance the COUNT reads 1; each duplicated contributing row bumps it
    by one. ``empty`` simulates an unreadable (header-only) result."""

    def __init__(self, columns, empty=False):
        self.columns = columns
        self.extra = 0          # contributing rows added by duplication
        self.done = True
        self.empty = empty

    def doJob(self, query):
        self.done = True
        header = tuple(f"c{i}" for i in range(len(self.columns)))
        if self.empty:
            return [header]
        row = tuple('1' if c == 'const1' else str(1 + self.extra) for c in self.columns)
        return [header, row]

    def get_all_nullfree_rows(self, res):
        return [r for r in res[1:] if 'None' not in r]


class _FakeRowProbe:
    def __init__(self, app):
        self.app = app
        self.duplicated = []
        self.deleted = []

    def list_ctids(self, fqn):
        return ['(0,1)']

    def duplicate_rows(self, fqn, ctids=None):
        self.app.extra += 1
        self.duplicated.append((fqn, ctids))
        return ['(0,2)']

    def delete_rows(self, fqn, ctids):
        self.app.extra -= 1
        self.deleted.append((fqn, ctids))


def _make_gb(columns, empty=False):
    """A duck-typed stand-in carrying exactly the attributes
    ``_confirm_const1_columns`` consults — no DB, no singleton __init__."""
    app = _FakeApp(columns, empty=empty)
    return types.SimpleNamespace(
        core_relations=['t'],
        do_init=lambda: None,
        app=app,
        _row_probe=_FakeRowProbe(app),
        get_fully_qualified_table_name=lambda t: f"unmasque.{t}",
        logger=_DummyLogger(),
        _max_int_in_col=GroupBy._max_int_in_col,
    )


def _confirm(gb, cols):
    return GroupBy._confirm_const1_columns(gb, "Qh", cols)


class GroupByConst1Test(unittest.TestCase):

    # ---- the bug WI-05 fixes ----
    def test_count_stuck_at_one_is_reclassified_to_count(self):
        gb = _make_gb(['count'])
        # Heuristic would have called it const-1; the probe must drop it so it
        # is left empty-projected and Aggregation renders COUNT(*).
        self.assertEqual([], _confirm(gb, [0]))
        # The probe duplicated and then reverted (D left unchanged).
        self.assertEqual(1, len(gb._row_probe.duplicated))
        self.assertEqual(1, len(gb._row_probe.deleted))
        self.assertEqual(0, gb.app.extra)

    # ---- the case it must NOT break ----
    def test_literal_one_is_confirmed(self):
        gb = _make_gb(['const1'])
        self.assertEqual([0], _confirm(gb, [0]))
        self.assertEqual(0, gb.app.extra)  # still reverted

    def test_mixed_count_and_literal(self):
        # column 0 is a COUNT==1, column 1 is a literal 1.
        gb = _make_gb(['count', 'const1'])
        self.assertEqual([1], _confirm(gb, [0, 1]))

    # ---- guards ----
    def test_no_candidates_short_circuits(self):
        gb = _make_gb(['const1'])
        self.assertEqual([], _confirm(gb, []))
        self.assertEqual(0, len(gb._row_probe.duplicated))  # no probe at all

    def test_unreadable_result_defaults_to_const1(self):
        # If the witness instance gives no readable row, keep the heuristic's
        # verdict (const-1) rather than risk corrupting a real constant.
        gb = _make_gb(['count'], empty=True)
        self.assertEqual([0], _confirm(gb, [0]))

    def test_targeted_duplicate_uses_single_ctid_of_first_relation(self):
        gb = _make_gb(['count'])
        _confirm(gb, [0])
        fqn, ctids = gb._row_probe.duplicated[0]
        self.assertEqual("unmasque.t", fqn)
        self.assertEqual(['(0,1)'], ctids)  # list_ctids()[:1]


class MaxIntInColTest(unittest.TestCase):

    def test_single_one(self):
        self.assertEqual(1, GroupBy._max_int_in_col([('1',)], 0))

    def test_takes_max_over_groups(self):
        self.assertEqual(2, GroupBy._max_int_in_col([('2',), ('1',)], 0))

    def test_non_integer_is_none(self):
        self.assertIsNone(GroupBy._max_int_in_col([('abc',)], 0))

    def test_out_of_range_index_is_none(self):
        self.assertIsNone(GroupBy._max_int_in_col([('1',)], 5))

    def test_strips_whitespace(self):
        self.assertEqual(3, GroupBy._max_int_in_col([(' 3 ',)], 0))


if __name__ == '__main__':
    unittest.main()

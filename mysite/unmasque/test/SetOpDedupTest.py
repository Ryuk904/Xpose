"""WI-14 — UNION vs UNION ALL (set-operator dedup) discrimination.

UNION (set) and UNION ALL (bag) differ only in whether duplicate output rows
are collapsed, which is invisible on the single-witness D1 the View Minimizer
leaves behind (one row per branch -> nothing to dedup). WI-14 makes the bit
observable with a controlled within-branch duplicate (enabler S2): on an
isolated branch reset to D1, count Qh's rows, duplicate one contributing
witness row, re-run Qh, and read the change:

    c1 > c0  -> the duplicate survived  -> UNION ALL (bag). Growth is
                impossible under set semantics, so this is unconditional proof.
    c1 == c0 -> the duplicate collapsed -> UNION (set). Trusted only on a
                plain-projection branch (no GROUP BY / aggregate), where the
                duplicate is guaranteed to have produced a pre-dedup duplicate.
    otherwise -> undecided -> caller keeps the safe UNION ALL default.

These cases are exercised on the REAL methods:

1. ``SetOpProbe.probe_branch`` -- the per-branch decision, driven by a fake
   ``app`` that scripts Qh's row count before/after the duplicate and a fake
   ``RowProbe`` that records the duplicate/revert. Bypasses the DB-touching
   ``__init__`` via ``__new__``.
2. ``UnionPipeLine._resolve_set_op`` -- combining per-branch verdicts into the
   global token (default UNION ALL; UNION only on positively-observed dedup;
   conflicting verdicts fall back to the safe default).
3. ``UnionPipeLine.__post_process`` -- the conditional join token and the
   single-branch unwrap, on the real string-assembly method.
"""

import types
import unittest

from types import SimpleNamespace

from mysite.unmasque.src.core.set_op_probe import (
    SetOpProbe, SET_OP_UNION, SET_OP_UNION_ALL)
from mysite.unmasque.src.pipeline.UnionPipeLine import UnionPipeLine


class _DummyLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeApp:
    """Scripts Qh's null-free row count over successive doJob calls.

    ``counts[i]`` data rows are returned on the i-th call (after a header row,
    exactly as the real Executable prepends one). ``done_seq`` optionally
    scripts ``app.done`` per call to model a mid-probe execution failure.
    """

    def __init__(self, counts, done=True, done_seq=None):
        self.counts = list(counts)
        self.done = done
        self.done_seq = done_seq
        self.calls = 0

    def doJob(self, query):
        if self.done_seq is not None:
            self.done = self.done_seq[min(self.calls, len(self.done_seq) - 1)]
        n = self.counts[self.calls] if self.calls < len(self.counts) else 0
        self.calls += 1
        return [("col",)] + [("x",) for _ in range(n)]

    def get_all_nullfree_rows(self, res):
        return res[1:]


class _FakeRowProbe:
    def __init__(self, ctids, new_ctids):
        self.ctids = list(ctids)
        self.new_ctids = list(new_ctids)
        self.dup_calls = []
        self.deleted = []

    def list_ctids(self, fqn):
        return list(self.ctids)

    def duplicate_rows(self, fqn, ctids=None):
        self.dup_calls.append((fqn, ctids))
        return list(self.new_ctids)

    def delete_rows(self, fqn, ctids):
        self.deleted.extend(ctids)


def _make_probe(app, row_probe, core_relations=("nation",)):
    """A real SetOpProbe with the DB-touching __init__ bypassed and the
    DB-side helpers stubbed, so probe_branch's REAL logic runs against the
    fake oracle."""
    p = SetOpProbe.__new__(SetOpProbe)
    p.core_relations = list(core_relations)
    p.logger = _DummyLogger()
    p.app = app
    p._row_probe = row_probe
    p.set_data_schema = lambda *a, **k: None
    p.do_init = lambda: None
    p.get_fully_qualified_table_name = lambda t: f"unmasque.{t}"
    return p


class SetOpProbeTest(unittest.TestCase):

    def test_growth_means_union_all(self):
        # Duplicate survived (1 -> 2): bag semantics.
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 2]), rp)
        self.assertEqual(p.probe_branch("Qh"), SET_OP_UNION_ALL)
        # The probe must revert its duplicate.
        self.assertEqual(rp.deleted, ["(0,2)"])
        # And it must duplicate exactly the first witness ctid (targeted).
        self.assertEqual(rp.dup_calls, [("unmasque.nation", ["(0,1)"])])

    def test_no_growth_means_union(self):
        # Duplicate collapsed (1 -> 1): set semantics.
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 1]), rp)
        self.assertEqual(p.probe_branch("Qh"), SET_OP_UNION)
        self.assertEqual(rp.deleted, ["(0,2)"])

    def test_multiplicative_growth_still_union_all(self):
        # A self-join branch can grow by more than one under bag semantics;
        # any growth is UNION ALL.
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 4]), rp)
        self.assertEqual(p.probe_branch("Qh"), SET_OP_UNION_ALL)

    def test_empty_witness_is_undecided(self):
        # c0 == 0 -> cannot probe; no duplication attempted.
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([0, 9]), rp)
        self.assertIsNone(p.probe_branch("Qh"))
        self.assertEqual(rp.dup_calls, [])
        self.assertEqual(rp.deleted, [])

    def test_baseline_execution_failure_is_undecided(self):
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 2], done=False), rp)
        self.assertIsNone(p.probe_branch("Qh"))
        self.assertEqual(rp.dup_calls, [])

    def test_duplicate_failure_is_undecided(self):
        # RowProbe could not duplicate -> undecided, no revert needed.
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=[])
        p = _make_probe(_FakeApp([1, 2]), rp)
        self.assertIsNone(p.probe_branch("Qh"))
        self.assertEqual(rp.deleted, [])

    def test_post_duplicate_execution_failure_reverts_and_undecided(self):
        # Baseline ok, post-duplicate Qh fails -> undecided, but the duplicate
        # is still reverted (finally).
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 0], done_seq=[True, False]), rp)
        self.assertIsNone(p.probe_branch("Qh"))
        self.assertEqual(rp.deleted, ["(0,2)"])

    def test_no_core_relations_is_undecided(self):
        rp = _FakeRowProbe(ctids=[], new_ctids=[])
        p = _make_probe(_FakeApp([1, 2]), rp, core_relations=())
        self.assertIsNone(p.probe_branch("Qh"))


class ResolveSetOpTest(unittest.TestCase):

    def test_default_is_union_all_when_no_verdict(self):
        self.assertEqual(UnionPipeLine._resolve_set_op([]), SET_OP_UNION_ALL)

    def test_all_verdict(self):
        self.assertEqual(UnionPipeLine._resolve_set_op([SET_OP_UNION_ALL]), SET_OP_UNION_ALL)

    def test_union_verdict(self):
        self.assertEqual(UnionPipeLine._resolve_set_op([SET_OP_UNION]), SET_OP_UNION)

    def test_multiple_union_verdicts(self):
        self.assertEqual(
            UnionPipeLine._resolve_set_op([SET_OP_UNION, SET_OP_UNION]), SET_OP_UNION)

    def test_conflict_falls_back_to_union_all(self):
        # Mixed operator is unrepresentable; the safe default wins.
        self.assertEqual(
            UnionPipeLine._resolve_set_op([SET_OP_UNION_ALL, SET_OP_UNION]), SET_OP_UNION_ALL)
        self.assertEqual(
            UnionPipeLine._resolve_set_op([SET_OP_UNION, SET_OP_UNION_ALL]), SET_OP_UNION_ALL)


class BranchProbeableTest(unittest.TestCase):
    """__branch_is_probeable disqualifies ONLY genuine aggregates; a bare
    GROUP BY with no aggregate (the UNION-induced artifact) stays probeable."""

    def _probeable(self, aggregated_attributes):
        pipe = UnionPipeLine.__new__(UnionPipeLine)
        pipe.pgao_ctx = SimpleNamespace(aggregated_attributes=aggregated_attributes)
        return pipe._UnionPipeLine__branch_is_probeable()

    def test_plain_projection_is_probeable(self):
        self.assertTrue(self._probeable([("n_name", ""), ("r_comment", "")]))

    def test_group_by_no_aggregate_is_probeable(self):
        # UNION-induced spurious GROUP BY on all cols: ops still empty.
        self.assertTrue(self._probeable([("n_name", ""), ("c_comment", "")]))

    def test_genuine_aggregate_is_not_probeable(self):
        self.assertFalse(self._probeable([("n_name", ""), ("l_quantity", "sum")]))

    def test_count_star_is_not_probeable(self):
        self.assertFalse(self._probeable([("", "Count(*)")]))

    def test_none_context_is_probeable(self):
        self.assertTrue(self._probeable(None))


class PostProcessTokenTest(unittest.TestCase):

    def _pp(self, u_eq, set_op):
        pipe = UnionPipeLine.__new__(UnionPipeLine)
        pipe.pipeLineError = False
        # name-mangled private method
        return pipe._UnionPipeLine__post_process("pstr", u_eq, set_op)

    def test_union_all_token(self):
        out = self._pp(["(Select a )", "(Select b )"], SET_OP_UNION_ALL)
        self.assertEqual(out, "(Select a )\n UNION ALL (Select b );")
        self.assertIn("UNION ALL", out)

    def test_union_token(self):
        out = self._pp(["(Select a )", "(Select b )"], SET_OP_UNION)
        self.assertEqual(out, "(Select a )\n UNION (Select b );")
        # A bare UNION must not accidentally read as UNION ALL.
        self.assertNotIn("UNION ALL", out)

    def test_three_branches(self):
        out = self._pp(["(Select a )", "(Select b )", "(Select c )"], SET_OP_UNION)
        self.assertEqual(out, "(Select a )\n UNION (Select b )\n UNION (Select c );")

    def test_single_branch_is_unwrapped(self):
        # One arm: no real set operation, the parenthesised wrapper is stripped
        # regardless of the (irrelevant) token.
        out = self._pp(["(Select a )"], SET_OP_UNION)
        self.assertEqual(out, "Select a ;")
        out2 = self._pp(["(Select a )"], SET_OP_UNION_ALL)
        self.assertEqual(out2, "Select a ;")

    def test_pipeline_error_returns_none(self):
        pipe = UnionPipeLine.__new__(UnionPipeLine)
        pipe.pipeLineError = True
        pipe.error = ""
        pipe.update_state = lambda s: None
        out = pipe._UnionPipeLine__post_process("half", ["(Select a )", "(Select b )"], SET_OP_UNION)
        self.assertIsNone(out)


if __name__ == '__main__':
    unittest.main()

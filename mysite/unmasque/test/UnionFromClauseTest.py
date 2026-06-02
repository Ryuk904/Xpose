"""Union FROM-clause junk-relation bug (found while verifying WI-14).

For a bare single-table single-column UNION branch (e.g.
``select n_regionkey from nation union all select r_regionkey from region``)
the two arms share NO common relation. The common-table detector
(``UnionFromClause.get_comTabs`` -> ``FromClause.doJob(QH, TYPE_RENAME)``) then
finds zero core relations, so ``FromClause.doActualJob`` raises
``UnmasqueError(ERROR_006)``, which ``Base.doJob`` swallows and turns into the
status string ``OK`` (``"OK "``).

The OLD code stored that string verbatim as ``self.comtabs``. Two consumers
then char-split it:

  * ``UnionFromClause.doActualJob``: ``set(self.get_comTabs(...))`` -> ``set("OK ")``
    -> ``{'O', 'K', ' '}``.
  * ``algorithm1.algo``: ``for ct in comtabs`` iterates the *characters* and
    ``cc.add(ct)`` injects ``'O'``, ``'K'``, ``' '`` into EVERY branch
    partition -> downstream ``_after_from_clause_extract`` errors on relations
    ``'O' / 'K' / ' '`` (ERROR_006) and the whole extraction fails.

The fix (``UnionFromClause._as_relation_list``) normalises any non-list
``FromClause.doJob`` result to ``[]`` -- an empty common-table set is the
*expected* outcome for disjoint single-table branches, not an error.

These cases run the REAL methods (DB-touching ``__init__`` bypassed via
``__new__``, ``FromClause`` replaced by a scriptable fake):

1. ``get_comTabs`` / ``get_fromTabs`` -- a status-string result collapses to
   ``[]`` (no char-split); a genuine list passes through unchanged.
2. ``doActualJob`` -- the branch partition is the real relations only, with
   none of the ``{'O','K',' '}`` junk tokens.
3. ``algorithm1.algo`` -- end to end over the real partition loop: the
   disjoint single-table union partitions into ``{{nation}, {region}}`` with
   no junk relation in any branch (the old code injected junk into every one).
"""

import unittest

from mysite.unmasque.src.core import algorithm1
from mysite.unmasque.src.core.from_clause import FromClause
from mysite.unmasque.src.core.union_from_clause import UnionFromClause
from mysite.unmasque.src.util.constants import OK

JUNK = set(OK)  # {'O', 'K', ' '} -- what set("OK ") would have produced


class _DummyLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeFromClause:
    """Scripts FromClause.doJob per method.

    ``error_ret`` is returned for the TYPE_ERROR (from-tabs) pass and
    ``rename_ret`` for the TYPE_RENAME (common-tabs) pass, so a test can drive
    either pass into the degenerate status-string case independently.
    """

    def __init__(self, error_ret, rename_ret):
        self.error_ret = error_ret
        self.rename_ret = rename_ret
        self.check_relations = None

    def set_check_relations(self, tabs):
        self.check_relations = tabs

    def reset_check_relations(self):
        self.check_relations = None

    def doJob(self, QH, method):
        return self.error_ret if method == FromClause.TYPE_ERROR else self.rename_ret


def _make_ufc(error_ret, rename_ret):
    """A real UnionFromClause with the DB-touching __init__ bypassed; the only
    collaborator is the scriptable fake FromClause. Singleton-safe: every
    mutable field the methods under test read is reset here."""
    db = UnionFromClause.__new__(UnionFromClause)
    db.comtabs = None
    db.fromtabs = None
    db.to_nullify = None
    db.logger = _DummyLogger()
    db.fromClause = _FakeFromClause(error_ret, rename_ret)
    return db


class GetComTabsTest(unittest.TestCase):

    def test_status_string_collapses_to_empty_list(self):
        db = _make_ufc(error_ret=["nation", "region"], rename_ret=OK)
        com = db.get_comTabs("Qh", {"nation", "region"})
        self.assertEqual(com, [])
        # The defining symptom: set() over the result must NOT char-split.
        self.assertEqual(set(db.get_comTabs("Qh", {"nation", "region"})), set())

    def test_false_setup_failure_collapses_to_empty_list(self):
        # Base.doJob returns False when setup fails -> also not a relation list.
        db = _make_ufc(error_ret=["nation"], rename_ret=False)
        self.assertEqual(db.get_comTabs("Qh", {"nation"}), [])

    def test_exception_string_collapses_to_empty_list(self):
        # Base.doJob returns str(e) on a non-Unmasque exception.
        db = _make_ufc(error_ret=["nation"], rename_ret="some traceback text")
        self.assertEqual(db.get_comTabs("Qh", {"nation"}), [])

    def test_genuine_common_relations_pass_through(self):
        # A real multi-table union with a shared relation: list is preserved.
        db = _make_ufc(error_ret=["nation", "region", "supplier"],
                       rename_ret=["nation"])
        self.assertEqual(db.get_comTabs("Qh", {"nation", "region", "supplier"}),
                         ["nation"])

    def test_get_fromTabs_status_string_collapses(self):
        db = _make_ufc(error_ret=OK, rename_ret=OK)
        self.assertEqual(db.get_fromTabs("Qh"), [])

    def test_get_fromTabs_list_passes_through(self):
        db = _make_ufc(error_ret=["nation", "region"], rename_ret=OK)
        self.assertEqual(db.get_fromTabs("Qh"), ["nation", "region"])


class DoActualJobPartitionTest(unittest.TestCase):
    """The real doActualJob must yield a clean partial-table set even when the
    common-table pass returns the OK status string."""

    def test_disjoint_single_table_branches_no_junk(self):
        db = _make_ufc(error_ret=["nation", "region"], rename_ret=OK)
        part = db.doActualJob(("Qh",))
        self.assertEqual(part, {"nation", "region"})
        # No char-split tokens leaked into the partition.
        self.assertEqual(part & JUNK, set())
        # And the cached common-table set the algorithm reads is clean.
        self.assertEqual(db.comtabs, [])

    def test_common_relation_subtracted_from_partition(self):
        # nation shared across arms -> part tables = {region, supplier}.
        db = _make_ufc(error_ret=["nation", "region", "supplier"],
                       rename_ret=["nation"])
        part = db.doActualJob(("Qh",))
        self.assertEqual(part, {"region", "supplier"})


class Algorithm1PartitionTest(unittest.TestCase):
    """End-to-end over the REAL algorithm1.algo partition loop (the actual
    damage site: ``for ct in comtabs``). Only the leaf DB ops and the Pop
    oracle are stubbed; doActualJob/get_comTabs/the partition loop are real."""

    def _make_union_db(self, branch_tables):
        db = _make_ufc(error_ret=list(branch_tables), rename_ret=OK)
        # Plumbing the REAL Base.doJob -> doAppCountJob -> doActualJob needs:
        db.enabled = True
        db.app_calls = 0

        class _App:
            method_call_count = 0

        db.app = _App()

        # Pop oracle: nullify_except keeps a relation subset; the result is
        # non-empty iff some branch relation survives. (Disjoint single-table
        # branches: any kept branch table -> non-empty.)
        kept = {}

        def nullify_except(s):
            kept["s"] = set(s)

        db.nullify_except = nullify_except
        db.run_query = lambda QH: kept.get("s", set())
        db.revert_nullify = lambda: None
        db.isEmpty = lambda res: len(set(res) & set(branch_tables)) == 0
        return db

    def test_disjoint_two_table_union_partitions_cleanly(self):
        db = self._make_union_db(["nation", "region"])
        p, pstr, _profile = algorithm1.algo(db, "Qh")
        # Two single-table branches, nothing else.
        self.assertEqual(p, {frozenset({"nation"}), frozenset({"region"})})
        # The decisive assertion: no junk char ever entered any branch.
        all_rels = {r for branch in p for r in branch}
        self.assertEqual(all_rels & JUNK, set())
        self.assertTrue(all(isinstance(r, str) and len(r) > 1 for r in all_rels))

    def test_comtabs_is_iterable_empty_not_string(self):
        # Guards the literal bug: db.comtabs must be a real (empty) list so the
        # ``for ct in comtabs`` loop iterates zero relations, not 3 characters.
        db = self._make_union_db(["nation", "region"])
        algorithm1.algo(db, "Qh")
        self.assertEqual(db.comtabs, [])
        self.assertNotIsInstance(db.comtabs, str)


if __name__ == '__main__':
    unittest.main()

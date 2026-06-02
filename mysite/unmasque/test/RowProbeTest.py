"""Tests for enabler S2 — the shared RowProbe duplicate/delete/count primitive.

Two layers:

* ``RowProbeUnitTest`` — deterministic, no DB. Fakes the connectionHelper and
  app to lock the SQL the primitive builds (duplicate-all, duplicate-by-ctid,
  delete-per-ctid, ctid read-back) and the header-stripping count logic.
* ``RowProbeIntegrationTest`` — the S2 acceptance criterion against the live
  PostgreSQL: ``duplicate -> count -> revert`` leaves the ctid set (and the row
  count) exactly as it was. Skips cleanly if no DB is reachable.
"""

import unittest

from mysite.unmasque.src.core.row_probe import RowProbe


class _DummyLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


# --------------------------------------------------------------------------- #
#  Unit layer: fake collaborators                                             #
# --------------------------------------------------------------------------- #
class _FakeQueries:
    def select_ctid_star_from(self, tab):
        return f"SELECT ctid::text, * FROM {tab} ORDER BY ctid;"


class _RecordingCH:
    """Records every SQL and returns canned fetch results by statement kind."""

    def __init__(self, insert_ctids=None, listed_ctids=None):
        self.executed = []        # statements run via execute_sql
        self.fetched = []         # statements run via execute_sql_fetchall
        self.queries = _FakeQueries()
        self._insert_ctids = insert_ctids if insert_ctids is not None else ["(0,9)"]
        self._listed = listed_ctids if listed_ctids is not None else ["(0,1)", "(0,2)"]

    def execute_sql_fetchall(self, sql, logger=None):
        self.fetched.append(sql)
        if sql.lstrip().upper().startswith("INSERT"):
            return [(c,) for c in self._insert_ctids], None
        if "SELECT CTID" in sql.upper():
            return [(c, "data") for c in self._listed], None
        return [], None

    def execute_sql(self, sqls, logger=None):
        self.executed.extend(sqls)


class _FakeApp:
    def __init__(self, result, raise_exc=False):
        self.result = result
        self.raise_exc = raise_exc

    def doJob(self, query):
        if self.raise_exc:
            raise RuntimeError("boom")
        return self.result


class RowProbeUnitTest(unittest.TestCase):

    # ---- duplicate ----
    def test_duplicate_all_builds_full_table_insert(self):
        ch = _RecordingCH(insert_ctids=["(0,9)"])
        rp = RowProbe(ch, None, _DummyLogger())
        out = rp.duplicate_rows("unmasque.t")
        self.assertEqual(["(0,9)"], out)
        self.assertEqual(
            ["INSERT INTO unmasque.t SELECT * FROM unmasque.t RETURNING ctid::text;"],
            ch.fetched,
        )

    def test_duplicate_by_ctid_filters_the_select(self):
        ch = _RecordingCH(insert_ctids=["(0,9)"])
        rp = RowProbe(ch, None, _DummyLogger())
        rp.duplicate_rows("unmasque.t", ["(0,1)", "(0,2)"])
        self.assertEqual(
            "INSERT INTO unmasque.t SELECT * FROM unmasque.t "
            "WHERE ctid IN ('(0,1)', '(0,2)') RETURNING ctid::text;",
            ch.fetched[0],
        )

    def test_duplicate_returns_empty_on_no_rows(self):
        ch = _RecordingCH(insert_ctids=[])
        rp = RowProbe(ch, None, _DummyLogger())
        self.assertEqual([], rp.duplicate_rows("unmasque.t"))

    # ---- delete ----
    def test_delete_issues_one_statement_per_ctid(self):
        ch = _RecordingCH()
        rp = RowProbe(ch, None, _DummyLogger())
        rp.delete_rows("unmasque.t", ["(0,9)", "(0,10)"])
        self.assertEqual(
            ["DELETE FROM unmasque.t WHERE ctid = '(0,9)';",
             "DELETE FROM unmasque.t WHERE ctid = '(0,10)';"],
            ch.executed,
        )

    # ---- list_ctids ----
    def test_list_ctids_parses_first_column(self):
        ch = _RecordingCH(listed_ctids=["(0,1)", "(0,2)", "(0,3)"])
        rp = RowProbe(ch, None, _DummyLogger())
        self.assertEqual(["(0,1)", "(0,2)", "(0,3)"], rp.list_ctids("unmasque.t"))

    # ---- count ----
    def test_count_strips_header(self):
        app = _FakeApp([("c0", "c1"), ("a", "b"), ("c", "d")])
        rp = RowProbe(None, app, _DummyLogger())
        self.assertEqual(2, rp.count_rows("Q"))

    def test_count_header_only_is_zero(self):
        app = _FakeApp([("c0", "c1")])
        rp = RowProbe(None, app, _DummyLogger())
        self.assertEqual(0, rp.count_rows("Q"))

    def test_count_none_is_zero(self):
        rp = RowProbe(None, _FakeApp(None), _DummyLogger())
        self.assertEqual(0, rp.count_rows("Q"))

    def test_count_exception_is_minus_one(self):
        rp = RowProbe(None, _FakeApp(None, raise_exc=True), _DummyLogger())
        self.assertEqual(-1, rp.count_rows("Q"))


# --------------------------------------------------------------------------- #
#  Integration layer: live DB, the S2 acceptance criterion                    #
# --------------------------------------------------------------------------- #
class RowProbeIntegrationTest(unittest.TestCase):
    TABLE = "public.s2_rowprobe_tmp"

    @classmethod
    def setUpClass(cls):
        try:
            from mysite.unmasque.src.util.ConnectionFactory import ConnectionHelperFactory
            from mysite.unmasque.src.core.factory.ExecutableFactory import ExecutableFactory
            cls.ch = ConnectionHelperFactory().createConnectionHelper()
            cls.ch.connectUsingParams()
            cls.app = ExecutableFactory().create_exe(cls.ch)
        except Exception as e:  # no DB / bad creds -> skip, don't fail the suite
            raise unittest.SkipTest(f"No live DB available: {e}")
        cls.rp = RowProbe(cls.ch, cls.app, _DummyLogger())
        # A throwaway table; never committed, dropped in teardown, and the
        # connection rolls back on close, so `public` is untouched regardless.
        cls.ch.execute_sql([
            f"DROP TABLE IF EXISTS {cls.TABLE};",
            f"CREATE TABLE {cls.TABLE} (a int, b text);",
            f"INSERT INTO {cls.TABLE} VALUES (1,'x'),(2,'y'),(3,'z');",
        ])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.ch.execute_sql([f"DROP TABLE IF EXISTS {cls.TABLE};"])
            cls.ch.closeConnection()
        except Exception:
            pass

    def test_duplicate_count_revert_restores_ctid_set(self):
        before = set(self.rp.list_ctids(self.TABLE))
        self.assertEqual(3, len(before))
        self.assertEqual(3, self.rp.count_rows(f"SELECT * FROM {self.TABLE}"))

        # Duplicate exactly one targeted row.
        target = sorted(before)[:1]
        new_ctids = self.rp.duplicate_rows(self.TABLE, target)
        self.assertEqual(1, len(new_ctids))

        mid = set(self.rp.list_ctids(self.TABLE))
        self.assertTrue(before.issubset(mid))
        self.assertEqual(4, len(mid))
        self.assertEqual(4, self.rp.count_rows(f"SELECT * FROM {self.TABLE}"))

        # Revert.
        self.rp.delete_rows(self.TABLE, new_ctids)
        after = set(self.rp.list_ctids(self.TABLE))
        self.assertEqual(before, after)  # ctid set restored -> D unchanged
        self.assertEqual(3, self.rp.count_rows(f"SELECT * FROM {self.TABLE}"))


if __name__ == '__main__':
    unittest.main()

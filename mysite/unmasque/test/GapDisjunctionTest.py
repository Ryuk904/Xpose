"""End-to-end tests for relocated within-attribute gap (OR-of-intervals) extraction.

Gap extraction now runs as a post-Projection NEP-style diff pass
(src/pipeline/fragments/GapPipeLine.py). Because it diffs the *reconstructed*
Q_E against Qh, it recovers within-attribute disjunctions for bare-column,
scalar-expression and aggregate (count/sum) projections alike -- the classes
the old in-Filter implementation could not handle.

These are integration tests: they need the live TPC-H Postgres from config.ini
(``nation`` suffices; all cases run on the 25-row nation table). Each test
isolates the ExtractionPipeLine path (outer-join/union/nep/or off, gap_aware
on) and asserts the pipeline's own Re==Rh equivalence flag plus the OR-of-
BETWEEN structure in the reconstructed SQL.

Run:  python -m unittest mysite.unmasque.test.GapDisjunctionTest
"""

import unittest

import psycopg2

from ..src.util.ConnectionFactory import ConnectionHelperFactory
from ..src.core.factory.PipeLineFactory import PipeLineFactory

_CORE = {'customer', 'lineitem', 'nation', 'orders', 'part', 'partsupp', 'region', 'supplier'}


def _clean_working_schema(cfg):
    """Drop comparator/minimizer artefacts left in the working schema by a
    prior run so sequential tests don't contaminate one another."""
    c = psycopg2.connect(dbname=cfg.dbname, user=cfg.user, password=cfg.password,
                         host=cfg.host, port=cfg.port)
    c.autocommit = True
    cur = c.cursor()
    cur.execute(f"select tablename from pg_tables where schemaname='{cfg.schema}'")
    for t in [r[0] for r in cur.fetchall()]:
        if t not in _CORE:
            cur.execute(f'drop table if exists {cfg.schema}."{t}" cascade')
    cur.execute(f"select viewname from pg_views where schemaname='{cfg.schema}'")
    for v in [r[0] for r in cur.fetchall()]:
        cur.execute(f'drop view if exists {cfg.schema}."{v}" cascade')
    c.close()


class GapDisjunctionTest(unittest.TestCase):

    def _run(self, query):
        conn = ConnectionHelperFactory().createConnectionHelper()
        cfg = conn.config
        cfg.detect_union = cfg.detect_oj = cfg.detect_nep = cfg.detect_or = cfg.use_cs2 = False
        cfg.detect_gap_aware = True
        try:
            _clean_working_schema(cfg)
        except Exception:
            pass
        pipe = PipeLineFactory().create_pipeline(conn)
        eq = pipe.doJob(query)
        try:
            conn.closeConnection()
        except Exception:
            pass
        return eq, pipe.correct

    # --- within-attribute OR is recovered, and Re == Rh -----------------------

    def test_bare_column_both_extremes(self):
        # A < 5 OR A > 20: both domain ends accepted -> Filter drops it; the gap
        # pass must resurrect the disjunction.
        eq, correct = self._run(
            "select n_name from nation where n_nationkey < 5 or n_nationkey > 20;")
        self.assertTrue(correct, f"Qe not equivalent to Qh: {eq}")
        self.assertIn(" OR ", (eq or "").upper(), f"expected OR-of-intervals, got: {eq}")

    def test_hole_in_interval(self):
        # A in [2,8] with a hole at 5: the binary-search envelope over-
        # approximates to [2,8]; the gap pass carves [2,4] OR [6,8].
        eq, correct = self._run(
            "select n_name from nation where n_nationkey >= 2 and n_nationkey <= 8 "
            "and n_nationkey <> 5;")
        self.assertTrue(correct, f"Qe not equivalent to Qh: {eq}")
        self.assertIn(" OR ", (eq or "").upper(), f"expected OR-of-intervals, got: {eq}")

    def test_aggregate_count_projection(self):
        # NEW capability: count(*) projection. Pop is uninformative (count
        # returns a row even when the witness is rejected); the gap pass uses a
        # result-fingerprint delta instead.
        eq, correct = self._run(
            "select count(*) from nation where n_nationkey < 5 or n_nationkey > 20;")
        self.assertTrue(correct, f"Qe not equivalent to Qh: {eq}")
        self.assertIn(" OR ", (eq or "").upper(), f"expected OR-of-intervals, got: {eq}")

    def test_expression_projection(self):
        # NEW capability: scalar-expression projection. The old in-Filter path
        # bailed (couldn't splice a non-bare header); the relocated diff over the
        # reconstructed Q_E handles it.
        eq, correct = self._run(
            "select n_nationkey * 2 from nation where n_nationkey < 5 or n_nationkey > 20;")
        self.assertTrue(correct, f"Qe not equivalent to Qh: {eq}")
        self.assertIn(" OR ", (eq or "").upper(), f"expected OR-of-intervals, got: {eq}")

    # --- regression: a single contiguous range must NOT become a disjunction --

    def test_single_range_no_false_disjunction(self):
        eq, correct = self._run(
            "select n_name from nation where n_nationkey >= 5 and n_nationkey <= 15;")
        self.assertTrue(correct, f"Qe not equivalent to Qh: {eq}")
        self.assertNotIn(" OR ", (eq or "").upper(),
                         f"single range wrongly split into a disjunction: {eq}")


if __name__ == "__main__":
    unittest.main()

"""
Tests for the disjunction refiner (mysite/unmasque/src/core/disjunction_refiner.py).

These are end-to-end pipeline tests and therefore need a live PostgreSQL with the TPC-H
schema loaded (same setup as the other tests in this directory). Each test runs a hidden
query whose WHERE clause has a *gap* (a disjunction of ranges, or a `<>`), and asserts
that the extracted query is correct - i.e. that ``pipeline.correct`` is True. Without the
refiner the binary-search extractor returns an over-approximated single range and these
would fail; with the refiner enabled (via the existing ``detect_or`` flag) they pass.

The last test is a regression check: a plain conjunctive query (no disjunction) must
still extract correctly with ``detect_or`` on - i.e. the refiner is a no-op there.

Small tables (nation / region) are used on purpose so the tests run quickly; the
big-table case (l_quantity on lineitem) is exercised via the DISJ2 query in main_cmd.py.

Run from the ``mysite`` directory:  python -m unmasque.test.DisjunctionRefinementTest
"""
import unittest

from ..src.core.factory.PipeLineFactory import PipeLineFactory
from .util.BaseTestCase import BaseTestCase


class DisjunctionRefinementTestCase(BaseTestCase):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conn.config.detect_union = False
        self.conn.config.detect_nep = False
        self.conn.config.detect_oj = False
        self.conn.config.detect_or = True          # this flag also enables the refiner
        self.conn.config.use_cs2 = False
        self.pipeline = None

    def setUp(self):
        super().setUp()
        self.pipeline = None

    def _do_test(self, query):
        factory = PipeLineFactory()
        self.pipeline = factory.create_pipeline(self.conn)
        eq = self.pipeline.doJob(query)
        print("Hidden   :", query)
        print("Extracted:", eq)
        self.pipeline.time_profile.print()
        self.assertTrue(self.pipeline.correct, f"extracted query is not equivalent:\n{eq}")
        del factory

    # ---- single attribute, one gap (the motivating example, on a small table) ----
    def test_two_ranges_one_attribute(self):
        query = ("select n_nationkey, n_name from nation "
                 "where n_nationkey between 1 and 5 or n_nationkey between 10 and 15;")
        self._do_test(query)

    # ---- single attribute, two gaps (must be cleaned up to an exact disjunction) ----
    def test_three_ranges_one_attribute(self):
        query = ("select n_name from nation "
                 "where n_nationkey between 0 and 3 or n_nationkey between 8 and 11 "
                 "or n_nationkey between 18 and 21;")
        self._do_test(query)

    # ---- a <> predicate, which is just a width-one gap -------------------------
    def test_not_equal_inside_range(self):
        query = ("select n_name from nation "
                 "where n_nationkey between 5 and 18 and n_nationkey <> 10;")
        self._do_test(query)

    # ---- gaps on two different attributes --------------------------------------
    def test_gaps_on_two_attributes(self):
        query = ("select n_name from nation "
                 "where (n_nationkey between 1 and 5 or n_nationkey between 10 and 15) "
                 "and (n_regionkey between 0 and 1 or n_regionkey between 3 and 4);")
        self._do_test(query)

    # ---- regression: no disjunction -> refiner is a harmless no-op -------------
    def test_plain_conjunctive_no_disjunction(self):
        query = ("select n_name from nation "
                 "where n_nationkey between 5 and 18 and n_regionkey >= 1;")
        self._do_test(query)


if __name__ == '__main__':
    suite = unittest.TestLoader().loadTestsFromTestCase(DisjunctionRefinementTestCase)
    unittest.TextTestRunner(verbosity=2).run(suite)

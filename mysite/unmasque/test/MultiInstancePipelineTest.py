"""Integration tests for the multi-instance (self-join) extension.

These drive the *real* extraction pipeline (db_restorer -> Cs2 -> MultiplicityDetect
-> floored ViewMinimizer -> conditional re-collapse -> Algorithms 2-4 -> §F probe
-> legacy SPJGAOL -> alias-aware assembler) against a live PostgreSQL + TPC-H
instance, with ``[feature] multi_instance`` forced on.  They are the end-to-end
counterpart of the pure-logic unit tests in Multiplicity/AliasAwareMinimizer/
CrossAliasPredicate/PerAliasFilter/PerAliasPinnedFilter/AliasAwareAssemblerTest.py.

The candidate queries live in ``EQC_SJ_workload.sql`` (each annotated with what the
pipeline should recover).  These assert the *structural* recoveries that work today
-- mult(R); which column is "coupled" (the equi-join key); per-alias filter bounds;
the projection alias-lift; that an alias-aware candidate query is produced -- but NOT
that the candidate `verified` against the DB: full observational exactness for
self-joins still needs the legacy SPJGAOL extractors to be alias-aware (the strict
cross-alias inequalities, in particular, can't be carried by the current re-collapse
workaround -- see docs/multi_instance_extractors_plan.md).  ``alias_aware_query_verified``
is logged but not asserted.

Run a single one with, e.g.::

    /path/to/unmasque-env/python -m unittest mysite.unmasque.test.MultiInstancePipelineTest -k A1

If no TPC-H database is reachable the whole module skips.  Cs2 is left disabled
(like the other direct-`_mutation_pipeline` tests) and MultiplicityDetect caps its
own probe snapshot, so even the lineitem queries stay tractable; the view minimizer
still runs on the full tables, so a lineitem-on-lineitem self-join test may take a
minute or two.
"""
import unittest

from mysite.unmasque.src.core.elapsed_time import create_zero_time_profile
from mysite.unmasque.src.pipeline.ExtractionPipeLine import ExtractionPipeLine
from mysite.unmasque.test.util import tpchSettings
from mysite.unmasque.test.util.BaseTestCase import BaseTestCase


def _db_reachable(conn):
    try:
        conn.connectUsingParams()
        conn.closeConnection()
        return True
    except Exception:
        return False


class MultiInstancePipelineTest(BaseTestCase):

    @classmethod
    def setUpClass(cls):
        if not _db_reachable(cls.conn):
            raise unittest.SkipTest("no TPC-H PostgreSQL reachable -- skipping multi-instance integration tests")
        cls._saved_multi = getattr(cls.conn.config, "detect_multi_instance", False)
        cls._saved_cs2 = getattr(cls.conn.config, "use_cs2", False)

    @classmethod
    def tearDownClass(cls):
        cls.conn.config.detect_multi_instance = getattr(cls, "_saved_multi", False)
        cls.conn.config.use_cs2 = getattr(cls, "_saved_cs2", False)

    def __init__(self, *args, **kwargs):
        super(BaseTestCase, self).__init__(*args, **kwargs)
        # Cs2 needs the join key_lists wired up (which `extract()` does but a direct
        # `_mutation_pipeline()` call does not); leave it disabled like the other
        # direct-`_mutation_pipeline` tests -- MultiplicityDetect caps its own probe
        # snapshot so it stays fast even on the full tables.
        self.conn.config.use_cs2 = False
        self.conn.config.detect_multi_instance = True
        self.pipeline = ExtractionPipeLine(self.conn)

    # ------------------------------------------------------------------ helpers
    def _run_mutation(self, core_rels, query):
        self.conn.connectUsingParams()
        self.pipeline.all_sizes = tpchSettings.all_size
        self.pipeline.key_lists = tpchSettings.key_lists
        tp = create_zero_time_profile()
        self.pipeline._mutation_pipeline(core_rels, query, tp)

    def _coupled(self, tab):
        return set(self.pipeline.cross_alias_coupled_columns.get(tab, []))

    def _inter_preds(self, tab):
        return [p for p in self.pipeline.cross_alias_predicates.get(tab, []) if p.get('kind') == 'inter']

    def _build_and_log_alias_aware(self, query):
        eq = self.pipeline.extract(query)
        aaq = self.pipeline.alias_aware_query
        print(f"\n  legacy eq      = {eq}")
        print(f"  alias-aware    = {aaq}")
        print(f"  verified       = {self.pipeline.alias_aware_query_verified}")
        print(f"  mult           = {self.pipeline.mult}")
        print(f"  coupled        = {self.pipeline.cross_alias_coupled_columns}")
        print(f"  cross-alias    = {self.pipeline.cross_alias_predicates}")
        print(f"  proj-attr      = {self.pipeline.projection_alias_attribution}")
        print(f"  per-alias filt = {self.pipeline.per_alias_filters}")
        print(f"  pinned filt    = {self.pipeline.per_alias_pinned_filters}")
        return eq, aaq

    # ============================ A. equi-join + non-strict inequality =========
    def test_A1_partsupp_supplycost_le(self):
        q = ("SELECT ps1.ps_partkey FROM partsupp ps1, partsupp ps2 "
             "WHERE ps1.ps_partkey = ps2.ps_partkey AND ps1.ps_supplycost <= ps2.ps_supplycost;")
        self._run_mutation(['partsupp'], q)
        self.assertEqual(self.pipeline.mult.get('partsupp'), 2)
        self.assertIn('ps_partkey', self._coupled('partsupp'))   # the equi-join key
        eq, aaq = self._build_and_log_alias_aware(q)
        self.assertIsNotNone(aaq)
        self.assertIn('partsupp_a2', aaq)

    # ============================ B. equi-join + STRICT inequality =============
    def test_B2_partsupp_strict_chain_projected(self):
        # STRICT inter-alias `<` on a column projected from BOTH aliases: Algorithm 3
        # recovers the `<`.  The legacy SPJGAOL extractors do NOT yet survive a strict
        # self-join (the re-collapse keeps the k-row D_min, but they mutate columns
        # uniformly across rows -- see docs/multi_instance_extractors_plan.md), so the
        # assembled query may be None; we assert the *artifacts*, which are correct.
        q = ("SELECT ps1.ps_supplycost AS lo, ps2.ps_supplycost AS hi "
             "FROM partsupp ps1, partsupp ps2 "
             "WHERE ps1.ps_partkey = ps2.ps_partkey AND ps1.ps_supplycost < ps2.ps_supplycost;")
        self._run_mutation(['partsupp'], q)
        self.assertEqual(self.pipeline.mult.get('partsupp'), 2)
        self.assertIn('ps_partkey', self._coupled('partsupp'))
        self.assertTrue(any(p['col'] == 'ps_supplycost' and p['op'] in ('<', '<=')
                            for p in self._inter_preds('partsupp')),
                        f"expected inter-alias < on ps_supplycost; got {self._inter_preds('partsupp')}")
        attr = self.pipeline.projection_alias_attribution or {}
        self.assertEqual(attr.get(0), ('partsupp', 1, 'ps_supplycost'))
        self.assertEqual(attr.get(1), ('partsupp', 2, 'ps_supplycost'))
        self._build_and_log_alias_aware(q)            # logged; not asserted (eq may be None)

    # ============================ C. per-alias filters =========================
    def test_C1_two_upper_bounds_no_chain(self):
        q = ("SELECT ps1.ps_partkey FROM partsupp ps1, partsupp ps2 "
             "WHERE ps1.ps_partkey = ps2.ps_partkey "
             "AND ps1.ps_supplycost <= 500 AND ps2.ps_supplycost <= 800;")
        self._run_mutation(['partsupp'], q)
        self.assertEqual(self.pipeline.mult.get('partsupp'), 2)
        paf = (self.pipeline.per_alias_filters.get('partsupp') or {}).get('ps_supplycost')
        self.assertIsNotNone(paf, "a per-alias filter on partsupp.ps_supplycost should be found")
        self.assertGreaterEqual(len(paf['upper']), 2)            # ~500 and ~800
        eq, aaq = self._build_and_log_alias_aware(q)
        self.assertIsNotNone(aaq)

    def test_C3_both_aliases_same_upper_bound(self):
        q = ("SELECT ps1.ps_partkey FROM partsupp ps1, partsupp ps2 "
             "WHERE ps1.ps_partkey = ps2.ps_partkey "
             "AND ps1.ps_supplycost <= 700 AND ps2.ps_supplycost <= 700;")
        self._run_mutation(['partsupp'], q)
        self.assertEqual(self.pipeline.mult.get('partsupp'), 2)
        paf = (self.pipeline.per_alias_filters.get('partsupp') or {}).get('ps_supplycost')
        self.assertIsNotNone(paf)
        # "both bounded at the same value" -> the multiset repeats it (x4 cardinality jump)
        self.assertGreaterEqual(len(paf.get('upper_multiset') or []), 2)
        eq, aaq = self._build_and_log_alias_aware(q)
        self.assertIsNotNone(aaq)

    # ============================ E. three-way self-join =======================
    @unittest.skip("heavy: a 3-way self-join on the full (un-sampled) partsupp makes the "
                   "view minimizer + the assembler's verification fetch large result sets; "
                   "needs Cs2 sampling wired up (key_lists) or a smaller DB.  Run manually.")
    def test_E1_partsupp_three_way(self):
        # k=3 on partsupp; ps_partkey is the shared equi-join key; the ordering
        # predicates are non-strict, so the 1-row re-collapse keeps Q_H FIT.
        q = ("SELECT ps1.ps_partkey FROM partsupp ps1, partsupp ps2, partsupp ps3 "
             "WHERE ps1.ps_partkey = ps2.ps_partkey AND ps2.ps_partkey = ps3.ps_partkey "
             "AND ps1.ps_supplycost <= ps2.ps_supplycost AND ps2.ps_supplycost <= ps3.ps_supplycost;")
        self._run_mutation(['partsupp'], q)
        self.assertEqual(self.pipeline.mult.get('partsupp'), 3)
        self.assertIn('ps_partkey', self._coupled('partsupp'))
        eq, aaq = self._build_and_log_alias_aware(q)
        self.assertIsNotNone(aaq)
        self.assertIn('partsupp_a3', aaq)

    # ============================ G. boundary cases (no crash) =================
    def test_G1_idempotent_self_join_does_not_crash(self):
        q = ("SELECT ps1.ps_partkey FROM partsupp ps1, partsupp ps2 "
             "WHERE ps1.ps_partkey = ps2.ps_partkey AND ps1.ps_suppkey = ps2.ps_suppkey;")
        self._run_mutation(['partsupp'], q)
        self.assertGreaterEqual(self.pipeline.mult.get('partsupp', 1), 1)
        eq, aaq = self._build_and_log_alias_aware(q)
        # mult is reported as 2 (the query genuinely self-joins; we don't do hom-folding)
        # -- whether or not it is, the pipeline must not crash and eq must be produced.
        self.assertIsNotNone(eq)


if __name__ == '__main__':
    unittest.main()

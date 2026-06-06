"""WI-36 — Uncorrelated EXISTS gate: detection (non-scaling probe), pipeline
reclassification, and QSG emission, exercised on the REAL methods.

An uncorrelated EXISTS gate (`SELECT n_name FROM nation WHERE EXISTS (SELECT 1
FROM region WHERE r_regionkey > 2)`) references a relation (`region`) that is
load-bearing (emptying it empties Qh -> from_clause keeps it as core),
*non-projecting*, *non-joining*, and *non-scaling*. Left in core_relations it is
comma-joined into FROM as a wrong cross join. WI-36 reclassifies it.

Three real components are covered:

1. ``ExistsGateProbe.is_nonscaling_gate`` — condition (4), driven by a fake
   ``app`` that scripts |Qh| before/after a row duplicate and a fake
   ``RowProbe`` that records the duplicate/revert. The DB-touching ``__init__``
   is bypassed with ``__new__``.
       c1 == c0 (unchanged) -> gate (True);  c1 > c0 (grew) -> cross join (False).

2. ``ExtractionPipeLine._gate_projected_tables`` / ``_gate_joined_tables`` /
   ``_reclassify_exists_gates`` / ``_strip_gate_relations`` — the conditions (2),
   (3) pre-filters and the fail-closed reclassification loop, with a stubbed
   probe injected via ``unittest.mock.patch``.

3. ``QueryStringGenerator.__generate_where_clause`` (+ the gate-clause and
   inner-predicate helpers) — the emission: a `<kind> (SELECT 1 FROM gate WHERE
   ...)` conjunct, with the gate's own filter predicate moved INSIDE the
   subquery and excluded from the outer WHERE.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from mysite.unmasque.src.core.exists_gate_probe import ExistsGateProbe
from mysite.unmasque.src.pipeline.ExtractionPipeLine import ExtractionPipeLine
from mysite.unmasque.src.util.QueryStringGenerator import QueryStringGenerator, QueryDetails


class _DummyLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeApp:
    """Scripts |Qh| (null-free row count) over successive doJob calls."""

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


def _make_probe(app, row_probe, core_relations=("region",)):
    """Real ExistsGateProbe with the DB-touching __init__ bypassed and the
    DB-side helpers stubbed, so is_nonscaling_gate's REAL logic runs."""
    p = ExistsGateProbe.__new__(ExistsGateProbe)
    p.core_relations = list(core_relations)
    p.logger = _DummyLogger()
    p.app = app
    p._row_probe = row_probe
    p.set_data_schema = lambda *a, **k: None
    p.do_init = lambda: None
    p.get_fully_qualified_table_name = lambda t: f"unmasque.{t}"
    return p


# --------------------------------------------------------------------------- 1
class ExistsGateProbeTest(unittest.TestCase):

    def test_non_scaling_is_gate(self):
        # Duplicate left |Qh| unchanged (1 -> 1): a 0/1 gate.
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 1]), rp)
        self.assertIs(p.is_nonscaling_gate("Qh", "region"), True)
        # Targeted dup of the first witness ctid, then revert.
        self.assertEqual(rp.dup_calls, [("unmasque.region", ["(0,1)"])])
        self.assertEqual(rp.deleted, ["(0,2)"])

    def test_scaling_is_cross_join(self):
        # Duplicate grew |Qh| (1 -> 2): cross/inner join, not a gate.
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 2]), rp)
        self.assertIs(p.is_nonscaling_gate("Qh", "region"), False)
        self.assertEqual(rp.deleted, ["(0,2)"])

    def test_multiplicative_scaling_is_cross_join(self):
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 4]), rp)
        self.assertIs(p.is_nonscaling_gate("Qh", "region"), False)

    def test_empty_witness_is_undecided(self):
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([0, 9]), rp)
        self.assertIsNone(p.is_nonscaling_gate("Qh", "region"))
        self.assertEqual(rp.dup_calls, [])  # never probed
        self.assertEqual(rp.deleted, [])

    def test_baseline_failure_is_undecided(self):
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 1], done=False), rp)
        self.assertIsNone(p.is_nonscaling_gate("Qh", "region"))
        self.assertEqual(rp.dup_calls, [])

    def test_duplicate_failure_is_undecided(self):
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=[])  # could not duplicate
        p = _make_probe(_FakeApp([1, 1]), rp)
        self.assertIsNone(p.is_nonscaling_gate("Qh", "region"))
        self.assertEqual(rp.deleted, [])

    def test_post_duplicate_failure_reverts_and_undecided(self):
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 0], done_seq=[True, False]), rp)
        self.assertIsNone(p.is_nonscaling_gate("Qh", "region"))
        self.assertEqual(rp.deleted, ["(0,2)"])  # still reverted (finally)

    def test_tab_not_core_is_undecided(self):
        rp = _FakeRowProbe(ctids=["(0,1)"], new_ctids=["(0,2)"])
        p = _make_probe(_FakeApp([1, 1]), rp, core_relations=("nation",))
        self.assertIsNone(p.is_nonscaling_gate("Qh", "region"))
        self.assertEqual(rp.dup_calls, [])


# --------------------------------------------------------------------------- 2
def _make_pipeline(detect_exists=True,
                   dependencies=None,
                   aggregated=None,
                   eq_edges=None,
                   aoa_le=None,
                   aoa_l=None,
                   instances=None,
                   alias_to_table=None):
    """Real ExtractionPipeLine with __init__ bypassed; only the attributes the
    WI-36 helpers read are populated. The REAL helper methods run on it."""
    obj = ExtractionPipeLine.__new__(ExtractionPipeLine)
    obj.logger = _DummyLogger()
    obj.connectionHelper = SimpleNamespace(config=SimpleNamespace(detect_exists=detect_exists))
    obj.genPipelineCtx = SimpleNamespace(core_relations=["nation", "region"])
    obj.pj = SimpleNamespace(dependencies=dependencies if dependencies is not None else [[("nation", "n_name")]])
    # PGAOcontext exposes aggregated columns via `aggregated_attributes` (its
    # `aggregate` property is write-only and raises on read).
    obj.pgao_ctx = SimpleNamespace(aggregated_attributes=aggregated or [])
    obj.aoa = SimpleNamespace(
        algebraic_eq_predicates=eq_edges or [],
        aoa_predicates=aoa_le or [],
        aoa_less_thans=aoa_l or [])
    obj.instances = instances
    obj.alias_to_table = alias_to_table
    return obj


class _FakeProbeFactory:
    """Stands in for ExistsGateProbe; returns a scripted verdict per table."""

    def __init__(self, verdicts):
        self.verdicts = verdicts  # {tab: True/False/None}
        self.calls = []

    def __call__(self, connectionHelper, genCtx):
        return self

    def is_nonscaling_gate(self, query, tab):
        self.calls.append(tab)
        return self.verdicts.get(tab)


_PROBE_PATH = "mysite.unmasque.src.core.exists_gate_probe.ExistsGateProbe"


class GateAttributionTest(unittest.TestCase):

    def test_projected_tables_from_dependencies(self):
        obj = _make_pipeline(dependencies=[[("nation", "n_name")], []])
        self.assertEqual(obj._gate_projected_tables(), {"nation"})

    def test_projected_tables_include_aggregated_column(self):
        obj = _make_pipeline(dependencies=[[]],
                             aggregated=[(("customer", "c_acctbal"), "Sum")])
        self.assertEqual(obj._gate_projected_tables(), {"customer"})

    def test_joined_tables_from_equi_edges(self):
        obj = _make_pipeline(eq_edges=[[("nation", "n_regionkey"), ("region", "r_regionkey")]])
        self.assertEqual(obj._gate_joined_tables(), {"nation", "region"})

    def test_joined_tables_from_aoa(self):
        obj = _make_pipeline(aoa_le=[(("a", "x"), ("b", "y"))],
                             aoa_l=[(("c", "z"), ("d", "w"))])
        self.assertEqual(obj._gate_joined_tables(), {"a", "b", "c", "d"})

    def test_joined_tables_skips_constant_nodes(self):
        # AOA node can be a bare constant (not a (tab, attr) tuple); skip it.
        obj = _make_pipeline(aoa_le=[(("a", "x"), 42)])
        self.assertEqual(obj._gate_joined_tables(), {"a"})


class ReclassifyExistsGatesTest(unittest.TestCase):

    def test_region_reclassified_as_gate(self):
        obj = _make_pipeline()  # nation projected, region neither projected nor joined
        with mock.patch(_PROBE_PATH, _FakeProbeFactory({"region": True})):
            gates, kept = obj._reclassify_exists_gates(["nation", "region"], "Qh")
        self.assertEqual(gates, [{"tab": "region", "kind": "EXISTS"}])
        self.assertEqual(kept, ["nation"])

    def test_scaling_region_kept(self):
        obj = _make_pipeline()
        with mock.patch(_PROBE_PATH, _FakeProbeFactory({"region": False})):
            gates, kept = obj._reclassify_exists_gates(["nation", "region"], "Qh")
        self.assertEqual(gates, [])
        self.assertEqual(kept, ["nation", "region"])

    def test_inconclusive_region_kept_fail_closed(self):
        obj = _make_pipeline()
        with mock.patch(_PROBE_PATH, _FakeProbeFactory({"region": None})):
            gates, kept = obj._reclassify_exists_gates(["nation", "region"], "Qh")
        self.assertEqual(gates, [])
        self.assertEqual(kept, ["nation", "region"])

    def test_flag_off_no_detection(self):
        obj = _make_pipeline(detect_exists=False)
        with mock.patch(_PROBE_PATH, _FakeProbeFactory({"region": True})) as fp:
            gates, kept = obj._reclassify_exists_gates(["nation", "region"], "Qh")
        self.assertEqual(gates, [])
        self.assertEqual(kept, ["nation", "region"])

    def test_single_relation_no_detection(self):
        obj = _make_pipeline()
        with mock.patch(_PROBE_PATH, _FakeProbeFactory({"region": True})):
            gates, kept = obj._reclassify_exists_gates(["region"], "Qh")
        self.assertEqual(gates, [])
        self.assertEqual(kept, ["region"])

    def test_joined_region_not_a_candidate(self):
        # region carries a join edge -> condition (3) excludes it; never probed.
        obj = _make_pipeline(eq_edges=[[("nation", "n_regionkey"), ("region", "r_regionkey")]])
        fp = _FakeProbeFactory({"region": True})
        with mock.patch(_PROBE_PATH, fp):
            gates, kept = obj._reclassify_exists_gates(["nation", "region"], "Qh")
        self.assertEqual(gates, [])
        self.assertEqual(kept, ["nation", "region"])
        self.assertEqual(fp.calls, [])  # excluded before probing

    def test_never_strips_last_relation(self):
        # Pathological: both relations look like gates. The guard keeps >= 1.
        obj = _make_pipeline(dependencies=[[]])  # nothing projected
        with mock.patch(_PROBE_PATH, _FakeProbeFactory({"nation": True, "region": True})):
            gates, kept = obj._reclassify_exists_gates(["nation", "region"], "Qh")
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(gates), 1)


class GateProjectedRealPgaoTest(unittest.TestCase):
    """Regression: _gate_projected_tables must NOT touch PGAOcontext.aggregate
    (a write-only property whose getter raises NotImplementedError). It must
    read the concrete `aggregated_attributes` instead."""

    def test_no_notimplemented_on_real_pgao_context(self):
        from mysite.unmasque.src.core.dataclass.pgao_context import PGAOcontext
        obj = ExtractionPipeLine.__new__(ExtractionPipeLine)
        obj.logger = _DummyLogger()
        obj.pj = SimpleNamespace(dependencies=[[("nation", "n_name")]])
        ctx = PGAOcontext()                       # real object
        ctx.aggregated_attributes = [("", "Count(*)")]
        obj.pgao_ctx = ctx
        # Would raise NotImplementedError if it read ctx.aggregate.
        self.assertEqual(obj._gate_projected_tables(), {"nation"})


class StripGateRelationsTest(unittest.TestCase):

    def test_strips_instances_and_alias_map(self):
        obj = _make_pipeline(
            instances=[SimpleNamespace(table="nation", alias="nation"),
                       SimpleNamespace(table="region", alias="region")],
            alias_to_table={"nation": "nation", "region": "region"})
        obj._strip_gate_relations([{"tab": "region", "kind": "EXISTS"}])
        self.assertEqual([i.table for i in obj.instances], ["nation"])
        self.assertEqual(obj.alias_to_table, {"nation": "nation"})


# --------------------------------------------------------------------------- 3
def _make_qsg(arithmetic_filters, exists_gates, join_edges=None):
    qsg = QueryStringGenerator.__new__(QueryStringGenerator)
    qsg.logger = _DummyLogger()
    wc = QueryDetails()
    wc.arithmetic_filters = list(arithmetic_filters)
    wc.exists_gates = list(exists_gates)
    wc.join_edges = join_edges or []
    qsg._workingCopy = wc
    qsg.get_datatype = lambda key: "int"
    return qsg


class ExistsEmissionTest(unittest.TestCase):

    def _where(self, qsg):
        return qsg._QueryStringGenerator__generate_where_clause()

    def test_gate_predicate_moves_into_subquery(self):
        # nation filter stays outside; region filter moves inside EXISTS.
        qsg = _make_qsg(
            arithmetic_filters=[("nation", "n_nationkey", ">=", 5, 2147483647),
                                ("region", "r_regionkey", ">=", 3, 2147483647)],
            exists_gates=[{"tab": "region", "kind": "EXISTS"}])
        where = self._where(qsg)
        self.assertIn("EXISTS (SELECT 1 FROM region WHERE region.r_regionkey >= 3)", where)
        self.assertIn("nation.n_nationkey >= 5", where)
        # The region predicate must NOT appear as an outer conjunct: the only
        # 'region.' reference is the one inside the subquery.
        self.assertEqual(where.count("region.r_regionkey"), 1)
        # And it is ANDed in.
        self.assertIn(" and ", where)

    def test_not_exists_kind(self):
        qsg = _make_qsg(
            arithmetic_filters=[("region", "r_regionkey", ">=", 10, 2147483647)],
            exists_gates=[{"tab": "region", "kind": "NOT EXISTS"}])
        where = self._where(qsg)
        self.assertIn("NOT EXISTS (SELECT 1 FROM region WHERE region.r_regionkey >= 10)", where)

    def test_gate_with_no_inner_predicate(self):
        qsg = _make_qsg(arithmetic_filters=[],
                        exists_gates=[{"tab": "region", "kind": "EXISTS"}])
        where = self._where(qsg)
        self.assertEqual(where, "EXISTS (SELECT 1 FROM region)")

    def test_no_gates_is_unchanged(self):
        qsg = _make_qsg(
            arithmetic_filters=[("nation", "n_nationkey", ">=", 5, 2147483647)],
            exists_gates=[])
        where = self._where(qsg)
        self.assertEqual(where, "nation.n_nationkey >= 5")
        self.assertNotIn("EXISTS", where)


if __name__ == "__main__":
    unittest.main()

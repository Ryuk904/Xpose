"""WI-11 — wire outer joins ON by default (route to the JOIN...ON renderer).

The outer-join stage decides, per join edge, whether the edge is INNER or an
OUTER variant by a black-box nullability probe: it breaks the join key on D1,
runs Qh, and reads whether each table's projected attribute survives as a
non-null value ('h' = preserved/outer side) or vanishes/goes-null ('l' = inner
side). The (imp_t1, imp_t2) marker pair maps to a join type via
``QueryStringGenerator.join_map``:

    ('l','l') -> INNER JOIN          ('l','h') -> RIGHT OUTER JOIN
    ('h','l') -> LEFT OUTER JOIN     ('h','h') -> FULL OUTER JOIN

Detection (the markers) already existed; WI-11 is the *emission/routing* half:
turn the markers into the actual FROM shape, by default. Two things are tested
here on the REAL methods (no DB):

1. ``OuterJoin._seq_routes_to_join_on`` -- the routing decision. Before WI-11
   the stage decided "is this outer?" by *string-matching* ``q_candidate.count(
   'OUTER')`` on the already-rendered SQL (the WI-01 anti-pattern: a substring
   scan of the output). WI-11 decides it directly on the importance_dict markers:
   route to JOIN...ON iff some edge marker != ('l','l'); an all-inner sequence
   keeps the comma-FROM baseline. We drive the real predicate with a synthetic
   importance_dict and assert the verdict for every marker combination, the
   reversed-edge lookup, and the hardened missing-edge default.

2. The "exactly one FROM emitter" guard, on the REAL ``QueryStringGenerator``
   methods (instantiated via __new__ to bypass the DB-touching __init__). The
   JOIN...ON route calls ``clear_from_where_ops()`` -- wiping the comma-FROM
   ``from_op`` -- BEFORE ``generate_from_on_clause`` builds JOIN...ON, so the
   resulting FROM is *purely* the JOIN form and never both at once.
"""

import types
import unittest

from mysite.unmasque.src.core.outer_join import OuterJoin
from mysite.unmasque.src.util.QueryStringGenerator import QueryStringGenerator, QueryDetails


class _DummyLogger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _edge(a1, t1, a2, t2):
    # An edge as the stage builds it: [(attr, table), (attr, table)].
    return [(a1, t1), (a2, t2)]


def _route_self(importance_dict):
    """Duck-typed self exposing the REAL routing methods + a synthetic
    importance_dict, so the verdict is the production code's, not a re-impl."""
    self_ns = types.SimpleNamespace(importance_dict=importance_dict, logger=_DummyLogger())
    self_ns._seq_routes_to_join_on = types.MethodType(
        OuterJoin._seq_routes_to_join_on, self_ns)
    # _seq_routes_to_join_on calls the name-mangled __determine_join_edge_type.
    self_ns._OuterJoin__determine_join_edge_type = types.MethodType(
        OuterJoin._OuterJoin__determine_join_edge_type, self_ns)
    return self_ns


def _imp(edge, m1, m2):
    """importance_dict entry: key = tuple(edge); value = {table: marker}."""
    return {tuple(edge): {edge[0][1]: m1, edge[1][1]: m2}}


class SeqRoutesToJoinOnTest(unittest.TestCase):
    """The routing predicate: marker pair -> comma-FROM vs JOIN...ON."""

    def test_all_inner_keeps_comma_from(self):
        e = _edge('c_custkey', 'customer', 'o_custkey', 'orders')
        oj = _route_self(_imp(e, 'l', 'l'))
        self.assertFalse(oj._seq_routes_to_join_on([e]))  # INNER -> comma-FROM

    def test_left_outer_routes_to_join_on(self):
        e = _edge('n_regionkey', 'nation', 'r_regionkey', 'region')
        oj = _route_self(_imp(e, 'h', 'l'))  # (h,l) = LEFT OUTER
        self.assertTrue(oj._seq_routes_to_join_on([e]))

    def test_right_outer_routes_to_join_on(self):
        e = _edge('n_regionkey', 'nation', 'r_regionkey', 'region')
        oj = _route_self(_imp(e, 'l', 'h'))  # (l,h) = RIGHT OUTER
        self.assertTrue(oj._seq_routes_to_join_on([e]))

    def test_full_outer_routes_to_join_on(self):
        e = _edge('n_regionkey', 'nation', 'r_regionkey', 'region')
        oj = _route_self(_imp(e, 'h', 'h'))  # (h,h) = FULL OUTER
        self.assertTrue(oj._seq_routes_to_join_on([e]))

    def test_mixed_multiedge_with_one_outer_routes_to_join_on(self):
        # A sequence with an inner edge AND an outer edge must route to JOIN...ON
        # (the whole FROM has to be rendered structurally once an outer appears).
        e_inner = _edge('c_custkey', 'customer', 'o_custkey', 'orders')
        e_outer = _edge('o_orderkey', 'orders', 'l_orderkey', 'lineitem')
        imp = {**_imp(e_inner, 'l', 'l'), **_imp(e_outer, 'h', 'l')}
        oj = _route_self(imp)
        self.assertTrue(oj._seq_routes_to_join_on([e_inner, e_outer]))

    def test_all_inner_multiedge_keeps_comma_from(self):
        e1 = _edge('c_custkey', 'customer', 'o_custkey', 'orders')
        e2 = _edge('o_orderkey', 'orders', 'l_orderkey', 'lineitem')
        imp = {**_imp(e1, 'l', 'l'), **_imp(e2, 'l', 'l')}
        oj = _route_self(imp)
        self.assertFalse(oj._seq_routes_to_join_on([e1, e2]))

    def test_reversed_edge_lookup_still_resolves_marker(self):
        # __create_final_edge_seq can hand us an edge in the reversed orientation
        # of the importance_dict key; the marker must still be found (by table
        # name, not position) and the routing verdict preserved.
        e = _edge('n_regionkey', 'nation', 'r_regionkey', 'region')
        e_rev = list(reversed(e))
        oj = _route_self(_imp(e, 'h', 'l'))           # dict keyed by e
        self.assertTrue(oj._seq_routes_to_join_on([e_rev]))  # queried with e_rev

    def test_missing_edge_defaults_to_inner_no_crash(self):
        # Hardened __determine_join_edge_type: an edge absent from importance_dict
        # defaults to ('l','l') (the sound INNER -> comma-FROM direction) instead
        # of raising UnboundLocalError, which would crash the now-default stage.
        e = _edge('a', 't1', 'b', 't2')
        oj = _route_self({})  # empty importance_dict
        self.assertFalse(oj._seq_routes_to_join_on([e]))

    def test_one_missing_one_outer_still_routes_to_join_on(self):
        e_missing = _edge('a', 't1', 'b', 't2')
        e_outer = _edge('n_regionkey', 'nation', 'r_regionkey', 'region')
        oj = _route_self(_imp(e_outer, 'l', 'h'))  # only the outer edge is known
        self.assertTrue(oj._seq_routes_to_join_on([e_missing, e_outer]))


class ExactlyOneFromEmitterTest(unittest.TestCase):
    """The guard: the JOIN...ON route REPLACES the comma-FROM, never appends."""

    def _qsg(self):
        # Real QSG, but bypass __init__ (which builds a DB executable). Only the
        # _workingCopy / logger and the class-level join_map + from_op/where_op
        # property descriptors are needed by the two methods under test.
        qsg = QueryStringGenerator.__new__(QueryStringGenerator)
        qsg._workingCopy = QueryDetails()
        qsg.logger = _DummyLogger()
        return qsg

    def test_clear_empties_from_and_where(self):
        qsg = self._qsg()
        qsg._workingCopy.from_op = "nation, region"
        qsg._workingCopy.where_op = "nation.n_regionkey = region.r_regionkey"
        qsg.clear_from_where_ops()
        self.assertEqual('', qsg.from_op)
        self.assertEqual('', qsg.where_op)

    def test_join_on_route_replaces_comma_from(self):
        qsg = self._qsg()
        qsg._workingCopy.from_op = "nation, region"  # the comma-FROM baseline
        e = _edge('n_regionkey', 'nation', 'r_regionkey', 'region')

        # Outer route: clear the comma-FROM, then build JOIN...ON.
        qsg.clear_from_where_ops()
        qsg.generate_from_on_clause(e, [], 'h', 'l', 'nation', 'region')

        from_op = qsg.from_op
        self.assertIn('LEFT OUTER JOIN', from_op)
        self.assertIn('ON nation.n_regionkey = region.r_regionkey', from_op)
        self.assertIn('nation', from_op)
        self.assertIn('region', from_op)
        # Exactly one emitter: the comma-separated table list is GONE -- the
        # JOIN...ON form replaced it rather than coexisting with it.
        self.assertNotIn('nation, region', from_op)

    def test_inner_marker_renders_inner_join_no_outer(self):
        # Why the routing decides on the marker, not the string: an ('l','l')
        # edge renders ' INNER JOIN ' (no 'OUTER'); routing simply never invokes
        # the renderer for an all-inner seq, keeping comma-FROM.
        qsg = self._qsg()
        e = _edge('c_custkey', 'customer', 'o_custkey', 'orders')
        qsg.clear_from_where_ops()
        qsg.generate_from_on_clause(e, [], 'l', 'l', 'customer', 'orders')
        from_op = qsg.from_op
        self.assertIn('INNER JOIN', from_op)
        self.assertNotIn('OUTER', from_op)


if __name__ == '__main__':
    unittest.main()

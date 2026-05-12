"""
Per-(alias, attribute) discriminator probe (report section F).

Algorithm 4 (:mod:`per_alias_filter`) recovers the *multiset* of per-alias filter
bounds on a column, but not which alias owns which -- because, with the aliases
free w.r.t. that column, the assignment is observationally irrelevant (a
``t1 <-> t2`` relabel is a no-op).  When an *inter-alias chain* on some column
``d`` pins the aliases to specific rows (``t_{a1}.d < t_{a2}.d < ... < t_{ak}.d``,
``k`` distinct ascending values), though, the assignment *is* identifiable:

1. discriminate ``d`` -- give the ``k`` rows of the alias-aware D_min ``k`` distinct
   ascending values -- so the chain forces alias ``a_i`` onto the i-th-smallest-``d``
   row;
2. for every other column ``c`` that Algorithm 4 found a per-alias bound on, recover
   alias ``a_i``'s bound on ``c`` directly: vary *just that one pinned row's* ``c``
   and binary-search the FIT/UNFIT boundary.  Every other alias keeps binding its
   own row (forced by the chain on ``d``), whose ``c`` is already inside its interval
   (Q_H is FIT on the D_min), so Q_H stays FIT iff the varied value lies in alias
   ``a_i``'s interval -- the boundary is exactly ``a_i``'s bound.

This works for any ``k >= 2`` (the v1 used a single confirming probe and handled
only ``k = 2``; the direct binary search is both simpler and complete).  When no
chain pins all ``k`` aliases the attribution is left to the verifier-guided search
in the assembler.

Still not done here (would need the legacy SPJGAOL extractors' join graph to be
alias-aware -- see docs/multi_instance.md §6): attributing the *legacy join* edge
``R.fk = S.x`` to a specific alias of ``R`` when ``fk`` is not alias-coupled (when
it is, the assembler's coupled-column chain ``R_a1.fk = R_a2.fk = ...`` already
carries the join to every alias, so it is exact there).

Like the other multi-instance stages this probes inside a transaction that is
rolled back; it is purely additive and gated behind ``[feature] multi_instance``.
Output: ``pinned_filters[tab] = {alias_index -> {col -> {'lower': l_or_None,
'upper': u_or_None}}}`` (alias_index is 1-based, in the chain's ascending order).
"""
from .abstract.AppExtractorBase import AppExtractorBase
from .alias_aware_assembler import _topo_order_slots
from .cross_alias_predicate import spread_values, _is_numeric
from .per_alias_filter import find_step_breakpoints, domain_endpoint, _orderable


# Probe calls allowed per side per (alias, column).  Must comfortably exceed
# log2(domain span) -- ~31 for the ±2**31 int/numeric endpoints -- so an int bound
# is pinned exactly and a numeric one to ~milli precision (find_step_breakpoints
# falls back to the coarse bracket if it still runs out, so this is a quality knob,
# not a correctness one).
_BISECT_BUDGET = 40


def recover_bound_via_fit_probe(fit_probe, start_val, dtype, direction, budget=_BISECT_BUDGET):
    """Binary-search one side of an alias's filter interval on a column.

    ``fit_probe(v) -> bool``: True iff Q_H stays FIT when *this* alias's pinned row
    has the column set to ``v`` (every other alias keeps binding its own row, already
    inside its interval).  ``start_val`` is a value strictly inside the interval (the
    D_min value -- Q_H is FIT there).  ``direction`` > 0 searches the upper bound, < 0
    the lower.  Returns the bound, or ``None`` if the interval is unbounded on that
    side (Q_H still FIT at the type's domain endpoint) or the boundary could not be
    bracketed.  Pure -- testable with a mock ``fit_probe``.
    """
    far_v = domain_endpoint(dtype, direction)
    if (direction > 0 and not far_v > start_val) or (direction < 0 and not far_v < start_val):
        return None
    try:
        if fit_probe(far_v):
            return None                               # unbounded on this side
    except Exception:
        return None
    probe = lambda v: 1 if fit_probe(v) else 0
    if direction > 0:
        bps = find_step_breakpoints(probe, start_val, far_v, 1, 0, dtype, [budget])
        return bps[0][0] if bps else None             # last value still FIT == the upper bound
    bps = find_step_breakpoints(probe, far_v, start_val, 0, 1, dtype, [budget])
    return bps[0][1] if bps else None                 # first value FIT (going up) == the lower bound


class PerAliasPinnedFilter(AppExtractorBase):
    """Attributes Algorithm 4's per-alias bound multiset to specific aliases when an
    inter-alias chain pins them (any ``k >= 2``)."""

    def __init__(self, connectionHelper, core_relations, mult,
                 alias_aware_min_instance_dict, cross_alias_predicates, per_alias_filters):
        super().__init__(connectionHelper, "PerAliasPinnedFilter")
        self.core_relations = list(dict.fromkeys(core_relations))
        self.mult = dict(mult or {})
        self.alias_aware_min_instance_dict = alias_aware_min_instance_dict or {}
        self.cross_alias_predicates = dict(cross_alias_predicates or {})
        self.per_alias_filters = dict(per_alias_filters or {})
        self.pinned_filters = {}
        self.notes = {}

    # ------------------------------------------------------------------ API ---
    def extract_params_from_args(self, args):
        return args[0]

    def doActualJob(self, args=None):
        query = self.extract_params_from_args(args)
        self.set_data_schema()
        try:
            self.connectionHelper.commit_transaction()
        except Exception as e:
            self.logger.debug(f"pre-probe commit: {e}")
        for tab in self.core_relations:
            k = int(self.mult.get(tab, 1))
            if k < 2:
                continue
            paf = self.per_alias_filters.get(tab) or {}
            if not paf:
                continue
            d, chain = self._full_chain_column(tab, k)
            if d is None:
                self.notes[tab] = f"no inter-alias chain pins all {k} aliases -- attribution skipped"
                continue
            try:
                attributed = self._attribute(query, tab, d, k, paf)
            except Exception as e:
                self.logger.error(f"PerAliasPinnedFilter failed on {tab}: {e}")
                self._rollback()
                attributed = {}
            if attributed:
                self.pinned_filters[tab] = attributed
                self.notes[tab] = f"ok (chain on {d} pins {k} aliases)"
                self.logger.info(f"pinned per-alias filters [{tab}]: {attributed}")
            else:
                self.notes[tab] = "no per-alias bound could be attributed to a specific alias"
        return self.pinned_filters

    # ----------------------------------------------------------- transactions --
    def _begin(self):
        self.connectionHelper.begin_transaction()

    def _rollback(self):
        try:
            self.connectionHelper.rollback_transaction()
        except Exception as e:
            self.logger.error(f"rollback failed: {e}")

    # --------------------------------------------------------------- helpers ---
    def _fq(self, tab):
        return self.get_fully_qualified_table_name(tab)

    def _exec(self, *sqls):
        self.connectionHelper.execute_sql(list(sqls), self.logger)

    def _fit(self, query):
        res = self.app.doJob(query)
        return isinstance(res, list) and self.app.isQ_result_nonEmpty_nullfree(res)

    def _column_types(self, tab):
        try:
            rows, _ = self.connectionHelper.execute_sql_fetchall(
                self.connectionHelper.queries.get_column_details_for_table(
                    self.connectionHelper.config.schema, tab))
        except Exception:
            return {}
        return {r[0]: r[1] for r in (rows or [])}

    @staticmethod
    def _lit(val, dtype):
        if val is None:
            return "NULL"
        if _is_numeric(dtype):
            return str(val)
        return "'" + str(val).replace("'", "''") + "'"

    def _materialize(self, tab, header, rows, types):
        cols = ", ".join(str(c) for c in header)
        self._exec(f"truncate table {self._fq(tab)};")
        if not rows:
            return
        chunks = ["(" + ", ".join(self._lit(v, types.get(h, "")) for h, v in zip(header, r)) + ")"
                  for r in rows]
        self._exec(f"insert into {self._fq(tab)} ({cols}) values " + ", ".join(chunks) + ";")

    def _full_chain_column(self, tab, k):
        """A column with an inter-alias chain whose topo order covers all ``k`` aliases."""
        by_col = {}
        for p in self.cross_alias_predicates.get(tab, []):
            if p.get('kind') == 'inter':
                by_col.setdefault(p['col'], []).append(p)
        for col, preds in by_col.items():
            chain = _topo_order_slots(preds)
            if len(set(chain)) == k:
                return col, chain
        return None, None

    # --------------------------------------------------------- the probe ---
    def _attribute(self, query, tab, d, k, paf):
        aa = self.alias_aware_min_instance_dict.get(tab)
        if not aa or len(aa) - 1 < k:
            return {}
        header = list(aa[0])
        rows = [list(r) for r in aa[1:1 + k]]
        types = self._column_types(tab)
        if d not in header:
            return {}
        di = header.index(d)

        self._begin()
        try:
            # discriminate d so the chain pins alias i (1-based) -> i-th-smallest-d row
            cur_d = [rows[r][di] for r in range(k)]
            spread = spread_values(cur_d, k, types.get(d, ""))
            if spread is None:
                if len(set(str(x) for x in cur_d)) != k:
                    return {}                          # can't make the chain column distinct
                target_d = sorted(cur_d, key=str)
            else:
                target_d = list(spread)
            for r in range(k):
                rows[r][di] = target_d[r]
            # order rows by d ascending: rows[i] is now alias (i+1)'s pinned row
            rows.sort(key=lambda rr: (str(rr[di]) if isinstance(rr[di], str) else rr[di]))
            self._materialize(tab, header, rows, types)
            if not self._fit(query):
                return {}                              # the chain isn't being cleanly pinned

            out = {}
            for c, info in paf.items():
                if c == d or c not in header:
                    continue
                ci = header.index(c)
                dt = types.get(c, "")
                if not _orderable(dt):
                    continue
                for ai in range(k):                    # ai -> alias index ai + 1
                    orig = rows[ai][ci]
                    if orig is None:
                        continue

                    def fit_probe(v, _ai=ai, _ci=ci):
                        rows[_ai][_ci] = v
                        self._materialize(tab, header, rows, types)
                        return self._fit(query)

                    try:
                        if not fit_probe(orig):        # D_min value should be inside the interval
                            continue
                        up = recover_bound_via_fit_probe(fit_probe, orig, dt, +1)
                        lo = recover_bound_via_fit_probe(fit_probe, orig, dt, -1)
                    finally:
                        rows[ai][ci] = orig
                        self._materialize(tab, header, rows, types)
                    if up is None and lo is None:
                        continue
                    out.setdefault(ai + 1, {})[c] = {'lower': lo, 'upper': up}
            return out
        finally:
            self._rollback()

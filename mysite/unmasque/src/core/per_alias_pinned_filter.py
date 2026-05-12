"""
Per-(alias, attribute) discriminator probe (report section F, lines 15-21).

Algorithm 4 (:mod:`per_alias_filter`) recovers the *set* of per-alias filter bounds
on a column, but not which alias owns which bound -- because, with the aliases free
w.r.t. that column, the assignment is observationally irrelevant (a ``t1 <-> t2``
relabel is a no-op).  When an *inter-alias chain* on some column ``d`` pins the
aliases to specific rows, though, the assignment *is* identifiable: discriminate
``d`` (give the k rows of the alias-aware D_min distinct, ascending values), so the
chain ``t_{a1}.d < t_{a2}.d < ...`` forces alias ``a_i`` onto the i-th-smallest-``d``
row; then a *targeted mutation* of that row reveals alias ``a_i``'s bound on every
other column.

v1 handles the clean ``k = 2`` case with a single confirming probe: for a column ``c``
whose Algorithm-4 bound multiset on the upper side is ``[u_tight, u_loose]`` (distinct),
set ``a1``'s row's ``c`` to ``u_loose``; if Q_H stays FIT, ``a1`` accepts ``u_loose``
so ``a1``'s upper bound is ``u_loose`` and ``a2``'s is ``u_tight`` (and vice versa).
Lower bounds symmetrically.  ``k >= 3`` is left to the verifier-guided search in the
assembler.

Like the other multi-instance stages this probes inside a transaction that is rolled
back; it is purely additive and gated behind ``[feature] multi_instance``.  Output:
``pinned_filters[tab] = {alias_index -> {col -> {'lower': l_or_None, 'upper': u_or_None}}}``.
"""
from .abstract.AppExtractorBase import AppExtractorBase
from .alias_aware_assembler import _topo_order_slots
from .cross_alias_predicate import spread_values, _is_numeric


class PerAliasPinnedFilter(AppExtractorBase):
    """Attributes Algorithm 4's per-alias bound multiset to specific aliases when an
    inter-alias chain pins them (k = 2 in v1)."""

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
            if int(self.mult.get(tab, 1)) != 2:
                continue                               # v1: k == 2 only
            paf = self.per_alias_filters.get(tab) or {}
            if not paf:
                continue
            d, _topo = self._full_chain_column(tab, k=2)
            if d is None:
                self.notes[tab] = "no inter-alias chain pins the aliases -- attribution skipped"
                continue
            try:
                attributed = self._attribute(query, tab, d, paf)
            except Exception as e:
                self.logger.error(f"PerAliasPinnedFilter failed on {tab}: {e}")
                self._rollback()
                attributed = {}
            if attributed:
                self.pinned_filters[tab] = attributed
                self.notes[tab] = "ok"
                self.logger.info(f"pinned per-alias filters [{tab}]: {attributed}")
            else:
                self.notes[tab] = "no column had distinct per-alias bounds to attribute"
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
    def _attribute(self, query, tab, d, paf):
        aa = self.alias_aware_min_instance_dict.get(tab)
        if not aa or len(aa) - 1 < 2:
            return {}
        header = list(aa[0])
        rows = [list(r) for r in aa[1:3]]
        types = self._column_types(tab)
        if d not in header:
            return {}
        di = header.index(d)

        self._begin()
        try:
            # discriminate d so the chain pins a1 = smaller-d row, a2 = larger-d row
            cur_d = [rows[r][di] for r in range(2)]
            spread = spread_values(cur_d, 2, types.get(d, ""))
            target_d = list(spread) if spread is not None else sorted(cur_d, key=str)
            for r in range(2):
                rows[r][di] = target_d[r]
            # order rows by d so rows[0] has the smaller d (= alias 1)
            rows.sort(key=lambda rr: (rr[di] if not isinstance(rr[di], str) else str(rr[di])))
            self._materialize(tab, header, rows, types)
            if not self._fit(query):
                return {}                              # the chain isn't being pinned cleanly

            out = {1: {}, 2: {}}
            for c, info in paf.items():
                if c == d or c not in header:
                    continue
                ci = header.index(c)
                up_ms = list(info.get('upper_multiset') or sorted(info.get('upper') or [], key=str))
                lo_ms = list(info.get('lower_multiset')
                             or sorted(info.get('lower') or [], key=str, reverse=True))
                up1 = self._attr_one_side(query, tab, header, rows, ci, types, up_ms, lower=False)
                lo1 = self._attr_one_side(query, tab, header, rows, ci, types, lo_ms, lower=True)
                if up1 is None and lo1 is None:
                    continue
                out[1][c] = {'upper': (up1[0] if up1 else None), 'lower': (lo1[0] if lo1 else None)}
                out[2][c] = {'upper': (up1[1] if up1 else None), 'lower': (lo1[1] if lo1 else None)}
            return {ai: cols for ai, cols in out.items() if cols}
        finally:
            self._rollback()

    def _attr_one_side(self, query, tab, header, rows, ci, types, multiset, lower):
        """Returns ``(bound_for_a1, bound_for_a2)`` from a 2-element distinct multiset,
        or ``None`` if there is nothing to attribute (uniform / empty)."""
        if len(multiset) < 2:
            return None
        # multiset is tightest-first; for the upper side: [u_tight, u_loose] (ascending);
        # for the lower side: [l_tight, l_loose] (descending, i.e. l_tight = max).
        tight, loose = multiset[0], multiset[-1]
        if str(tight) == str(loose):
            return None
        # set a1's row's column ci to the *loose* bound; if Q_H stays FIT, a1 accepts it.
        orig = rows[0][ci]
        rows[0][ci] = loose
        try:
            self._materialize(tab, header, rows, types)
            a1_takes_loose = self._fit(query)
        finally:
            rows[0][ci] = orig
            self._materialize(tab, header, rows, types)
        return (loose, tight) if a1_takes_loose else (tight, loose)

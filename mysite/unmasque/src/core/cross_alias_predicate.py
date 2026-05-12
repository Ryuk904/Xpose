"""
Algorithm 3 -- cross-alias predicate extraction (discriminator injection).

Once the multiplicity ``mult(R) = k`` is known (Algorithm 1) and an alias-aware
D_min with ``k`` distinct rows of ``R`` is available (Algorithm 2), this stage
recovers the predicates that relate the ``k`` aliases of ``R`` to each other --
the ones the legacy pipeline cannot even represent, let alone find:

* **intra-alias self-equi-joins** ``t.c = t.c'`` -- a within-row column equality
  on a single alias.  Detectable on a *single-row* copy of the witness before any
  inflation (report section D.3.1): if the witness row has ``R.c = R.c'`` and
  changing ``R.c`` alone makes Q_H UNFIT but also setting ``R.c' := R.c`` makes it
  FIT again, the query contains ``t.c = t.c'``.

* **inter-alias predicates on the same column** ``t_p.c REL t_q.c`` for
  ``REL in {=, <, >}`` (and "no relation").  We build *discriminator windows*:
  give column ``c`` ``k`` distinct, ascending values across the ``k`` rows of the
  alias-aware D_min (where the data range allows it without breaking Q_H), run
  ``Q_H`` once, and look at the output slots that expose ``c``.  In every output
  row the slot for ``t_p.c`` carries some window value and the slot for ``t_q.c``
  carries some window value; if ``t_p.c`` is always in a strictly lower window
  than ``t_q.c`` the query has ``t_p.c < t_q.c``; if always the same window,
  ``t_p.c = t_q.c``; if mixed, the two aliases are free w.r.t. ``c``.  This is the
  black-box reading of which alias-to-row binding combinations the hidden query
  keeps -- the provenance combinations of report section D.

What v1 deliberately does *not* do (kept as future work, noted in :attr:`notes`):
cross-*column* cross-alias predicates (``t_p.a < t_q.b`` with ``a != b``), and
cross-alias predicates on columns of ``R`` that the query does not project (those
need the s-value-bound-floating machinery Xpose already has for *single*-table
algebraic predicates, lifted to alias pairs).

Like Algorithms 1 & 2 this stage probes inside a transaction that is rolled back,
so the live D_min is untouched; it is purely additive and gated behind the
``[feature] multi_instance`` flag.  Its output is published on
``self.cross_alias_predicates`` for the (still to be made alias-aware) query
assembler.
"""
from datetime import date, timedelta

from .abstract.AppExtractorBase import AppExtractorBase
from ..util.constants import NON_TEXT_TYPES


def _is_numeric(dtype):
    d = str(dtype).lower()
    return any(t in d for t in ("int", "numeric", "double", "real", "decimal", "serial"))


def _is_date(dtype):
    d = str(dtype).lower()
    return ("date" in d or "timestamp" in d) and "interval" not in d


def _is_discriminable(dtype):
    d = str(dtype).lower()
    return _is_numeric(dtype) or _is_date(dtype) or any(t in d for t in NON_TEXT_TYPES)


def spread_values(current, k, dtype):
    """``k`` distinct, ascending values 'near' the ones already present.

    Returns ``None`` if a safe spread is not possible (data range too narrow for
    ``k`` distinct integers, type not orderable here, ...) -- the caller then
    leaves the column alone.
    """
    vals = [v for v in current if v is not None]
    if not vals:
        return None
    try:
        lo, hi = min(vals), max(vals)
    except TypeError:
        return None
    if _is_numeric(dtype):
        is_int = "int" in str(dtype).lower() or "serial" in str(dtype).lower()
        if lo == hi:
            return [lo + j for j in range(k)] if is_int else [lo + j * 0.001 for j in range(k)]
        if is_int:
            if hi - lo < k - 1:
                return None
            step = (hi - lo) // (k - 1)
            out = [lo + j * step for j in range(k)]
        else:
            step = (hi - lo) / (k - 1)
            out = [lo + j * step for j in range(k)]
        return out if len(set(out)) == k else None
    if _is_date(dtype):
        if not isinstance(lo, date):
            return None
        if lo == hi:
            return [lo + timedelta(days=j) for j in range(k)]
        span = (hi - lo).days
        if span < k - 1:
            return None
        step = max(1, span // (k - 1))
        out = [lo + timedelta(days=j * step) for j in range(k)]
        return out if len(set(out)) == k else None
    return None


def _window_index(value, ascending_windows):
    """Position (1-based) of ``value`` among the sorted window values, or None."""
    for i, w in enumerate(ascending_windows):
        if value == w or str(value) == str(w):
            return i + 1
    return None


def infer_inter_alias_predicates(out_rows, disc_windows):
    """Read ``t_p.c REL t_q.c`` predicates off the output of Q_H on the
    discriminated D_min.

    ``out_rows`` is the result with row 0 the header; ``disc_windows`` maps a
    column name to its sorted-ascending list of ``k`` window values.  Returns a
    list of ``(col, slot_p, slot_q, op)`` with ``slot_p < slot_q`` output-column
    indices both exposing ``col`` and ``op in {'=', '<', '>'}``; pairs with no
    consistent relationship are omitted.
    """
    if not out_rows or len(out_rows) < 2:
        return []
    data = out_rows[1:]
    n_cols = len(out_rows[0])
    preds = []
    for col, windows in disc_windows.items():
        carrying = []
        for s in range(n_cols):
            seen, ok = set(), True
            for r in data:
                wi = _window_index(r[s], windows)
                if wi is None:
                    ok = False
                    break
                seen.add(wi)
            if ok and seen:
                carrying.append(s)
        if len(carrying) < 2:
            continue
        for a in range(len(carrying)):
            for b in range(a + 1, len(carrying)):
                sp, sq = carrying[a], carrying[b]
                rel = set()
                for r in data:
                    ip, iq = _window_index(r[sp], windows), _window_index(r[sq], windows)
                    rel.add('=' if ip == iq else ('<' if ip < iq else '>'))
                # the t_i = t_i self-pairs (which a non-strict `<=`/`>=` keeps) add an '='
                # to an otherwise-strictly-ordered relation set -- read those as `<=`/`>=`.
                op = {frozenset({'='}): '=', frozenset({'<'}): '<', frozenset({'>'}): '>',
                      frozenset({'<', '='}): '<=', frozenset({'>', '='}): '>='}.get(frozenset(rel))
                if op:
                    preds.append((col, sp, sq, op))
    return preds


def attribute_output_columns(out_rows, disc_windows):
    """Map output columns to ``(alias_index, source_col)`` of the discriminated
    table.  A column qualifies iff its value is constant across every output row and
    uniquely equals one discriminator window value; columns whose value varies are
    alias-symmetric (no inter-alias predicate pins them) and are omitted.  Returns
    ``{output_col_index -> (alias_index, source_col)}`` (alias_index is 1-based).
    """
    if not out_rows or len(out_rows) < 2:
        return {}
    data = out_rows[1:]
    out = {}
    for s in range(len(out_rows[0])):
        vals = {str(r[s]) for r in data}
        if len(vals) != 1:
            continue
        v = next(iter(vals))
        matches = [(c, j + 1) for c, ws in disc_windows.items()
                   for j, w in enumerate(ws) if str(w) == v]
        if len(matches) == 1:
            c, ai = matches[0]
            out[s] = (ai, c)
    return out


class CrossAliasPredicate(AppExtractorBase):
    """Extracts intra- and inter-alias predicates for every multi-instance table.

    Public outputs after :meth:`doJob`:

    * ``cross_alias_predicates`` -- ``{table -> [pred, ...]}`` where each ``pred``
      is a dict, e.g. ``{'kind': 'intra_eq', 'cols': (c, c')}`` or
      ``{'kind': 'inter', 'col': c, 'op': '<', 'slots': (sp, sq)}``.
    * ``coupled_columns`` -- ``{table -> [cols]}`` columns whose ``k`` values could
      not be made distinct without breaking Q_H (candidate cross-alias equi-join
      columns, or columns pinned by a tight filter / join to another table).
    * ``output_attribution`` -- ``{output_col_index -> (table, alias_index, source_col)}``
      for the output columns of Q_H that a discriminated multi-instance table pins to a
      specific alias (this is the "alias-lift" of the projection extractor: it tells the
      assembler which ``R_a<j>`` a projected column really belongs to).  Only populated for
      columns whose value is constant across the output and uniquely matches one
      discriminator window; columns left out are observationally alias-symmetric anyway.
    * ``notes`` -- per-table free-text remarks about what was / wasn't analysable.
    """

    def __init__(self, connectionHelper, core_relations, mult, alias_aware_min_instance_dict):
        super().__init__(connectionHelper, "CrossAliasPredicate")
        self.core_relations = list(dict.fromkeys(core_relations))
        self.mult = dict(mult or {})
        self.alias_aware_min_instance_dict = alias_aware_min_instance_dict or {}
        self.cross_alias_predicates = {}
        self.coupled_columns = {}
        self.output_attribution = {}
        self._attr_conflicts = set()
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
            k = max(1, int(self.mult.get(tab, 1)))
            if k <= 1:
                continue
            try:
                preds, coupled, note = self._analyse_one(query, tab, k)
            except Exception as e:
                self.logger.error(f"CrossAliasPredicate failed on {tab}: {e}")
                self._rollback()
                preds, coupled, note = [], [], f"analysis raised: {e}"
            self.cross_alias_predicates[tab] = preds
            self.coupled_columns[tab] = coupled
            self.notes[tab] = note
            self.logger.info(f"cross-alias predicates for {tab}: {preds or 'none'}"
                             + (f"; coupled cols: {coupled}" if coupled else ""))
        return self.cross_alias_predicates

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

    def _q(self, query):
        res = self.app.doJob(query)
        return res if isinstance(res, list) else None

    def _fit(self, query):
        res = self._q(query)
        return bool(res) and self.app.isQ_result_nonEmpty_nullfree(res)

    def _card(self, query):
        res = self._q(query)
        return (len(res) - 1) if isinstance(res, list) and len(res) >= 1 else 0

    def _column_types(self, tab):
        try:
            rows, _ = self.connectionHelper.execute_sql_fetchall(
                self.connectionHelper.queries.get_column_details_for_table(
                    self.connectionHelper.config.schema, tab))
        except Exception as e:
            self.logger.error(f"could not read columns of {tab}: {e}")
            return []
        return [(r[0], r[1]) for r in (rows or [])]

    @staticmethod
    def _lit(val, dtype):
        if val is None:
            return "NULL"
        if _is_numeric(dtype):
            return str(val)
        return "'" + str(val).replace("'", "''") + "'"

    def _materialize(self, tab, header, rows, types):
        """Working table := exactly ``rows`` (lists of values matching ``header``).

        Rows are always rebuilt, never updated in place -- an in-place UPDATE
        changes a row's ctid, which would make any subsequent ctid-keyed edit miss.
        """
        cols = ", ".join(str(c) for c in header)
        self._exec(f"truncate table {self._fq(tab)};")
        if not rows:
            return
        chunks = []
        for r in rows:
            chunks.append("(" + ", ".join(self._lit(v, types.get(h, "")) for h, v in zip(header, r)) + ")")
        self._exec(f"insert into {self._fq(tab)} ({cols}) values " + ", ".join(chunks) + ";")

    def _read_col_sorted(self, tab, col):
        rows, _ = self.connectionHelper.execute_sql_fetchall(
            f"select {col} from {self._fq(tab)} order by {col};")
        return [r[0] for r in (rows or [])]

    # ---------------------------------------- step 0: intra-alias self-equi ---
    def _detect_intra_alias(self, query, tab, header, witness_row, types):
        """``t.c = t.c'`` -- probed on a one-row copy of the witness."""
        self._materialize(tab, header, [list(witness_row)], types)
        if not self._fit(query):
            return []
        rowvals = {header[i]: witness_row[i] for i in range(len(header))}
        names = [h for h in header if _is_discriminable(types.get(h, ""))]
        found = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                ci, cj = names[i], names[j]
                vi, vj = rowvals.get(ci), rowvals.get(cj)
                if vi is None or vj is None or str(vi) != str(vj):
                    continue  # necessary condition: the witness row has c == c'
                for v in self._nearby_values(vi, types.get(ci, "")):
                    trial = dict(rowvals)
                    trial[ci] = v
                    self._materialize(tab, header, [[trial[h] for h in header]], types)
                    if self._fit(query):
                        break  # changing ci alone is fine -> ci not coupled to cj
                    trial[cj] = v
                    self._materialize(tab, header, [[trial[h] for h in header]], types)
                    repaired = self._fit(query)
                    if repaired:
                        found.append((ci, cj))
                        break
        # leave the table holding the plain witness row
        self._materialize(tab, header, [list(witness_row)], types)
        return found

    @staticmethod
    def _nearby_values(v, dtype):
        if _is_numeric(dtype):
            if "int" in str(dtype).lower() or "serial" in str(dtype).lower():
                return [int(v) + 1, int(v) - 1]
            return [v + 1, v - 1]
        if _is_date(dtype) and isinstance(v, date):
            return [v + timedelta(days=1), v - timedelta(days=1)]
        return [str(v) + "Z", "A" + str(v)]

    # ---------------------------- step 1+2: discriminators + slot inference ---
    def _analyse_one(self, query, tab, k):
        aa = self.alias_aware_min_instance_dict.get(tab)
        if not aa or len(aa) - 1 < k:
            return [], [], "no k-row alias-aware D_min available"
        col_types = self._column_types(tab)
        if not col_types:
            return [], [], "could not read column metadata"
        header = list(aa[0])
        rows = [list(r) for r in aa[1:1 + k]]
        types = {name: dt for name, dt in col_types}

        self._begin()
        try:
            preds = []

            # --- step 0: intra-alias self-equi-joins (on a one-row copy) ---
            intra = self._detect_intra_alias(query, tab, header, rows[0], types)
            for ci, cj in intra:
                preds.append({'kind': 'intra_eq', 'cols': (ci, cj)})

            # columns that must move together because of an intra-alias equality
            rep = {h: h for h in header}
            for ci, cj in intra:
                rep[cj] = rep[ci]
            # one pass of path-compression so chains a=b=c land in one class
            for h in header:
                seen = set()
                while rep[h] != h and rep[h] not in seen:
                    seen.add(rep[h])
                    rep[h] = rep[rep[h]]

            # --- materialise the alias-aware D_min ---
            cur_rows = [list(r) for r in rows]
            self._materialize(tab, header, cur_rows, types)
            base_card = self._card(query)
            if base_card < 1:
                return preds, [], "alias-aware D_min not FIT after re-materialise"
            # A self-join's D_min normally exhibits cross-row pairs (|Q_H| > k -- not
            # just the k trivial t_i = t_i self-pairs); if it does, discriminating the
            # equi-join key collapses |Q_H| down to ~k, which is how we tell that column
            # is *coupled* (essential to the join) rather than freely discriminable.
            # If the D_min is already degenerate (|Q_H| <= k) we can't make that call,
            # so we fall back to the FIT-only check.
            track_card = base_card > k

            # --- discriminate column-groups, one at a time ---
            disc_windows = {}
            coupled = []
            handled = set()
            for h in header:
                if h in handled or not _is_discriminable(types.get(h, "")):
                    continue
                group = [g for g in header
                         if rep[g] == rep[h] and _is_discriminable(types.get(g, ""))]
                handled.update(group)
                gi = header.index(h)
                cur_vals = [cur_rows[r][gi] for r in range(k)]
                spread = spread_values(cur_vals, k, types.get(h, ""))
                already_distinct = len(set(str(x) for x in cur_vals)) == k
                if spread is None and not already_distinct:
                    coupled.append(h)
                    continue
                if spread is None:                       # already distinct -- keep order
                    target = sorted(cur_vals, key=lambda x: str(x))
                else:
                    target = list(spread)
                trial_rows = [list(r) for r in cur_rows]
                for j in range(k):
                    for g in group:
                        trial_rows[j][header.index(g)] = target[j]
                self._materialize(tab, header, trial_rows, types)
                trial_card = self._card(query)
                ok = trial_card >= 1 and (trial_card > k if track_card else True)
                if ok:
                    cur_rows = trial_rows
                    for g in group:
                        stored = self._read_col_sorted(tab, g)
                        if len(stored) == k and len(set(str(v) for v in stored)) == k:
                            disc_windows[g] = list(stored)
                else:
                    self._materialize(tab, header, cur_rows, types)  # revert
                    coupled.extend(group)

            # --- step 2: Q_H on the discriminated D_min, infer same-column preds ---
            out0 = self._q(query)
            if out0 and len(out0) >= 2:
                for col, sp, sq, op in infer_inter_alias_predicates(out0, disc_windows):
                    preds.append({'kind': 'inter', 'col': col, 'op': op, 'slots': (sp, sq)})
                # alias-lift the projection: which R_a<j>.col does each output column expose?
                self._attribute_output_columns(tab, out0, disc_windows)

            if not disc_windows:
                note = "no discriminable column kept Q_H FIT -- only intra-alias checks ran"
            elif coupled:
                note = ("coupled columns may carry a same-column cross-alias equi-join, an "
                        "equi-join to another table, or a tight constant filter")
            else:
                note = "ok"
            return preds, coupled, note
        finally:
            self._rollback()

    def _attribute_output_columns(self, tab, out0, disc_windows):
        """Merge ``tab``'s output-column attributions into ``self.output_attribution``,
        dropping any column that two relations disagree on."""
        for s, (ai, c) in attribute_output_columns(out0, disc_windows).items():
            if s in self._attr_conflicts:
                continue
            cand = (tab, ai, c)
            prev = self.output_attribution.get(s)
            if prev is None:
                self.output_attribution[s] = cand
            elif prev != cand:
                self.output_attribution.pop(s, None)
                self._attr_conflicts.add(s)

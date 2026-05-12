"""
Algorithm 4 -- per-alias filter extraction.

When a table ``T`` occurs ``k`` times in the hidden query, each alias may carry
its *own* filter on a column ``c``:

    SELECT ... FROM T t1, T t2 WHERE t1.x <= 10 AND t2.x <= 20

The legacy single-row filter extractor only ever recovers the *tightest* bound
(``<= 10`` here) because it walks one row.  This stage recovers the whole set of
per-alias bounds by a *cardinality-step search*.

Let ``A`` be the legacy D_min row of ``T``.  Q_H is FIT with ``T = {A}`` (it is
the D_min), so every alias accepts ``A`` -- i.e. ``A.c`` lies in the intersection
of all the per-alias intervals ``[l_a, u_a]``.  Probe with ``T`` set to two rows,
``A`` and ``B`` where ``B`` is ``A`` with column ``c`` set to ``V``:

    f(V) = |Q_H| on  T = {A, B(V)}        (bag semantics, SELECT *)
         = C * prod_{a=1..k} (1 + [V in [l_a, u_a]])

because each alias independently binds either ``A`` (always allowed) or ``B`` (iff
``V`` is inside that alias's interval).  ``f`` is a step function whose break
points, as ``V`` moves away from ``A.c``, are exactly the per-alias bounds (the
size of each step -- a factor of two per alias -- says how many aliases share
that bound).  We push ``V`` outward to find a "floor", then bisect each side.

What v1 does *not* do (noted in :attr:`notes`):

* **per-alias HAVING** (``HAVING l <= AGGR(t_i.x) <= u`` per alias) -- that
  composes with Xpose/Alaap's aggregate-predicate "diagram" machinery rather than
  this cardinality probe; left as future work.
* GROUP BY / DISTINCT queries that collapse the row count (the step then lives in
  an aggregate *value*, not in ``|Q_H|``); v1 then sees a flat count and reports
  nothing for that column.
* text / low-cardinality columns; columns that Algorithm 3 flagged as carrying a
  *cross-alias* predicate on ``c`` (the aliases are not independent there);
  per-alias bounds that lie further than ~2**HUGE_EXP from the witness value.

Like Algorithms 1-3 this stage probes inside a transaction that is rolled back, so
the live D_min is untouched; it is purely additive and gated behind the
``[feature] multi_instance`` flag.  Output is published on
``self.per_alias_filters`` for the (still to be made alias-aware) query assembler.
"""
import math
from datetime import date, timedelta

from .abstract.AppExtractorBase import AppExtractorBase
from ..util.constants import min_int_val, max_int_val, min_numeric_val, max_numeric_val

# Practical (safely-insertable) probe endpoints for date columns -- the postgres
# date domain runs 0001..9999 but a per-alias bound outside this window is not
# realistic and very old/new dates can trip roundtrip quirks.
_DATE_LO = date(1900, 1, 1)
_DATE_HI = date(2400, 1, 1)


def _is_int(dtype):
    d = str(dtype).lower()
    return ("int" in d or "serial" in d) and "interval" not in d


def _is_float(dtype):
    d = str(dtype).lower()
    return any(t in d for t in ("numeric", "double", "real", "decimal"))


def _is_date(dtype):
    d = str(dtype).lower()
    return ("date" in d or "timestamp" in d) and "interval" not in d


def _orderable(dtype):
    return _is_int(dtype) or _is_float(dtype) or _is_date(dtype)


# ---- generic value arithmetic over int / float / date -----------------------
def _add(v, delta_units, dtype):
    if _is_date(dtype):
        return v + timedelta(days=int(delta_units))
    if _is_int(dtype):
        return int(v) + int(delta_units)
    return v + delta_units


def _midpoint(a, b, dtype):
    if _is_date(dtype):
        return a + timedelta(days=(b - a).days // 2)
    if _is_int(dtype):
        return (int(a) + int(b)) // 2
    return (a + b) / 2.0


def _adjacent(a, b, dtype):
    if _is_date(dtype):
        return abs((b - a).days) <= 1
    if _is_int(dtype):
        return abs(int(b) - int(a)) <= 1
    return abs(b - a) <= max(1e-6, 1e-6 * max(abs(a), abs(b)))


def _alias_mult(big_card, small_card, k):
    """How many aliases share a break point whose cardinality dropped from
    ``big_card`` to ``small_card`` -- each alias contributes a factor of two.
    Clamped to ``[1, k]``."""
    try:
        ratio = float(big_card) / float(small_card)
    except (ZeroDivisionError, TypeError, ValueError):
        return 1
    if ratio <= 1:
        return 1
    return max(1, min(k, int(round(math.log2(ratio)))))


def find_step_breakpoints(probe, a, b, av, bv, dtype, budget):
    """Break points of the (weakly) monotone step function ``probe`` on ``[a, b]``.

    ``probe(v) -> int``; ``av = probe(a)``, ``bv = probe(b)``.  ``budget`` is a
    one-element list capping the number of probe calls.  Returns
    ``[(low_v, high_v, from_card, to_card), ...]`` ordered from ``a`` toward ``b``;
    each entry is an adjacent pair where the cardinality changes -- ``low_v`` is the
    last value with cardinality ``from_card`` (i.e. the *upper* bound of whatever
    interval was open to the left) and ``high_v`` is the first with ``to_card``
    (the *lower* bound of the interval open to the right).  If the call budget is
    exhausted while a step is still bracketed (but not yet pinned to adjacency) the
    coarse bracket ``[a, b]`` is returned rather than dropping the step entirely --
    a slightly imprecise bound is better than a missed one.
    """
    if av == bv:
        return []
    if _adjacent(a, b, dtype) or budget[0] <= 0:
        return [(a, b, av, bv)]
    m = _midpoint(a, b, dtype)
    if m == a or m == b:
        return [(a, b, av, bv)]
    budget[0] -= 1
    mv = probe(m)
    return (find_step_breakpoints(probe, a, m, av, mv, dtype, budget)
            + find_step_breakpoints(probe, m, b, mv, bv, dtype, budget))


def domain_endpoint(dtype, direction):
    """Far probe endpoint for the given side -- the column type's practical domain
    limit, so a probe never pushes a value out of range.  ``direction`` > 0 -> the
    high end, < 0 -> the low end.  (Module-level so the per-(alias,attribute) probe
    in :mod:`per_alias_pinned_filter` can reuse it.)"""
    d = str(dtype).lower()
    if _is_date(dtype):
        return _DATE_HI if direction > 0 else _DATE_LO
    if "smallint" in d or "int2" in d:
        return 32767 if direction > 0 else -32768
    if _is_int(dtype):
        return max_int_val if direction > 0 else min_int_val
    return max_numeric_val if direction > 0 else min_numeric_val


class PerAliasFilter(AppExtractorBase):
    """Recovers per-alias filter bounds for every multi-instance core relation.

    Public outputs after :meth:`doJob`:

    * ``per_alias_filters`` -- ``{table -> {column -> {'lower': [...], 'upper': [...],
      'lower_multiset': [...], 'upper_multiset': [...], 'tightest': (l, u),
      'loosest': (l, u)}}}``.  ``lower``/``upper`` are the *distinct* break points;
      ``*_multiset`` repeats each break point by the number of aliases that share it
      (read off the cardinality-jump size -- a ×2 per alias), so it has one entry per
      alias that has a finite bound on that side, tightest first.
    * ``notes`` -- per-table free-text remarks.
    """

    _BISECT_BUDGET = 30          # probe calls allowed per side per column

    def __init__(self, connectionHelper, core_relations, mult,
                 global_min_instance_dict, cross_alias_predicates=None, coupled_columns=None):
        super().__init__(connectionHelper, "PerAliasFilter")
        self.core_relations = list(dict.fromkeys(core_relations))
        self.mult = dict(mult or {})
        self.global_min_instance_dict = global_min_instance_dict or {}
        self.cross_alias_predicates = cross_alias_predicates or {}
        self.coupled_columns = coupled_columns or {}
        self.per_alias_filters = {}
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
                cols_info, note = self._analyse_one(query, tab, k)
            except Exception as e:
                self.logger.error(f"PerAliasFilter failed on {tab}: {e}")
                self._rollback()
                cols_info, note = {}, f"analysis raised: {e}"
            self.per_alias_filters[tab] = cols_info
            self.notes[tab] = note
            for c, info in cols_info.items():
                self.logger.info(f"per-alias filter on {tab}.{c}: "
                                 f"tightest={info.get('tightest')}, loosest={info.get('loosest')}")
        return self.per_alias_filters

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

    def _card(self, query):
        out = self._q(query)
        if not isinstance(out, list) or len(out) <= 1:
            return 0
        return len(out) - 1

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
        if _is_int(dtype) or _is_float(dtype):
            return str(val)
        return "'" + str(val).replace("'", "''") + "'"

    def _materialize_two(self, tab, header, row_a, row_b, types):
        cols = ", ".join(str(c) for c in header)
        va = ", ".join(self._lit(v, types.get(h, "")) for h, v in zip(header, row_a))
        vb = ", ".join(self._lit(v, types.get(h, "")) for h, v in zip(header, row_b))
        self._exec(f"truncate table {self._fq(tab)};",
                   f"insert into {self._fq(tab)} ({cols}) values ({va}), ({vb});")

    def _coupled_cols(self, tab):
        out = set(self.coupled_columns.get(tab, []))     # Algorithm-3 coupled columns (equi-join keys etc.)
        for p in self.cross_alias_predicates.get(tab, []):
            if p.get('kind') == 'inter':
                out.add(p.get('col'))
            if p.get('kind') == 'intra_eq':
                out.update(p.get('cols', ()))
        return out

    _domain_endpoint = staticmethod(domain_endpoint)

    # ----------------------------------------------------- the cardinality probe
    def _analyse_one(self, query, tab, k):
        legacy = self.global_min_instance_dict.get(tab)
        if not legacy or len(legacy) < 2:
            return {}, "no legacy D_min row available"
        header = list(legacy[0])
        row_a = list(legacy[1])
        col_types = self._column_types(tab)
        if not col_types:
            return {}, "could not read column metadata"
        types = {name: dt for name, dt in col_types}
        skip = self._coupled_cols(tab)

        self._begin()
        try:
            results = {}
            analysed_any = False
            for ci, h in enumerate(header):
                dt = types.get(h, "")
                if not _orderable(dt) or h in skip:
                    continue
                a_val = row_a[ci]
                if a_val is None:
                    continue

                cache = {}

                def probe(v, _ci=ci):
                    key = str(v)
                    if key in cache:
                        return cache[key]
                    row_b = list(row_a)
                    row_b[_ci] = v
                    self._materialize_two(tab, header, row_a, row_b, types)
                    c = self._card(query)
                    cache[key] = c
                    return c

                top = probe(a_val)         # T = {A, A} -> C * 2**k
                if top <= 0:
                    continue
                analysed_any = True

                upper_bps = self._bps_one_side(probe, a_val, dt, +1, top)
                lower_bps = self._bps_one_side(probe, a_val, dt, -1, top)
                if not upper_bps and not lower_bps:
                    continue

                # each break point's cardinality jump (a factor of 2 per alias that
                # shares that bound) gives the per-alias bound *multiset*.
                upper_ms, lower_ms = [], []
                for low_v, _high_v, fc, tc in upper_bps:        # bound is the upper endpoint
                    upper_ms += [low_v] * _alias_mult(fc, tc, k)
                for _low_v, high_v, fc, tc in lower_bps:        # bound is the lower endpoint
                    lower_ms += [high_v] * _alias_mult(tc, fc, k)
                upper_ms.sort(key=str)                           # ascending: tightest first
                lower_ms.sort(key=str, reverse=True)             # descending: tightest first
                upper_vals = sorted(set(upper_ms), key=str)
                lower_vals = sorted(set(lower_ms), key=str)
                results[h] = {
                    'lower': lower_vals,
                    'upper': upper_vals,
                    'lower_multiset': lower_ms,
                    'upper_multiset': upper_ms,
                    'tightest': (lower_ms[0] if lower_ms else None,
                                 upper_ms[0] if upper_ms else None),
                    'loosest': (lower_ms[-1] if lower_ms else None,
                                upper_ms[-1] if upper_ms else None),
                }
            if not analysed_any:
                note = "no orderable column kept Q_H FIT under the two-row probe"
            elif not results:
                note = ("no per-alias filter variation detected "
                        "(uniform bounds, or masked by GROUP BY/DISTINCT)")
            elif skip:
                note = "columns flagged by Algorithm 3 as cross-alias-coupled were skipped"
            else:
                note = "ok"
            return results, note
        finally:
            self._rollback()

    def _bps_one_side(self, probe, a_val, dtype, direction, top):
        """Break points of ``f`` on the ``direction`` side of ``a_val`` (``+1`` =
        increasing V => upper bounds; ``-1`` => lower bounds)."""
        far_v = self._domain_endpoint(dtype, direction)
        # don't probe the wrong side of the witness
        if (direction > 0 and not far_v > a_val) or (direction < 0 and not far_v < a_val):
            return []
        try:
            far_c = probe(far_v)
        except Exception:
            return []
        if far_c >= top:
            return []   # the whole way out is accepted -> no bound on this side
        if direction > 0:
            return find_step_breakpoints(probe, a_val, far_v, top, far_c, dtype,
                                         [self._BISECT_BUDGET])
        return find_step_breakpoints(probe, far_v, a_val, far_c, top, dtype,
                                     [self._BISECT_BUDGET])

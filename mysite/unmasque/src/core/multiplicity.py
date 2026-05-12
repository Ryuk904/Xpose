"""
Algorithm 1 -- MultiplicityDetect (+ FreshTupleProbe fallback).

Detects, for every physical relation already known to participate in the hidden
query Q_H, the *multiplicity* ``mult(T)`` -- i.e. how many times T occurs in the
``FROM`` clause under (possibly implicit) aliases.  The legacy UNMASQUE/Xpose
From-Clause extractor only reports the *set* of physical tables; it never reports
how many alias copies of a table are present.  This module fills that gap and is
the prerequisite for alias-aware minimization (Algorithm 2), cross-alias
predicate extraction (Algorithm 3) and per-alias filter extraction (Algorithm 4).

Theory
------
Cardinality-scaling fingerprint (Lemma B.1).  If T occurs ``k`` times under
aliases ``t_1, ..., t_k`` and ``k0 <= k`` of those aliases are not pinned to a
single key value of another relation, then replacing T's content with ``n``
copies of a fixed witness row makes the result cardinality grow as a *polynomial
of degree* ``k0`` in ``n`` (under bag semantics).  Equi-joins between the aliases
on the duplicated values keep every combination, so the count is ``c * n**k0``;
non-strict cross-alias inequalities keep it polynomial of the same degree.  We
recover ``k0`` by sampling ``|Q_H|`` at ``n = 1, 2, ..., kmax+2`` and reading off
the order of the leading non-zero finite difference -- this is robust to the
(unknown) leading coefficient and to the ``n^k`` vs ``C(n,k)`` distinction.  The
exponent is the black-box shadow of the provenance polynomial
(Green-Karvounarakis-Tannen 2007): the monomial degree of an output tuple's
annotation equals the alias multiplicity.

Fallback (FreshTupleProbe, section B.3).  When GROUP BY / DISTINCT / LIMIT
collapses the cardinality signal, insert one *fresh* witness row with sentinel
attribute values and count, over all output rows, the maximum number of output
cells that carry a sentinel; divide by ``|attrs(T)|``.  Each alias of T that the
fresh row binds to contributes its own slice of columns to that output row.

Inflation copies the *whole* current table content ``n`` times, not one row, so
a witness set that already contains the rows distinguishing the aliases (a
strictly increasing ``x_a < x_b < x_c`` for a 3-way chain, say) keeps the
cross-alias predicates satisfiable across copies and the count still scales as
``n^k``.  This is why running after (even a collapsed) minimization works: a
FIT D_min necessarily holds such a witness set.

Known weak boundary cases (section J.4):
* Idempotent self-joins whose canonical core folds to the diagonal and whose
  projection erases the redundancy are homomorphism-equivalent to a single scan
  (Chandra-Merlin 1977).  Under bag semantics ``SELECT * FROM T t1, T t2 WHERE
  t1.k = t2.k`` still scales (k=2 is reported, which is correct for that query);
  the truly indistinguishable variants are reported as ``mult = 1``.  We do not
  attempt the homomorphism-folding analysis that would prefer the smaller query.
* If the (pre-)minimized D_min that this stage runs on does *not* hold a complete
  witness set -- e.g. the legacy minimizer collapsed or failed on a hard self-join
  before this stage ran -- the result inherits that outcome (such tables land in
  :attr:`ambiguous` via the fresh-tuple fallback).

Operational notes
-----------------
* Designed to run *after* view minimization, when each core table holds a witness
  set on which Q_H is guaranteed to be FIT.  Only one table's content is changed
  at a time -- everything else stays at D_min -- which is exactly the per-table
  independent treatment required for mixed multi-instance schemas (section A.5).
* All perturbations happen inside an explicit transaction that is rolled back, so
  the database is left exactly as it was found even if a probe fails midway.
"""
import math

from .abstract.AppExtractorBase import AppExtractorBase
from ..util.constants import NON_TEXT_TYPES
from ..util.utils import get_dummy_val_for


# How far up the alias count we are willing to look (Assumption SJ-A1).  k <= 4
# covers every self-join pattern in TPC-H.
DEFAULT_MAX_MULT = 4

# Name of the throw-away copy of a table we use while probing it.
_BKP = "m_bkp"

# Cap on the witness snapshot we inflate.  The cardinality fingerprint only needs
# a snapshot that *exhibits* the self-join (some rows that join across aliases) --
# not the whole (possibly sampled) table.  Kept small so even the ``kmax``-way
# self-join of ``kmax+2`` inflated copies stays bounded -- and so the fresh-tuple
# fallback, which *fetches* the result rows, doesn't blow up the client.
_SNAPSHOT_CAP = 500

# Relative tolerance when deciding two cardinalities are "the same".
_EPS = 1e-9


def _almost_equal(a, b):
    return abs(a - b) <= _EPS * max(1.0, abs(a), abs(b))


def _finite_difference_degree(values, kmax):
    """Order of the leading non-zero finite difference of ``values``.

    ``values`` are ``f(1), f(2), ...`` for an (unknown) eventually-polynomial f.
    Returns the polynomial degree, clamped to ``[1, kmax]``; returns 0 if f is
    constant.  A degree-d polynomial has a constant (d)-th finite difference and
    a zero (d+1)-th one, so we walk the difference table until it flattens.
    """
    cur = list(map(float, values))
    if len(cur) >= 1 and all(_almost_equal(v, cur[0]) for v in cur):
        return 0
    d = 0
    while len(cur) >= 2 and d <= kmax + 1:
        nxt = [cur[i + 1] - cur[i] for i in range(len(cur) - 1)]
        d += 1
        if all(_almost_equal(v, 0.0) for v in nxt):
            return max(1, min(d - 1, kmax))      # f was degree d-1
        if all(_almost_equal(v, nxt[0]) for v in nxt):
            return max(1, min(d, kmax))          # constant non-zero d-th diff
        cur = nxt
    return max(1, min(d, kmax))


class MultiplicityDetect(AppExtractorBase):
    """Detects ``mult(T)`` for each core relation of the hidden query.

    Public outputs after :meth:`doJob`:

    * ``mult``        -- ``{table -> int}`` multiplicity of every core relation.
    * ``ambiguous``   -- set of tables whose multiplicity could not be pinned
                         down (boundary cases, or a failed probe).
    * ``method_used`` -- ``{table -> "scaling" | "fresh-tuple" | "trivial"}``.
    * ``cardinalities`` -- ``{table -> [c1, c2, ...]}`` raw probe results, for
                           diagnostics / experiment plots.
    """

    def __init__(self, connectionHelper, core_relations, max_mult=DEFAULT_MAX_MULT):
        super().__init__(connectionHelper, "MultiplicityDetect")
        self.core_relations = list(dict.fromkeys(core_relations))  # de-dup, keep order
        self.max_mult = max(2, int(max_mult))
        self.mult = {tab: 1 for tab in self.core_relations}
        self.ambiguous = set()
        self.method_used = {}
        self.cardinalities = {}

    # ------------------------------------------------------------------ API ---
    def extract_params_from_args(self, args):
        return args[0]  # (query, ...)

    def doActualJob(self, args=None):
        query = self.extract_params_from_args(args)
        # Q_H must run against the working (minimized) schema, not the pristine one.
        self.set_data_schema()
        # Lock in everything done by earlier stages so our own per-table
        # transactions can be rolled back without touching their work.
        try:
            self.connectionHelper.commit_transaction()
        except Exception as e:
            self.logger.debug(f"pre-probe commit: {e}")
        for tab in self.core_relations:
            try:
                k, how = self._detect_one(query, tab)
            except Exception as e:  # never let one table sink the whole stage
                self.logger.error(f"MultiplicityDetect failed on {tab}: {e}")
                self._rollback()
                k, how = 1, "trivial"
                self.ambiguous.add(tab)
            self.mult[tab] = k
            self.method_used[tab] = how
            self.logger.info(f"mult({tab}) = {k}  [{how}]")
        return self.mult

    # ------------------------------------------------------------- internals --
    def _fq(self, tab):
        return self.get_fully_qualified_table_name(tab)

    def _begin(self):
        self.connectionHelper.begin_transaction()

    def _rollback(self):
        try:
            self.connectionHelper.rollback_transaction()
        except Exception as e:
            self.logger.error(f"rollback failed: {e}")

    def _q_card(self, query):
        """Cardinality of Q_H's result on the current DB state (bag semantics).

        Wrapped in ``SELECT count(*) FROM (...)`` so the server aggregates it -- an
        inflated self-join can produce millions of rows, and fetching them all into
        the client (as ``self.app.doJob`` would) blows up memory."""
        q = query.strip().rstrip(";").strip()
        res = self.app.doJob(f"SELECT count(*) AS _n FROM ({q}) _sub;")
        if not isinstance(res, list) or len(res) <= 1:
            return 0          # UNFIT / empty / errored result; row 0 is the header
        try:
            return int(res[1][0])
        except (ValueError, TypeError, IndexError):
            return 0

    def _snapshot_into_temp(self, tab):
        self.connectionHelper.execute_sql(
            [f"drop table if exists pg_temp.{_BKP};",
             f"create temp table {_BKP} on commit drop as "
             f"select * from {self._fq(tab)} limit {int(_SNAPSHOT_CAP)};"],
            self.logger)

    def _inflate_from_temp(self, tab, n):
        """Replace T's content by ``n`` copies of the snapshot taken earlier.

        Done by ``DROP TABLE`` + ``CREATE TABLE AS`` rather than ``TRUNCATE`` +
        ``INSERT``: this stage runs *before* the view minimizer, so T may still
        carry its primary key / unique indexes (the minimizer strips them later via
        ``CREATE TABLE AS``), and ``n`` copies of every row would violate them.
        ``CREATE TABLE AS`` makes a plain table with no constraints/indexes, and the
        whole thing is inside the rolled-back probe transaction so the original T
        (PK and all) comes back on rollback."""
        fq = self._fq(tab)
        self.connectionHelper.execute_sql(
            [f"drop table if exists {fq} cascade;",
             f"create table {fq} as select b.* from {_BKP} b, generate_series(1, {int(n)}) g;"],
            self.logger)

    # --- the per-table decision -------------------------------------------
    def _detect_one(self, query, tab):
        if self._q_card(query) == 0:
            # Q_H is not FIT on the current DB for this table -- cannot probe.
            self.ambiguous.add(tab)
            return 1, "trivial"

        self._begin()
        try:
            self._snapshot_into_temp(tab)               # bounded witness snapshot

            # f(n) = |Q_H| when T holds n copies of the (capped) witness snapshot.
            # Sample n = 1 .. kmax + 2 so the (kmax+1)-th finite difference is
            # available; f(1) is taken on the *sample* too (not the full table) so
            # the series is internally consistent.
            cards = []
            for n in range(1, self.max_mult + 3):
                self._inflate_from_temp(tab, n)
                cards.append(self._q_card(query))
            self.cardinalities[tab] = list(cards)
            self.logger.debug(f"{tab}: cardinality probes (n=1..) {cards}")

            base_card = cards[0]
            if base_card == 0 or any(c == 0 for c in cards):
                # The capped sample is not FIT, or a probe collapsed the result (a
                # strict inequality / LIMIT / aggregate broke under duplication).
                # Fall back to slot counting.
                return self._fresh_tuple_probe(query, tab), "fresh-tuple"

            if all(_almost_equal(c, base_card) for c in cards):
                # Plateau: mult == 1, or a self-join hidden by GROUP BY/DISTINCT.
                k_fresh = self._fresh_tuple_probe(query, tab)
                return (k_fresh, "fresh-tuple") if k_fresh > 1 else (1, "scaling")

            if not all(cards[i] < cards[i + 1] for i in range(len(cards) - 1)):
                # Non-monotone: a LIMIT / partial DISTINCT / aggregate caps the row
                # count, so the polynomial fit is unreliable.  Slot-count.
                k_fresh = self._fresh_tuple_probe(query, tab)
                return (k_fresh, "fresh-tuple") if k_fresh > 1 else (1, "scaling")

            return max(1, _finite_difference_degree(cards, self.max_mult)), "scaling"
        finally:
            self._rollback()  # undoes the temp table and the recreated table

    # --- Algorithm 1b: FreshTupleProbe (section B.3) -----------------------
    def _fresh_tuple_probe(self, query, tab):
        """Insert one fresh row of T with sentinel values; ``mult(T)`` is the
        max number of output cells that carry a sentinel, over all output rows,
        divided by ``|attrs(T)|``.

        Must be called from within :meth:`_detect_one` (it relies on the temp
        snapshot ``m_bkp`` existing and on the caller's ``finally`` rolling the
        transaction back).  Resets T to its single-witness content first so the
        only sentinel-bearing rows in the output come from the fresh tuple.
        """
        cols = self._column_types(tab)
        if not cols:
            self.ambiguous.add(tab)
            return 1
        sentinels = {name: self._sentinel_for(dtype) for name, dtype in cols}
        col_list = ", ".join(name for name, _ in cols)
        val_list = ", ".join(self._sql_literal(sentinels[name], dtype) for name, dtype in cols)
        self._inflate_from_temp(tab, 1)  # back to the D_min witness content
        self.connectionHelper.execute_sql(
            [f"insert into {self._fq(tab)} ({col_list}) values ({val_list});"], self.logger)
        res = self.app.doJob(query)
        if not isinstance(res, list) or len(res) <= 1:
            return 1
        sentinel_strs = {str(v) for v in sentinels.values()}
        max_slots = 0
        for row in res[1:]:
            max_slots = max(max_slots, sum(1 for cell in row if str(cell) in sentinel_strs))
        if max_slots <= 0:
            self.ambiguous.add(tab)  # fresh row was filtered out (boundary B4)
            return 1
        return max(1, min(self.max_mult, math.ceil(max_slots / max(1, len(cols)))))

    # --- small helpers -----------------------------------------------------
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
    def _is_numeric_or_date(dtype):
        d = str(dtype).lower()
        if any(t in d for t in NON_TEXT_TYPES):
            return True
        return any(t in d for t in ("int", "numeric", "double", "real", "decimal", "serial"))

    def _sentinel_for(self, dtype):
        d = str(dtype).lower()
        if "date" in d or "time" in d:
            return get_dummy_val_for("date")
        if self._is_numeric_or_date(dtype):
            return 1987654321 if "int" in d else 1987654.321
        return "ZZ_unmasque_sentinel"

    def _sql_literal(self, val, dtype):
        d = str(dtype).lower()
        if "date" in d or "time" in d:
            return f"'{val}'"
        if self._is_numeric_or_date(dtype):
            return str(val)
        return "'" + str(val).replace("'", "''") + "'"

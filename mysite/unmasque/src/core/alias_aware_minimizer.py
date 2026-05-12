"""
Algorithm 2 -- alias-aware k-coloured halving minimizer.

The legacy view minimizer reduces every core relation to a single row.  That is
correct only when every table occurs once in the hidden query; for a k-way
self-join it produces an "anomalous" D_min that hides the self-join (a single row
that is a fixpoint of the join, e.g. one row satisfying ``t1.x = t2.x``).
Downstream predicate extractors then cannot recover the second alias.

This module computes, for every relation ``R`` with ``mult(R) = k >= 2`` (as
detected by Algorithm 1 / :class:`MultiplicityDetect`), an *alias-aware* D_min:
a set of ``k`` rows of ``R`` on which the hidden query is still FIT and that keeps
the ``k`` aliases distinguishable.  It does so with a colour-aware, FIT-guided
halving (the report's "k-coloured halving"):

1. start from the legacy single-row D_min of ``R`` (guaranteed FIT) *plus* a
   bounded sample of ``R``'s original content -- a pool of candidate rows for the
   remaining aliases;
2. binary-halve the pool, always keeping a half that (a) still makes Q_H FIT and
   (b) has at least ``k`` rows -- the multiplicity lower bound is never crossed
   (Lemmas C.1 / C.2);
3. partition what remains into ``k`` contiguous "colours" and greedily drop rows
   colour by colour, never letting the total fall below ``k`` -- so the result is
   one (representative) row per colour rather than ``k`` copies of one row
   (Lemma C.3 / Corollary C.4).

Only the table being worked on is touched; every other core table stays at its
legacy single-row D_min, which is exactly the right semantics for minimizing one
relation in isolation.  And it all happens inside a transaction that is rolled
back, so the *live* D_min that the (not-yet-alias-aware) downstream extractors
consume is left exactly as it was.  The alias-aware D_min is published separately,
on ``alias_aware_min_instance_dict``, for Algorithms 3 & 4 (cross-alias predicate
extraction, per-alias filters) to use once they are implemented.  Until then this
stage is purely additive and is gated behind the ``[feature] multi_instance`` flag.
"""
from .abstract.AppExtractorBase import AppExtractorBase


# How many original rows (beyond the legacy D_min) to pool as alias candidates.
_INITIAL_POOL = 64
_MAX_POOL = 1 << 13            # 8192 -- plenty for the SF<=1 TPC-H tables

# Temp tables used while probing one relation (dropped on rollback anyway).
_DMIN_BKP = "m_aam_dmin"
_CAND = "m_aam_cand"


def split_into_k_blocks(items, k):
    """Partition a list into ``k`` contiguous blocks (as evenly as possible).
    If ``len(items) < k`` the trailing blocks are empty."""
    n = len(items)
    k = max(1, int(k))
    base, rem = divmod(n, k)
    blocks, i = [], 0
    for b in range(k):
        size = base + (1 if b < rem else 0)
        blocks.append(items[i:i + size])
        i += size
    return blocks


def _flatten(blocks):
    out = []
    for b in blocks:
        out.extend(b)
    return out


def kcolour_halve(content, k, fit_fn):
    """The core of Algorithm 2, factored out for testability.

    ``content`` is the current list of row identifiers (any hashable, e.g. row
    numbers); ``k`` is the multiplicity lower bound; ``fit_fn(list) -> bool`` says
    whether the hidden query is FIT when the table holds exactly those rows.

    Returns a reduced list of length ``>= k`` (ideally exactly ``k``) on which
    ``fit_fn`` is True, never crossing the ``k``-row floor.  Assumes
    ``fit_fn(content)`` is already True and ``len(content) >= k``.
    """
    content = list(content)
    # 1. binary halving -- only ever keep a half that is FIT and has >= k rows.
    while len(content) > 2 * k:
        mid = len(content) // 2
        lower, upper = content[mid:], content[:mid]
        if len(lower) >= k and fit_fn(lower):
            content = lower
        elif len(upper) >= k and fit_fn(upper):
            content = upper
        else:
            break
    # 2. colour-partitioned greedy per-row removal, never below k.
    blocks = split_into_k_blocks(content, k)
    changed = True
    while changed and len(_flatten(blocks)) > k:
        changed = False
        for bi in range(len(blocks)):
            for r in list(blocks[bi]):
                if len(_flatten(blocks)) <= k:
                    break
                trial = [list(b) for b in blocks]
                trial[bi].remove(r)
                if fit_fn(_flatten(trial)):
                    blocks = trial
                    changed = True
    final = _flatten(blocks)
    # 3. if still above k (Q_H genuinely needs more rows -- e.g. a strict chain),
    #    try to trim to exactly k while staying FIT, otherwise keep what we have.
    if len(final) > k:
        trimmed = final[:k]
        if fit_fn(trimmed):
            final = trimmed
    return final


class AliasAwareMinimizer(AppExtractorBase):
    """Builds the alias-aware D_min for every multi-instance core relation.

    Public outputs after :meth:`doJob`:

    * ``alias_aware_min_instance_dict`` -- ``{table -> [header_tuple, row, ...]}``
      with (ideally) ``mult(table)`` data rows for multi-instance tables, and the
      legacy single row for the rest (copied from ``global_min_instance_dict``).
    * ``expanded`` -- tables for which a genuine multi-row witness set was found.
    * ``fallback`` -- tables for which we could not synthesise one and fell back
      to ``k`` copies of the legacy single row.
    """

    def __init__(self, connectionHelper, core_relations, mult, global_min_instance_dict):
        super().__init__(connectionHelper, "AliasAwareMinimizer")
        self.core_relations = list(dict.fromkeys(core_relations))
        self.mult = dict(mult or {})
        self.global_min_instance_dict = global_min_instance_dict or {}
        self.alias_aware_min_instance_dict = {}
        self.expanded = set()
        self.fallback = set()

    # ------------------------------------------------------------------ API ---
    def extract_params_from_args(self, args):
        return args[0]  # (query, ...)

    def doActualJob(self, args=None):
        query = self.extract_params_from_args(args)
        self.set_data_schema()
        try:
            self.connectionHelper.commit_transaction()   # lock in earlier stages' work
        except Exception as e:
            self.logger.debug(f"pre-probe commit: {e}")

        for tab in self.core_relations:
            k = max(1, int(self.mult.get(tab, 1)))
            legacy = self.global_min_instance_dict.get(tab)
            if k <= 1:
                self.alias_aware_min_instance_dict[tab] = list(legacy) if legacy else None
                continue
            if legacy and len(legacy) - 1 >= k:
                # the (floored) minimizer already left a k-row witness set -- use it.
                self.alias_aware_min_instance_dict[tab] = list(legacy)
                self.expanded.add(tab)
                self.logger.info(f"alias-aware D_min[{tab}]: {len(legacy) - 1} rows from the floored minimizer")
                continue
            try:
                rows = self._kcolour_witness(query, tab, k)
            except Exception as e:
                self.logger.error(f"AliasAwareMinimizer failed on {tab}: {e}")
                self._rollback()
                rows = None
            if rows is None or len(rows) < 1 + k:
                rows = self._k_copies_fallback(tab, k)
                self.fallback.add(tab)
            else:
                self.expanded.add(tab)
            self.alias_aware_min_instance_dict[tab] = rows
            self.logger.info(f"alias-aware D_min[{tab}]: {max(0, len(rows) - 1) if rows else 0} rows "
                             f"(target {k}, {'fallback' if tab in self.fallback else 'witnessed'})")
        return self.alias_aware_min_instance_dict

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

    def _orig(self, tab):
        return self.get_original_table_name(tab)

    def _exec(self, *sqls):
        self.connectionHelper.execute_sql(list(sqls), self.logger)

    def _fetchone0(self, sql):
        return self.connectionHelper.execute_sql_fetchone_0(sql, self.logger)

    def _fit(self, query):
        res = self.app.doJob(query)
        if not isinstance(res, list):
            return False
        return self.app.isQ_result_nonEmpty_nullfree(res)

    def _col_list(self, tab):
        legacy = self.global_min_instance_dict.get(tab)
        if legacy and legacy[0]:
            return ", ".join(str(c) for c in legacy[0])
        return "*"

    def _read_table(self, tab):
        rows, desc = self.connectionHelper.execute_sql_fetchall(f"select * from {self._fq(tab)};")
        header = tuple(d[0] for d in desc) if desc else tuple()
        return [header] + [tuple(r) for r in (rows or [])]

    def _k_copies_fallback(self, tab, k):
        legacy = self.global_min_instance_dict.get(tab)
        if not legacy or len(legacy) < 2:
            return legacy
        header, row = legacy[0], legacy[1]
        return [header] + [row for _ in range(k)]

    # ------------------------------------------------- candidate-pool plumbing --
    def _materialize_pool(self, tab, n_extra):
        """Working table := legacy D_min rows  ++  first ``n_extra`` original rows."""
        self._exec(f"truncate table {self._fq(tab)};",
                   f"insert into {self._fq(tab)} select * from pg_temp.{_DMIN_BKP};",
                   f"insert into {self._fq(tab)} select * from {self._orig(tab)} "
                   f"order by ctid limit {int(n_extra)};")

    def _freeze_candidates(self, tab):
        """Snapshot the working table into a numbered candidate temp table; return
        the candidate count."""
        self._exec(f"drop table if exists pg_temp.{_CAND};",
                   f"create temp table {_CAND} on commit drop as "
                   f"select row_number() over () as _rn, t.* from {self._fq(tab)} t;")
        c = self._fetchone0(f"select count(*) from pg_temp.{_CAND};")
        return int(c) if c is not None else 0

    def _materialize_rns(self, tab, rns):
        """Working table := the candidate rows with the given row-numbers."""
        cols = self._col_list(tab)
        self._exec(f"truncate table {self._fq(tab)};")
        if not rns:
            return
        arr = ", ".join(str(int(r)) for r in rns)
        self._exec(f"insert into {self._fq(tab)} ({cols}) "
                   f"select {cols} from pg_temp.{_CAND} where _rn = any(array[{arr}]);")

    def _fit_with_rns(self, query, tab, rns):
        self._materialize_rns(tab, rns)
        return self._fit(query)

    # ----------------------------------------------- the k-coloured halving ---
    def _kcolour_witness(self, query, tab, k):
        legacy = self.global_min_instance_dict.get(tab)
        if not legacy or len(legacy) < 2:
            return None
        self._begin()
        try:
            # Snapshot the legacy D_min so we can rebuild the candidate pool around it.
            self._exec(f"drop table if exists pg_temp.{_DMIN_BKP};",
                       f"create temp table {_DMIN_BKP} on commit drop as select * from {self._fq(tab)};")

            # Grow the candidate pool (D_min ++ original prefix) until Q_H is FIT
            # *and* there are at least k candidate rows.
            n_extra = _INITIAL_POOL
            cand_n = 0
            while True:
                self._materialize_pool(tab, n_extra)
                if self._fit(query):
                    cand_n = self._freeze_candidates(tab)
                    if cand_n >= k:
                        break
                if n_extra >= _MAX_POOL:
                    break
                n_extra *= 4

            if cand_n < k:
                # Fall back: the legacy D_min alone must still be FIT; if even that
                # broke we cannot do anything sensible here.
                self._exec(f"truncate table {self._fq(tab)};",
                           f"insert into {self._fq(tab)} select * from pg_temp.{_DMIN_BKP};")
                if not self._fit(query):
                    return None
                return None  # -> k-copies fallback in the caller

            # k-coloured halving over candidate row-numbers 1..cand_n.
            fit_fn = lambda rns: self._fit_with_rns(query, tab, rns)
            final = kcolour_halve(list(range(1, cand_n + 1)), k, fit_fn)
            if len(final) < k or not fit_fn(final):
                return None

            self._materialize_rns(tab, final)
            return self._read_table(tab)
        finally:
            self._rollback()

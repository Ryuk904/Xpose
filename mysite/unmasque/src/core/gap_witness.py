"""Gap-aware disjunction extraction v2: NEP-style Re-Rh witness finder.

Reuses the NEP machinery (Comparator's EXCEPT-ALL diff + ctid bisection) to
find a witness row whose A-value lies in a gap inside Filter's extracted
envelope.

Why this works where a projection-based set-diff doesn't:
- Bisection narrows over BASE-TABLE ctids, not Qh's result rows. Once we
  bisect down to a single row, we read the witness's A-value directly from
  the base table. Qh's projection does NOT need to include A.

DB state contract:
- On setup(), the working-schema copies of every table in `from_clause_tables`
  are renamed aside (`<tab>_unmasque_GapWitness_bkp`) and replaced with full-D
  clones from user_schema. This lets Qh run against full D for the duration
  of v2.
- On teardown(), the cloned tables are dropped and the D-min backups are
  renamed back. Filter's D-min state is preserved across the call.
- This module never mutates user_schema.

Single-table is the well-tested path. For multi-table from clauses, all
joined tables are restored to full D for the diff to be meaningful; callers
should snapshot D-min via `global_min_instance_dict` before invocation in
case anything else needs to reset state.
"""

from typing import List, Optional, Tuple

from ..util.utils import get_format


_BKP_SUFFIX = "_unmasque_GapWitness_bkp"
_RE_VIEW = "gap_witness_r_e"
_RH_TABLE = "gap_witness_r_h"


class GapWitnessFinder:
    """One instance per (tab, attr, envelope) refinement attempt.

    Lifecycle:
        finder = GapWitnessFinder(...)
        if not finder.setup(qh): return  # full-D unavailable -> fall back
        try:
            while ...:
                v = finder.find_witness_value(intervals_so_far)
                if v is None: break
                # bisect outward, update intervals_so_far
        finally:
            finder.teardown()
    """

    def __init__(self, connectionHelper, logger, app,
                 tab, attr, datatype, from_clause_tables):
        self.connectionHelper = connectionHelper
        self.logger = logger
        self.app = app
        self.schema = connectionHelper.config.schema
        self.user_schema = connectionHelper.config.user_schema
        self.tab = tab
        self.attr = attr
        self.datatype = datatype
        self.from_clause_tables = list(from_clause_tables) if from_clause_tables else [tab]
        self.qh = None
        self._setup_done = False
        # Set of tables we successfully swapped, for teardown.
        self._swapped: List[str] = []
        # Qh's projection columns, captured from one trial run. Required so
        # that Qe and Qh produce comparable result rows for EXCEPT ALL.
        # If the projection contains an expression (not a bare column name)
        # we can't safely splice it into Qe — setup returns False in that
        # case and the caller falls back to v1.
        self._qh_cols: List[str] = []

    # ------------------------------------------------------------------ setup

    def setup(self, qh: str) -> bool:
        self.qh = qh
        # Swap each from-clause table: rename D-min copy aside, clone full D
        # from user_schema into working schema with the original name.
        for t in self.from_clause_tables:
            ok = self._swap_to_full_d(t)
            if not ok:
                for done in list(self._swapped):
                    self._swap_back(done)
                self._swapped.clear()
                return False
            self._swapped.append(t)
        # Capture Qh's projection columns. Bisection works on base-table
        # rows regardless, but the diff (Re EXCEPT ALL Rh) needs Re's schema
        # to match Rh's, so Re must project the same columns Qh does.
        if not self._capture_qh_cols():
            for done in list(self._swapped):
                self._swap_back(done)
            self._swapped.clear()
            return False
        self._setup_done = True
        return True

    def _capture_qh_cols(self) -> bool:
        """Run Qh once against full D, read result header. Validate that each
        entry is a bare column name (Re will re-project these from the FROM
        clause). Bail if Qh's projection contains expressions/aggregates we
        can't safely splice — caller falls back to v1."""
        try:
            res = self.app.doJob(self.qh)
        except Exception as e:
            self.logger.error(f"GapWitnessFinder: Qh trial run failed: {e}")
            return False
        if not res or len(res) < 1:
            return False
        header = res[0]
        if not header:
            return False
        if isinstance(header, str):
            cols = [header]
        else:
            try:
                cols = [str(c) for c in header]
            except TypeError:
                cols = [str(header)]
        for c in cols:
            if not c or any(ch in c for ch in "() ,*"):
                self.logger.debug(
                    f"GapWitnessFinder: Qh projection contains non-bare-column {c!r}, "
                    f"falling back to v1")
                return False
        self._qh_cols = cols
        return True

    def teardown(self):
        # Drop comparator artefacts in case of mid-loop exception.
        try:
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.drop_view(self._fq(_RE_VIEW)),
                 self.connectionHelper.queries.drop_table_cascade(self._fq(_RH_TABLE))],
                self.logger)
        except Exception as e:
            self.logger.debug(f"GapWitnessFinder.teardown comparator cleanup: {e}")
        # Restore each swapped table to its D-min state.
        for t in self._swapped:
            self._swap_back(t)
        self._swapped.clear()
        self._setup_done = False

    # --------------------------------------------------------- public witness

    def find_witness_value(self, intervals_so_far: List[Tuple]) -> Optional[object]:
        """Returns an A-value satisfying the current envelope (intervals) but
        rejected by Qh, or None if no such value exists in D."""
        if not self._setup_done:
            return None
        qe = self._build_qe(intervals_so_far)
        if qe is None:
            return None
        if not self._diff_nonempty(qe):
            return None
        witness_ctid = self._bisect_witness_ctid(intervals_so_far)
        if witness_ctid is None:
            return None
        return self._read_attr_at_ctid(witness_ctid)

    # ----------------------------------------------------------- swap helpers

    def _fq(self, t: str) -> str:
        return f"{self.schema}.{t}"

    def _bkp_name(self, t: str) -> str:
        return f"{t}{_BKP_SUFFIX}"

    def _swap_to_full_d(self, t: str) -> bool:
        fq_t = self._fq(t)
        fq_bkp = self._fq(self._bkp_name(t))
        us_t = f"{self.user_schema}.{t}"
        try:
            # Rename the D-min copy aside.
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.drop_table_cascade(fq_bkp),
                 self.connectionHelper.queries.alter_table_rename_to(fq_t, self._bkp_name(t))],
                self.logger)
            # Clone full D into working schema.
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.create_table_like(fq_t, us_t),
                 self.connectionHelper.queries.insert_into_tab_select_star_fromtab(fq_t, us_t)],
                self.logger)
            return True
        except Exception as e:
            self.logger.error(f"GapWitnessFinder._swap_to_full_d({t}): {e}")
            return False

    def _swap_back(self, t: str):
        fq_t = self._fq(t)
        fq_bkp = self._fq(self._bkp_name(t))
        try:
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.drop_table_cascade(fq_t),
                 self.connectionHelper.queries.alter_table_rename_to(fq_bkp, t)],
                self.logger)
        except Exception as e:
            self.logger.error(f"GapWitnessFinder._swap_back({t}): {e}")

    # ------------------------------------------------------ Qe + diff helpers

    def _build_qe(self, intervals_so_far: List[Tuple]) -> Optional[str]:
        """SELECT <qh_cols> FROM <from_clause> WHERE <attr> IN <intervals>.

        We use Qh's projection (not SELECT *) so that the EXCEPT ALL diff
        between r_e and r_h is meaningful — r_h is created LIKE r_e and
        Qh's results are inserted into it, so they must share schema.
        """
        if not intervals_so_far or not self._qh_cols:
            return None
        from_parts = [self._fq(t) + " AS " + t for t in self.from_clause_tables]
        from_clause = ", ".join(from_parts)
        or_clauses = []
        for (lb, ub) in intervals_so_far:
            try:
                f_lb = get_format(self.datatype, lb)
                f_ub = get_format(self.datatype, ub)
            except Exception:
                return None
            or_clauses.append(f"{self.tab}.{self.attr} BETWEEN {f_lb} AND {f_ub}")
        where = " OR ".join(or_clauses)
        if len(or_clauses) > 1:
            where = f"({where})"
        select_list = ", ".join(self._qh_cols)
        return f"SELECT {select_list} FROM {from_clause} WHERE {where}"

    def _diff_nonempty(self, qe: str) -> bool:
        """True iff `Re EXCEPT ALL Rh` has at least one row.

        We avoid materialising Rh row-by-row (which costs minutes per Qh
        execution on large tables) by computing the diff inline:

            SELECT count(*) FROM (
                (<Qe>) EXCEPT ALL (<Qh>)
            ) AS T

        This works as long as Qh is a single SELECT statement (with or
        without a trailing semicolon). Postgres handles the EXCEPT ALL
        natively. Qe is built by us so it's always a single SELECT.
        """
        qh = (self.qh or "").rstrip().rstrip(";").strip()
        if not qh:
            return False
        diff_sql = (f"SELECT count(*) FROM ("
                    f"({qe}) EXCEPT ALL ({qh})"
                    f") AS T;")
        try:
            count = self.connectionHelper.execute_sql_fetchone_0(diff_sql, self.logger)
        except Exception as e:
            self.logger.error(f"GapWitnessFinder: inline diff failed: {e}")
            return False
        return bool(count and count > 0)

    # ------------------------------------------------------ ctid bisection

    def _bisect_witness_ctid(self, intervals_so_far) -> Optional[str]:
        """Bisect working_schema.<tab> by ctid until a single witness row
        remains. We do this by repeatedly slicing <tab> in place: rename
        aside, recreate as the chosen half, drop the aside copy. Mirrors
        NepMinimizer.reduce_Database_Instance but with our diff test.
        """
        fq_tab = self._fq(self.tab)

        for _ in range(64):  # log2(any reasonable D) is well under 64
            count = self._row_count(fq_tab)
            if count is None or count <= 1:
                break

            min_c, max_c = self._ctid_bounds(fq_tab)
            if min_c is None:
                break
            mid1, mid2 = self._mid_ctids(fq_tab, count)
            if mid1 is None:
                break

            # Try lower half first.
            if self._half_has_witness(fq_tab, min_c, mid1, intervals_so_far):
                self._restrict_table_to_range(fq_tab, min_c, mid1)
                continue
            if self._half_has_witness(fq_tab, mid2, max_c, intervals_so_far):
                self._restrict_table_to_range(fq_tab, mid2, max_c)
                continue
            # Witness straddles the cut; stop here, return whatever ctid we
            # can locate within the current span.
            break

        # Take any remaining row's ctid. If multiple, the first lexicographically
        # is acceptable since the caller only needs one A-value as a seed.
        try:
            res, _ = self.connectionHelper.execute_sql_fetchall(
                f"SELECT ctid FROM {fq_tab} ORDER BY ctid LIMIT 1;", self.logger)
            if res:
                return str(res[0][0])
        except Exception:
            return None
        return None

    def _row_count(self, fq_tab: str) -> Optional[int]:
        try:
            return self.connectionHelper.execute_sql_fetchone_0(
                self.connectionHelper.queries.get_row_count(fq_tab), self.logger)
        except Exception:
            return None

    def _ctid_bounds(self, fq_tab: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            lo = self.connectionHelper.execute_sql_fetchone_0(
                self.connectionHelper.queries.get_ctid_from("min", fq_tab), self.logger)
            hi = self.connectionHelper.execute_sql_fetchone_0(
                self.connectionHelper.queries.get_ctid_from("max", fq_tab), self.logger)
        except Exception:
            return None, None
        return (str(lo), str(hi)) if lo and hi else (None, None)

    def _mid_ctids(self, fq_tab: str, count: int) -> Tuple[Optional[str], Optional[str]]:
        if count < 2:
            return None, None
        offset = (count // 2) - 1
        if offset < 0:
            offset = 0
        try:
            res, _ = self.connectionHelper.execute_sql_fetchall(
                f"SELECT ctid FROM {fq_tab} ORDER BY ctid OFFSET {offset} LIMIT 2;",
                self.logger)
        except Exception:
            return None, None
        if not res or len(res) < 2:
            return None, None
        return str(res[0][0]), str(res[1][0])

    def _half_has_witness(self, fq_tab: str, start_c: str, end_c: str,
                          intervals_so_far) -> bool:
        """Run the diff against <tab> restricted to ctid in [start_c, end_c]
        without modifying <tab>. Uses a temporary view that shadows <tab>.

        We can't shadow via a same-named view (already a table), so instead
        we materialise the half as a temp table swap: rename <tab> aside,
        create <tab> as the slice, run diff, swap back.
        """
        # Snapshot current <tab>: rename aside, create slice as <tab>, diff,
        # then drop slice and rename original back. This is cheaper than
        # ctid-rewriting Qe because Qh references <tab> by name.
        bkp = self._fq(self.tab + "_gw_slice_bkp")
        try:
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.drop_table_cascade(bkp),
                 self.connectionHelper.queries.alter_table_rename_to(
                     fq_tab, self.tab + "_gw_slice_bkp"),
                 f"CREATE TABLE {fq_tab} AS SELECT * FROM {bkp} "
                 f"WHERE ctid >= '{start_c}' AND ctid <= '{end_c}';"],
                self.logger)
        except Exception as e:
            self.logger.error(f"GapWitnessFinder._half_has_witness slice setup: {e}")
            return False
        qe = self._build_qe(intervals_so_far)
        result = self._diff_nonempty(qe) if qe else False
        try:
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.drop_table_cascade(fq_tab),
                 self.connectionHelper.queries.alter_table_rename_to(
                     bkp, self.tab)],
                self.logger)
        except Exception as e:
            self.logger.error(f"GapWitnessFinder._half_has_witness slice teardown: {e}")
        return result

    def _restrict_table_to_range(self, fq_tab: str, start_c: str, end_c: str):
        bkp = self._fq(self.tab + "_gw_restrict_bkp")
        try:
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.drop_table_cascade(bkp),
                 self.connectionHelper.queries.alter_table_rename_to(
                     fq_tab, self.tab + "_gw_restrict_bkp"),
                 f"CREATE TABLE {fq_tab} AS SELECT * FROM {bkp} "
                 f"WHERE ctid >= '{start_c}' AND ctid <= '{end_c}';",
                 self.connectionHelper.queries.drop_table_cascade(bkp)],
                self.logger)
        except Exception as e:
            self.logger.error(f"GapWitnessFinder._restrict_table_to_range: {e}")

    def _read_attr_at_ctid(self, ctid: str):
        fq_tab = self._fq(self.tab)
        try:
            return self.connectionHelper.execute_sql_fetchone_0(
                f"SELECT {self.attr} FROM {fq_tab} WHERE ctid = '{ctid}';",
                self.logger)
        except Exception:
            return None

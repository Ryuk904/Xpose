"""Pre-Filter self-join detection for k=1 tables.

When the hidden query Qh is a pure-equality self-join (e.g.
`SELECT l1.l_orderkey FROM lineitem l1, lineitem l2 WHERE
l1.l_orderkey = l2.l_orderkey`), the view minimizer halves the table
to 1 row: a single row joined to itself keeps Qh non-empty, so the
Pop-oracle minimizer has no signal to keep two rows. The downstream
alias-aware Filter / EquiJoin pipeline never sees a multi-instance
table and emits a single-instance query that is bag-inequivalent on
the full DB.

This probe runs between ViewMinimizer and Filter. For each table at
min_card=1 it:
  1. Counts |Qh| on the current D1 (baseline B_orig).
  2. INSERTs a duplicate of the single row, counts |Qh| again (B_dup).
  3. If B_dup / B_orig is in the m^2 band (≈ 4 for m=2, k=2), the
     query is a self-join with two aliases; promote the table to k=2
     and record the duplicate row's ctid for alias dict.
  4. Identifies which columns Qh actually references via a schema-
     level rename probe (ALTER TABLE … RENAME COLUMN col TO col__cp;
     run Qh; if it errors, col is referenced; ROLLBACK undoes the
     rename). Then per Qh-referenced column, runs a mutation probe
     on alias2's value: if mutating drops |Qh| significantly, the
     column is a join key; otherwise it's a SELECT/projection column
     and gets no seed predicate.
  5. For each join column emits a pair of constant-equality seed
     predicates `(alias_i, X, '=', K, K)` keyed on the witness value
     K. The pipeline appends these to filter_predicates after Filter
     runs; EquiJoin then groups them as a self-equi-join via the
     existing Phase 5 detection.

For k=1 tables that aren't self-joins, the duplicate INSERT is
reverted (DELETE by ctid) and the table stays at k=1 with no state
changes.
"""

from typing import Dict, List, Set, Tuple

from ..util.instance import Instance, make_alias
from ..util.utils import get_format, get_unused_dummy_val
from ..core.abstract.un2_where_clause import UN2WhereClause
from .row_probe import RowProbe


# Ratio band for B_dup / B_orig. m=2, k=2 → expected 4. Tight enough to
# reject single-table (ratio ≈ 2), wide enough to absorb minor variance.
RATIO_MIN = 3.5
RATIO_MAX = 4.5

# Required drop fraction for the mutation probe to call a column a join key.
# |Qh| after mutation < B_dup * (1 - JOIN_DROP_THRESHOLD) → join key.
JOIN_DROP_THRESHOLD = 0.1


class CardinalityProbe(UN2WhereClause):

    def __init__(self, connectionHelper, core_relations: List[str],
                 global_min_instance_dict: dict,
                 global_alias_row_dict: dict,
                 instances: List[Instance],
                 alias_to_table: Dict[str, str],
                 min_card: Dict[str, int]):
        if instances is None:
            instances = [Instance(table=t, alias=t) for t in core_relations]
        if alias_to_table is None:
            alias_to_table = {t: t for t in core_relations}
        super().__init__(connectionHelper, core_relations, global_min_instance_dict,
                         "CardinalityProbe",
                         global_alias_row_dict=global_alias_row_dict,
                         instances=instances, alias_to_table=alias_to_table)
        # The base class deep-copies the dicts. Re-bind to the caller's
        # references so promotions made here are visible to downstream
        # stages (Filter/EquiJoin/AOA are constructed after this runs
        # and read from the pipeline's state).
        self.global_min_instance_dict = global_min_instance_dict
        self.global_alias_row_dict = global_alias_row_dict
        self.instances = instances
        self.alias_to_table = alias_to_table
        self.min_card = min_card
        self.seed_filter_predicates: List[Tuple] = []
        self.promoted_tables: List[str] = []
        # Per-table Qh-referenced columns (from the rename probe). Surfaced for
        # downstream EquiJoin so it can pick a marker column when disambiguating
        # swap-symmetric cross-alias join edges on cross-column self-joins.
        self.qh_cols_by_table: Dict[str, Set[str]] = {}
        self._attrib_types: Dict[Tuple[str, str], str] = {}
        # Enabler S2: shared duplicate-by-ctid / delete-by-ctid / count helper.
        self._row_probe = RowProbe(self.connectionHelper, self.app, self.logger)

    def do_init(self):
        # Pull column types from information_schema so get_datatype works
        # without depending on Filter having run first.
        for tab in self.core_relations:
            res, _ = self.connectionHelper.execute_sql_fetchall(
                self.connectionHelper.queries.get_column_details_for_table(
                    self.connectionHelper.config.schema, tab))
            for row in res:
                col = row[0].lower()
                typ = row[1].lower()
                self._attrib_types[(tab, col)] = typ
                # Mirror into the base class's attrib_types_dict so get_datatype works
                self.attrib_types_dict[(tab, col)] = typ

    def doActualJob(self, args=None):
        query = super().doActualJob(args)
        self.do_init()
        for tab in list(self.core_relations):
            if int(self.min_card.get(tab, 1)) != 1:
                continue
            try:
                self._probe_table(tab, query)
            except Exception as e:
                self.logger.error(f"CardinalityProbe: unexpected error on {tab}: {e}")
                # Best-effort cleanup so the rest of the pipeline runs
                try:
                    self.connectionHelper.rollback_transaction()
                except Exception:
                    pass
        return True

    # ---------- core flow ----------

    def _probe_table(self, tab: str, query: str) -> None:
        fqn = self.get_fully_qualified_table_name(tab)
        b_orig = self._count_qh(query)
        if b_orig <= 0:
            self.logger.debug(f"CardinalityProbe: {tab} B_orig={b_orig}, skipping")
            return

        new_ctids = self._insert_duplicate(fqn)
        if not new_ctids:
            return
        # Commit so the rename probe's BEGIN/ROLLBACK below doesn't roll
        # back our duplicate INSERT along with the ALTER.
        try:
            self.connectionHelper.commit_transaction()
        except Exception as e:
            self.logger.debug(f"CardinalityProbe: commit after INSERT failed for {tab}: {e}")

        b_dup = self._count_qh(query)
        ratio = b_dup / b_orig if b_orig > 0 else 0
        self.logger.info(
            f"CardinalityProbe: {tab} B_orig={b_orig} B_dup={b_dup} ratio={ratio:.2f}"
        )
        if not (RATIO_MIN <= ratio <= RATIO_MAX):
            # Not a 2-alias self-join; revert
            self._delete_rows_at_ctids(fqn, new_ctids)
            try:
                self.connectionHelper.commit_transaction()
            except Exception:
                pass
            return

        # Self-join confirmed; promote the table to k=2
        self._promote_to_k2(tab, new_ctids[0])

        # Rename probe: identify columns Qh references on this table
        qh_cols = self._run_rename_probe(tab, query)
        self.qh_cols_by_table[tab] = set(qh_cols)
        self.logger.info(f"CardinalityProbe: {tab} qh_cols={sorted(qh_cols)}")

        # Mutation probe per Qh-referenced col: distinguish JOIN keys
        # from SELECT/projection columns. Only JOIN keys get seed
        # predicates so EquiJoin can group them as a self-equi-join.
        a1 = make_alias(tab, 1)
        a2 = make_alias(tab, 2)
        a1_entry = self.global_alias_row_dict[a1]
        cols = a1_entry["cols"]
        join_keys: List[str] = []
        for col in qh_cols:
            if self._mutation_probe_is_join_key(a2, col, query, b_dup):
                join_keys.append(col)
        self.logger.info(f"CardinalityProbe: {tab} join_keys={join_keys}")

        for col in join_keys:
            if col not in cols:
                continue
            k = a1_entry["row"][cols.index(col)]
            self.seed_filter_predicates.append((a1, col, '=', k, k))
            self.seed_filter_predicates.append((a2, col, '=', k, k))

        self.promoted_tables.append(tab)

    # ---------- DB helpers ----------

    def _count_qh(self, query: str) -> int:
        try:
            res = self.app.doJob(query)
        except Exception:
            return 0
        if not self.app.done or not isinstance(res, list):
            return 0
        # Executable.doActualJob returns [header_tuple, row1, row2, ...]
        return max(0, len(res) - 1)

    def _insert_duplicate(self, fqn: str) -> List[str]:
        # Enabler S2: duplicate every current row of `fqn` (self-join case).
        return self._row_probe.duplicate_rows(fqn)

    def _delete_rows_at_ctids(self, fqn: str, ctids: List[str]) -> None:
        # Enabler S2: revert the duplicate by ctid.
        self._row_probe.delete_rows(fqn, ctids)

    # ---------- promotion: rewire alias / instance state in place ----------

    def _promote_to_k2(self, tab: str, dup_ctid: str) -> None:
        a1 = make_alias(tab, 1)
        a2 = make_alias(tab, 2)

        # min_card
        self.min_card[tab] = 2

        # global_min_instance_dict: append a second row (same content as row1)
        values = self.global_min_instance_dict.get(tab)
        if values is not None and len(values) >= 2:
            values.append(values[1])

        # global_alias_row_dict: rename existing 'tab' -> 'tab__a1', add 'tab__a2'
        if tab in self.global_alias_row_dict:
            entry = self.global_alias_row_dict.pop(tab)
            self.global_alias_row_dict[a1] = entry
        # else: nothing to rename; build a1 entry from min_instance_dict
        if a1 not in self.global_alias_row_dict and values is not None:
            cols = values[0]
            row = values[1]
            # Try to find a1's ctid from the table
            try:
                res, _ = self.connectionHelper.execute_sql_fetchall(
                    self.connectionHelper.queries.select_ctid_star_from(
                        self.get_fully_qualified_table_name(tab)))
                ctid = str(res[0][0]) if res else None
            except Exception:
                ctid = None
            self.global_alias_row_dict[a1] = {
                "table": tab,
                "cols": tuple(cols),
                "row": tuple(row),
                "ctid": ctid,
            }

        a1_entry = self.global_alias_row_dict[a1]
        self.global_alias_row_dict[a2] = {
            "table": tab,
            "cols": a1_entry["cols"],
            "row": a1_entry["row"],
            "ctid": dup_ctid,
        }

        # instances: drop any Instance for this table, add a1 + a2
        keep = [inst for inst in self.instances if inst.table != tab]
        self.instances.clear()
        self.instances.extend(keep)
        self.instances.append(Instance(table=tab, alias=a1))
        self.instances.append(Instance(table=tab, alias=a2))

        # alias_to_table
        if tab in self.alias_to_table:
            del self.alias_to_table[tab]
        self.alias_to_table[a1] = tab
        self.alias_to_table[a2] = tab

        self.logger.info(
            f"CardinalityProbe: promoted {tab} to k=2 "
            f"(aliases {a1}, {a2}; dup_ctid={dup_ctid})"
        )

    # ---------- probe A: rename ----------

    def _run_rename_probe(self, tab: str, query: str) -> Set[str]:
        cols = self.global_alias_row_dict[make_alias(tab, 1)]["cols"]
        referenced: Set[str] = set()
        for col in cols:
            if self._column_referenced_in_qh(tab, col, query):
                referenced.add(col)
        return referenced

    def _column_referenced_in_qh(self, tab: str, col: str, query: str) -> bool:
        """Rename `tab.col` and run Qh; if Qh errors, the column is
        referenced by Qh. ROLLBACK undoes the rename automatically.
        Mirrors the pattern from from_clause.get_core_relations_by_error."""
        fqn = self.get_fully_qualified_table_name(tab)
        rename_to = f"{col}__cp"
        is_referenced = False
        try:
            self.connectionHelper.begin_transaction()
            self.connectionHelper.execute_sql(
                [f"ALTER TABLE {fqn} RENAME COLUMN {col} TO {rename_to};"],
                self.logger,
            )
            result = self.app.doJob(query)
            if not self.app.done:
                is_referenced = True
            elif isinstance(result, str):
                rl = result.lower()
                if "does not exist" in rl or col.lower() in rl:
                    is_referenced = True
        except Exception as e:
            self.logger.debug(f"RenameProbe: error for {tab}.{col}: {e}")
            # Conservative: don't flag — Qh may have legitimately referenced
            # something else and we can't tell from the exception alone.
        finally:
            try:
                self.connectionHelper.rollback_transaction()
            except Exception:
                pass
        return is_referenced

    # ---------- probe B: per-column mutation (JOIN vs SELECT) ----------

    def _mutation_probe_is_join_key(self, alias: str, col: str,
                                    query: str, b_dup: int) -> bool:
        entry = self.global_alias_row_dict.get(alias)
        if entry is None:
            return False
        cols = entry["cols"]
        if col not in cols:
            return False
        idx = cols.index(col)
        original_val = entry["row"][idx]
        base_tab = self._to_base(alias)
        try:
            datatype = self.get_datatype((base_tab, col))
        except Exception:
            return False
        try:
            dummy = get_unused_dummy_val(datatype, [original_val])
        except Exception:
            return False
        formatted_dummy = get_format(datatype, dummy)
        is_date = datatype == 'date'

        used = self._exec_alias_ctid_update(
            alias, col, formatted_dummy, is_date=is_date, raw_val=dummy
        )
        if not used:
            return False

        try:
            b_mut = self._count_qh(query)
        finally:
            # Always restore so subsequent probes see a clean alias2.
            formatted_orig = get_format(datatype, original_val)
            self._exec_alias_ctid_update(
                alias, col, formatted_orig, is_date=is_date, raw_val=original_val
            )

        threshold = b_dup * (1 - JOIN_DROP_THRESHOLD)
        return b_mut < threshold

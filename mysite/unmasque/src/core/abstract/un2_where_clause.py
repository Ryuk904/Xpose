import copy
from typing import Tuple

from ...util.constants import NON_TEXT_TYPES, TEXT_TYPES, NUMERIC_TYPES, INT_TYPES
from ....src.core.abstract.MutationPipeLineBase import MutationPipeLineBase
from ....src.util.aoa_utils import get_tab, get_attrib, get_constants_for
from ....src.util.utils import get_format, get_min_and_max_val
from ....src.util.error_handling import UnmasqueError
from ....src.util.error_codes import ERROR_005


class UN2WhereClause(MutationPipeLineBase):
    SUPPORTED_DATATYPES = NON_TEXT_TYPES
    TEXT_EQUALITY_OP = 'equal'
    MATH_EQUALITY_OP = '='
    init_done = False

    def __init__(self, connectionHelper,
                 core_relations,
                 global_min_instance_dict, name,
                 global_alias_row_dict=None,
                 instances=None,
                 alias_to_table=None):
        super().__init__(connectionHelper, core_relations, global_min_instance_dict, name,
                         global_alias_row_dict=global_alias_row_dict,
                         instances=instances, alias_to_table=alias_to_table)
        # init data
        self.global_attrib_types = []
        self.global_all_attribs = {}
        self.global_d_plus_value = {}  # this is the tuple from D_min
        self.global_attrib_max_length = {}
        self.attrib_types_dict = {}
        self.global_min_instance_dict_bkp = copy.deepcopy(self.global_min_instance_dict)
        # Phase 3: backup of alias-row dict so mutations can be reverted per-alias.
        self.global_alias_row_dict_bkp = copy.deepcopy(self.global_alias_row_dict) \
            if self.global_alias_row_dict else None
        self.constants_dict = {}

    def do_init(self):
        pass

    def doActualJob(self, args=None):
        query = self.extract_params_from_args(args)
        self.mock = self.mock
        self.init_constants()
        return query

    def init_constants(self) -> None:
        for datatype in self.SUPPORTED_DATATYPES:
            i_min, i_max = get_min_and_max_val(datatype)
            delta, _ = get_constants_for(datatype)
            self.constants_dict[datatype] = (i_min, i_max, delta)

    def mutate_global_min_instance_dict(self, tab: str, attrib: str, val) -> None:
        g_min_dict = self.global_min_instance_dict
        data = g_min_dict[tab]
        idx = data[0].index(attrib)
        new_data = []
        for i in range(0, len(data[1])):
            if idx == i:
                new_data.append(val)
            else:
                new_data.append(data[1][i])
        data[1] = tuple(new_data)

    def mutate_global_alias_row_dict(self, alias: str, attrib: str, val) -> None:
        """Phase 3: update the in-memory alias-row dict to reflect a ctid-scoped
        DB mutation. Safe to call only when global_alias_row_dict is populated."""
        if not self.global_alias_row_dict or alias not in self.global_alias_row_dict:
            return
        entry = self.global_alias_row_dict[alias]
        cols = entry["cols"]
        if attrib not in cols:
            return
        idx = cols.index(attrib)
        row = list(entry["row"])
        row[idx] = val
        entry["row"] = tuple(row)

    def updateTab_alias_attrib_with_value(self, alias: str, attrib: str, val,
                                          quoted: bool = False, is_date: bool = False) -> bool:
        """Phase 3: emit a ctid-scoped UPDATE so a single alias's witness row
        on D¹ can be mutated without touching the sibling alias's row.

        Returns True iff the alias-scoped path ran; False if caller must fall
        back to the whole-table UPDATE.
        """
        return self._exec_alias_ctid_update(alias, attrib, val, quoted=quoted, is_date=is_date)

    def _exec_alias_ctid_update(self, alias: str, attrib: str, val,
                                quoted: bool = False, is_date: bool = False,
                                raw_val=None) -> bool:
        """Centralised ctid-scoped UPDATE + ctid refresh + alias dict mutate.
        Postgres MVCC re-versions the row on every UPDATE, so the new ctid is
        captured via RETURNING and persisted back into the alias dict — without
        this, every subsequent UPDATE silently affects zero rows.

        `val` is the value to splice into the SQL template (may already be
        SQL-formatted, e.g. with surrounding quotes for str/date). `raw_val`
        is the Python value to cache in the alias-row dict; defaults to `val`
        when the caller hasn't pre-formatted. Separating these prevents stray
        quotes (`'N'`) from leaking back through prev_values into a follow-up
        revert and producing malformed SQL (`''N''`)."""
        if not self.global_alias_row_dict or alias not in self.global_alias_row_dict:
            return False
        entry = self.global_alias_row_dict[alias]
        ctid = entry.get("ctid")
        if ctid is None:
            return False
        tab = entry["table"]
        fqn = self.get_fully_qualified_table_name(tab)
        if is_date:
            sql = self.connectionHelper.queries.update_tab_date_attrib_value_at_ctid(fqn, attrib, val, ctid)
        elif quoted:
            sql = self.connectionHelper.queries.update_tab_attrib_with_quoted_value_at_ctid(
                fqn, attrib, val, ctid)
        else:
            sql = self.connectionHelper.queries.update_tab_attrib_with_value_at_ctid(fqn, attrib, val, ctid)
        try:
            new_ctid = self.connectionHelper.execute_sql_fetchone_0(sql, self.logger)
        except Exception as e:
            self.logger.debug(f"alias ctid UPDATE failed for {alias}.{attrib}: {e}")
            return False
        if new_ctid is None:
            # WHERE ctid='...' matched zero rows. The cached ctid is stale —
            # most likely a concurrent path moved the row. Re-anchor by reading
            # the row that still carries the alias's last-known surrogate values.
            self.logger.debug(
                f"alias ctid UPDATE on {alias}.{attrib} matched 0 rows at {ctid}; row likely moved"
            )
            return False
        entry["ctid"] = str(new_ctid)
        self.mutate_global_alias_row_dict(alias, attrib, raw_val if raw_val is not None else val)
        return True

    def restore_d_min_from_dict(self) -> None:
        self.global_min_instance_dict = copy.deepcopy(self.global_min_instance_dict_bkp)
        # Phase 3 (Bug #3): also restore the per-alias witness rows so
        # multi-instance D¹ comes back with k rows, not 1. The single-row
        # insert below would otherwise drop the sibling alias's witness.
        if self.global_alias_row_dict_bkp is not None:
            self.global_alias_row_dict = copy.deepcopy(self.global_alias_row_dict_bkp)
        if not len(self.global_min_instance_dict):
            return
        for tab in self.core_relations:
            self.insert_into_dmin_dict_values(tab)

    def insert_into_dmin_dict_values(self, tabname):
        values = self.global_min_instance_dict[tabname]
        attribs = values[0]
        attrib_list = ", ".join(attribs)
        fqn = self.get_fully_qualified_table_name(tabname)
        self.connectionHelper.execute_sql([self.connectionHelper.queries.truncate_table(fqn)])

        # Phase 3 (Bug #3): on multi-instance D¹, insert one row per alias
        # from the alias-row dict instead of just values[1]. Sort aliases for
        # deterministic INSERT order so the post-insert ctid re-anchor below
        # matches alias i to ctid i.
        aliases_for_tab = []
        if self.global_alias_row_dict:
            aliases_for_tab = sorted(
                a for a, e in self.global_alias_row_dict.items() if e["table"] == tabname
            )
        if len(aliases_for_tab) > 1:
            rows = [self.global_alias_row_dict[a]["row"] for a in aliases_for_tab]
        else:
            rows = [values[1]]

        self.connectionHelper.execute_sql_with_params(
            self.connectionHelper.queries.insert_into_tab_attribs_format(f"({attrib_list})", "", fqn),
            rows)

        # TRUNCATE+INSERT produces fresh ctids; re-anchor the alias dict so the
        # next ctid-scoped UPDATE doesn't target a stale ctid and silently miss.
        if aliases_for_tab:
            res, _ = self.connectionHelper.execute_sql_fetchall(
                self.connectionHelper.queries.select_ctid_star_from(fqn))
            for alias, r in zip(aliases_for_tab, res):
                self.global_alias_row_dict[alias]["ctid"] = str(r[0])

    def get_datatype(self, tab_attrib: Tuple[str, str]) -> str:
        # Phase 4: tab_attrib[0] may be either a base table name or a synthetic
        # alias. attrib_types_dict is keyed by base table — resolve before lookup.
        key = (self._to_base(tab_attrib[0]), tab_attrib[1])
        if any(x in self.attrib_types_dict[key] for x in INT_TYPES):
            return 'int'
        elif 'date' in self.attrib_types_dict[key]:
            return 'date'
        elif any(x in self.attrib_types_dict[key] for x in TEXT_TYPES):
            return 'str'
        elif any(x in self.attrib_types_dict[key] for x in NUMERIC_TYPES):
            return 'numeric'
        else:
            raise UnmasqueError(ERROR_005, "un2_where_clause", f"Problem occured in Table {tab_attrib[0]}, attribute {tab_attrib[1]}, datatype {self.attrib_types_dict[key]}")

    def get_dmin_val_of_attrib_list(self, attrib_list: list) -> list:
        val_list = []
        for tab_attrib in attrib_list:
            tabname, attrib = tab_attrib[0], tab_attrib[1]
            val = self.get_dmin_val(attrib, tabname)
            val_list.append(val)
        return val_list

    def mutate_dmin_with_val(self, datatype, t_a, val):
        # Phase 4: when get_tab(t_a) is an alias and we have a per-alias witness
        # row, scope the UPDATE to that row's ctid (with RETURNING-based ctid
        # refresh) so sibling aliases on the same base table are left untouched.
        identifier = get_tab(t_a)
        attrib = get_attrib(t_a)
        base_tab = self._to_base(identifier)
        formatted = get_format(datatype, val)
        used_alias = self._exec_alias_ctid_update(identifier, attrib, formatted,
                                                  is_date=(datatype == 'date'),
                                                  raw_val=val)
        if not used_alias:
            if datatype == 'date':
                self.connectionHelper.execute_sql(
                    [self.connectionHelper.queries.update_sql_query_tab_date_attrib_value(
                        self.get_fully_qualified_table_name(base_tab), attrib, formatted)], self.logger)
            else:
                self.connectionHelper.execute_sql(
                    [self.connectionHelper.queries.update_tab_attrib_with_value(
                        self.get_fully_qualified_table_name(base_tab), attrib, formatted)], self.logger)
        self.mutate_global_min_instance_dict(base_tab, attrib, val)
        self.global_d_plus_value[attrib] = val
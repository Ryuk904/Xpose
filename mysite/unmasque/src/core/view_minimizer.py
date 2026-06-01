import pandas as pd

from .abstract.MinimizerBase import Minimizer
from ..util.error_handling import UnmasqueError
from ..util.error_codes import ERROR_002, ERROR_001


def extract_start_and_end_page(logger, rctid):
    min_ctid = rctid[0]
    min_ctid2 = min_ctid.split(",")
    start_page = int(min_ctid2[0][1:])
    max_ctid = rctid[1]
    logger.debug(max_ctid)
    max_ctid2 = max_ctid.split(",")
    end_page = int(max_ctid2[0][1:])
    start_ctid = min_ctid
    end_ctid = max_ctid
    return end_ctid, end_page, start_ctid, start_page


class ViewMinimizer(Minimizer):
    max_row_no = 1

    def __init__(self, connectionHelper,
                 core_relations, all_sizes,
                 sampling_status):
        super().__init__(connectionHelper, core_relations, all_sizes, "View_Minimizer")
        self.cs2_passed = sampling_status
        self.global_min_instance_dict = {}
        # Phase 1: per-table minimum cardinality that keeps Qh non-empty.
        # min_card[T] > 1 is the signal that T is referenced multiple times
        # in Qh's FROM (a self-join with a distinguishing predicate).
        self.min_card = {}
        # Phase 3: alias-keyed witness rows with ctid bookkeeping.
        # Shape: {alias: {"cols": (...), "row": (...), "ctid": "(p,r)"}}.
        # For single-instance tables (alias == table) this mirrors
        # global_min_instance_dict; for multi-instance tables it lets
        # downstream stages mutate the row backing a specific alias.
        self.global_alias_row_dict = {}

    def doActualJob(self, args=None):
        query = self.extract_params_from_args(args)
        if not self.sanity_check(query):
            self.logger.error(" Original database is not giving populated result!")
            raise UnmasqueError(ERROR_001, "view_minimizer", "")
        return self.reduce_Database_Instance(query, True) if self.cs2_passed \
            else self.reduce_Database_Instance(query, False)

    def do_interPage_viewBased_binary_halving(self, core_sizes,
                                              query,
                                              tabname,
                                              rctid,
                                              dirty_tab):
        end_ctid, end_page, start_ctid, start_page = extract_start_and_end_page(self.logger, rctid)
        floor_reached = False
        while start_page < end_page - 1:
            mid_page = int((start_page + end_page) / 2)
            mid_ctid1 = "(" + str(mid_page) + ",1)"
            mid_ctid2 = "(" + str(mid_page) + ",2)"

            nend_ctid, nstart_ctid = self.create_view_execute_app_drop_view(end_ctid,
                                                                            mid_ctid1, mid_ctid2, query,
                                                                            start_ctid, tabname, dirty_tab)
            if nend_ctid is None:
                # Phase 1: floor reached at inter-page granularity — neither
                # half of the page range preserves Pop. Stop halving.
                floor_reached = True
                break
            else:
                start_ctid = nstart_ctid
                end_ctid = nend_ctid
            start_ctid2 = start_ctid.split(",")
            start_page = int(start_ctid2[0][1:])
            end_ctid2 = end_ctid.split(",")
            end_page = int(end_ctid2[0][1:])

        core_sizes = self.update_with_remaining_size(core_sizes, end_ctid, start_ctid, tabname, dirty_tab)
        if floor_reached:
            self.logger.info(f"Inter-page halving floor for {tabname} at size {core_sizes[tabname]}")
        return core_sizes

    def reduce_Database_Instance(self, query, cs_pass):
        core_sizes = self.getCoreSizes()
        sorted_relations = self.__get_tables_sorted_by_size(core_sizes)

        for tabname in sorted_relations:
            view_name = self._get_dirty_name(tabname) if cs_pass \
                else self.connectionHelper.queries.get_restore_name(tabname)
            self.connectionHelper.execute_sql([self.connectionHelper.queries.alter_table_rename_to(
                self.get_fully_qualified_table_name(tabname), view_name)])
            rctid = self.connectionHelper.execute_sql_fetchone(
                self.connectionHelper.queries.get_min_max_ctid(self.get_fully_qualified_table_name(view_name)))
            core_sizes = self.do_interPage_viewBased_binary_halving(core_sizes, query, tabname, rctid, view_name)
            core_sizes = self.do_intraPage_copyBased_binary_halving(core_sizes, query, tabname,
                                                                    self._get_dirty_name(tabname))

            # Phase 1: record the floor. min_card[T] > 1 indicates T is referenced
            # multiple times in Qh's FROM with at least one distinguishing predicate.
            self.min_card[tabname] = int(core_sizes[tabname])
            if self.min_card[tabname] > self.max_row_no:
                self.logger.info(
                    f"Multi-instance signal: {tabname} requires {self.min_card[tabname]} "
                    f"rows to keep Qh non-empty (likely self-join)."
                )

            if not self.sanity_check(query):
                raise UnmasqueError(ERROR_002, "view_minimizer", f"Problem occured in Table {tabname}, having size {core_sizes[tabname]}.")

        for tabname in self.core_relations:
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.drop_table_cascade(
                    self.connectionHelper.queries.get_dmin_tabname(tabname)),
                 self.connectionHelper.queries.create_table_as_select_star_from(
                     self.connectionHelper.queries.get_dmin_tabname(tabname), tabname)])

        self.populate_dict_info()
        return True

    def __get_tables_sorted_by_size(self, core_sizes):
        sort_by_size = sorted(core_sizes, key=lambda x: core_sizes[x], reverse=True)
        sorted_relations = []
        for tab in sort_by_size:
            if tab in self.core_relations:
                sorted_relations.append(tab)
        return sorted_relations

    def reduce_Database_Instance_kapil(self, query, cs_pass):
        core_sizes = self.getCoreSizes()

        for tabname in self.core_relations:
            view_name = self._get_dirty_name(tabname) if cs_pass \
                else self.connectionHelper.queries.get_restore_name(tabname)
            core_sizes = self.do_intraPage_copyBased_binary_halving(core_sizes, query, tabname, view_name)

            if not self.sanity_check(query):
                return False

        for tabname in self.core_relations:
            self.connectionHelper.execute_sql(
                [self.connectionHelper.queries.drop_table_cascade(self.connectionHelper.queries.get_dmin_tabname(tabname)),
                 self.connectionHelper.queries.create_table_as_select_star_from(
                     self.connectionHelper.queries.get_dmin_tabname(tabname), tabname)])

        self.populate_dict_info()
        return True

    def do_intraPage_copyBased_binary_halving(self, core_sizes, query, tabname, dirty_tab):
        while int(core_sizes[tabname]) > self.max_row_no:
            pre_size = int(core_sizes[tabname])
            end_ctid, start_ctid = self.get_start_and_end_ctids(core_sizes, query, tabname, dirty_tab)
            if end_ctid is None or start_ctid is None:
                # Phase 1: floor reached — neither half of the remaining rows
                # preserves Pop. The current size is the minimum cardinality
                # this table needs for Qh to return a non-empty result, which
                # is the signal for self-join / multi-instance usage.
                # get_start_and_end_ctids leaves the table renamed to dirty_tab
                # (the working copy) when it returns None; the downstream
                # sanity_check / dmin creation assumes `tabname` exists, so
                # rebuild `tabname` from dirty_tab at the floor size and drop
                # the dirty copy. This preserves the floor rows while restoring
                # the normal post-halving state.
                self.logger.info(f"Intra-page halving floor for {tabname} at size {pre_size}")
                fqn_tab = self.get_fully_qualified_table_name(tabname)
                fqn_dirty = self.get_fully_qualified_table_name(dirty_tab)
                self.connectionHelper.execute_sql([
                    self.connectionHelper.queries.create_table_as_select_star_from(fqn_tab, fqn_dirty),
                    self.connectionHelper.queries.drop_table_cascade(fqn_dirty),
                ], self.logger)
                core_sizes[tabname] = self.connectionHelper.execute_sql_fetchone_0(
                    self.connectionHelper.queries.get_row_count(fqn_tab), self.logger)
                break
            core_sizes = self.update_with_remaining_size(core_sizes, end_ctid, start_ctid, tabname, dirty_tab)
            if int(core_sizes[tabname]) >= pre_size:
                # No progress on this iteration; bail out to avoid an infinite loop.
                self.logger.info(f"Intra-page halving stalled for {tabname} at size {pre_size}")
                break
        return core_sizes

    def populate_dict_info(self):
        # POPULATE MIN INSTANCE DICT
        # Phase 3: also build alias-keyed witness rows. ctids are pulled in
        # the same query so the i-th row maps to the i-th alias deterministically.
        from ..util.instance import make_alias

        for tabname in self.core_relations:
            self.global_min_instance_dict[tabname] = []
            sql_query = pd.read_sql_query(
                self.connectionHelper.queries.select_ctid_star_from(
                    self.get_fully_qualified_table_name(tabname)),
                self.connectionHelper.conn)
            df = pd.DataFrame(sql_query)
            all_cols = tuple(df.columns)
            # The first column is the ctid (from select_ctid_star_from);
            # downstream consumers of global_min_instance_dict don't expect it,
            # so we strip it for the legacy dict and keep it separately for the
            # alias-keyed dict.
            if len(all_cols) and all_cols[0] == "ctid":
                data_cols = all_cols[1:]
            else:
                data_cols = all_cols
            self.global_min_instance_dict[tabname].append(tuple(data_cols))

            rows = []
            ctids = []
            for _, row in df.iterrows():
                if all_cols and all_cols[0] == "ctid":
                    ctids.append(str(row[0]))
                    rows.append(tuple(row[1:]))
                else:
                    ctids.append(None)
                    rows.append(tuple(row))
            for r in rows:
                self.global_min_instance_dict[tabname].append(r)

            k = int(self.min_card.get(tabname, 1))
            for i, (r, ctid) in enumerate(zip(rows, ctids), start=1):
                alias = tabname if k == 1 else make_alias(tabname, i)
                self.global_alias_row_dict[alias] = {
                    "table": tabname,
                    "cols": tuple(data_cols),
                    "row": r,
                    "ctid": ctid,
                }
                # Once we've consumed k rows (matching min_card), stop. If the
                # table on disk has more rows than min_card (it shouldn't, since
                # halving stops at the floor), we ignore the extras.
                if i >= max(k, 1):
                    break
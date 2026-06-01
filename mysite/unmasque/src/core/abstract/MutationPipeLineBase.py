import copy
from _decimal import Decimal
from abc import ABC

from .AppExtractorBase import AppExtractorBase
from ....src.core.abstract.abstractConnection import AbstractConnectionHelper
from typing import List


class MutationPipeLineBase(AppExtractorBase, ABC):

    def __init__(self, connectionHelper: AbstractConnectionHelper,
                 core_relations: List[str],
                 global_min_instance_dict: dict,
                 name: str,
                 global_alias_row_dict: dict = None,
                 instances=None,
                 alias_to_table: dict = None):
        super().__init__(connectionHelper, name)
        # from from clause
        self.core_relations = core_relations
        # from view minimizer
        self.global_min_instance_dict = copy.deepcopy(global_min_instance_dict)
        # Phase 3: per-alias witness row + ctid. None when the upstream did not
        # populate it (backwards-compatible default).
        self.global_alias_row_dict = copy.deepcopy(global_alias_row_dict) if global_alias_row_dict else None
        # Phase 4: alias-aware data model. For single-instance tables alias == table,
        # so these are populated even on legacy single-instance pipelines.
        self.instances = list(instances) if instances else None
        self.alias_to_table = dict(alias_to_table) if alias_to_table else None
        self.mock = False

    def _to_base(self, name: str) -> str:
        """Phase 4: resolve an identifier that may be an alias or a base table
        to its base table name. Safe to call when alias_to_table is None."""
        if self.alias_to_table and name in self.alias_to_table:
            return self.alias_to_table[name]
        return name

    def see_d_min(self):
        self.logger.debug("======================")
        for tab in self.core_relations:
            self.see_d_min_tab(tab)
        self.logger.debug("======================")

    def see_d_min_tab(self, tab):
        res, des = self.connectionHelper.execute_sql_fetchall(self.connectionHelper.queries.get_star(
                                                self.get_fully_qualified_table_name(tab)))
        self.logger.debug(f"-----  {tab} ------")
        self.logger.debug(res)

    def restore_d_min(self):
        for tab in self.core_relations:
            self.connectionHelper.execute_sql([
                self.connectionHelper.queries.truncate_table(self.get_fully_qualified_table_name(tab)),
                self.connectionHelper.queries.insert_into_tab_select_star_fromtab(
                    self.get_fully_qualified_table_name(tab), self.get_fully_qualified_table_name(
                                                    self.connectionHelper.queries.get_dmin_tabname(tab)))])

    def extract_params_from_args(self, args):
        return args[0]

    def get_dmin_val(self, attrib: str, tab: str):
        # Phase 4: if `tab` is actually an alias, prefer the alias-row dict
        # so we read the witness row backing that specific alias (matters when
        # min_card > 1 and the two aliases hold different values for `attrib`).
        if self.global_alias_row_dict and tab in self.global_alias_row_dict:
            entry = self.global_alias_row_dict[tab]
            cols = entry["cols"]
            if attrib in cols:
                val = entry["row"][cols.index(attrib)]
                return float(val) if isinstance(val, Decimal) else val
        base_tab = self._to_base(tab)
        res, des, val = None, None, None
        data_problem = False
        try:
            res, des = self.connectionHelper.execute_sql_fetchall(
                self.connectionHelper.queries.select_attribs_from_relation([attrib],
                                                            self.get_fully_qualified_table_name(base_tab)))
            val = res[0][0]
        except ValueError as e:
            data_problem = True
            self.logger.debug(e, "Could not fetch data from d_min, getting it from local dmin_instance_dict")
        except IndexError as e:
            data_problem = True
            self.logger.debug(e, "Could not fetch data from d_min, getting it from local dmin_instance_dict")
        except Exception as e:
            data_problem = True
            self.logger.debug(e, "Could not fetch data from d_min, getting it from local dmin_instance_dict")
        if data_problem:
            values = self.global_min_instance_dict[base_tab]
            attribs, vals = values[0], values[1]
            attrib_idx = attribs.index(attrib)
            val = vals[attrib_idx]
        ret_val = float(val) if isinstance(val, Decimal) else val
        return ret_val

    def truncate_core_relations(self):
        for table in self.core_relations:
            self.connectionHelper.execute_sql([self.connectionHelper.queries.truncate_table(
                                                    self.get_fully_qualified_table_name(table))])

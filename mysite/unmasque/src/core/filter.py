import copy
import math
import decimal
from typing import List

from ..core.abstract.abstractConnection import AbstractConnectionHelper
from ..core.abstract.un2_where_clause import UN2WhereClause
from ..util.aoa_utils import get_constants_for
from ..util.constants import un_precision
from ..util.error_handling import UnmasqueError
from ..util.error_codes import ERROR_007
from ..util.utils import is_int, get_val_plus_delta, get_min_and_max_val, \
    is_left_less_than_right_by_cutoff, get_format, get_mid_val, get_cast_value


def parse_for_int(val):
    try:
        v_int = int(val)
        v_int = str(val)
    except ValueError:
        v_int = f"\'{str(val)}\'"
    except TypeError:
        v_int = f"\'{str(val)}\'"
    return v_int


def round_ceil(num, places):
    adder = 5 / (10 ** (places + 1))
    ans = decimal.Decimal(num - adder)
    ans = ((ans * (10 ** places)).to_integral_exact(rounding=decimal.ROUND_CEILING)) / (10 ** places)
    return ans


def round_floor(num, places):
    adder = 5 / (10 ** (places + 1))
    # return round(num + adder, places)
    return num


def truncate_value(num, places):
    num = num * (10 ** (places + 1))
    num = math.trunc(num)
    num = num / (10 ** (places + 1))
    return num


class Filter(UN2WhereClause):

    def __init__(self, connectionHelper: AbstractConnectionHelper,
                 core_relations: List[str],
                 global_min_instance_dict: dict,
                 global_alias_row_dict: dict = None,
                 instances=None,
                 alias_to_table: dict = None):
        # Phase 4: derive a default single-instance alias model if upstream did
        # not supply one, so the iteration below has a uniform shape.
        if instances is None:
            from ..util.instance import Instance
            instances = [Instance(table=t, alias=t) for t in core_relations]
            alias_to_table = {t: t for t in core_relations}
        super().__init__(connectionHelper, core_relations, global_min_instance_dict, "Filter",
                         global_alias_row_dict=global_alias_row_dict,
                         instances=instances, alias_to_table=alias_to_table)
        self.filter_predicates = None

    def do_init(self):
        for tabname in self.core_relations:
            res, desc = self.connectionHelper.execute_sql_fetchall(
                self.connectionHelper.queries.get_column_details_for_table(self.connectionHelper.config.schema,
                                                                           tabname))

            tab_attribs = [row[0].lower() for row in res]
            self.global_all_attribs[tabname] = tab_attribs

            this_attribs = [(tabname, row[0].lower(), row[1].lower()) for row in res]
            self.global_attrib_types.extend(this_attribs)

            for entry in this_attribs:
                self.attrib_types_dict[(entry[0], entry[1])] = entry[2]

            self.global_attrib_max_length.update(
                {(tabname, row[0].lower()): int(str(row[2])) for row in res if is_int(str(row[2]))})

            if self.mock:
                self.insert_into_dmin_dict_values(tabname)

            res, desc = self.connectionHelper.execute_sql_fetchall(
                self.connectionHelper.queries.select_attribs_from_relation(tab_attribs,
                                                                    self.get_fully_qualified_table_name(tabname)))
            for row in res:
                for attrib, value in zip(tab_attribs, row):
                    self.global_d_plus_value[attrib] = value

    def doActualJob(self, args=None):
        query = super().doActualJob(args)
        self.do_init()
        self.filter_predicates = self.get_filter_predicates(query)
        self.logger.debug(self.filter_predicates)
        return self.filter_predicates

    def get_filter_predicates(self, query: str) -> list:
        # Phase 4: iterate per-alias rather than per-base-table. For single-
        # instance tables alias == table so predicate tuples are unchanged;
        # for multi-instance tables this is where the two aliases get
        # independent filter extraction passes.
        filter_attribs = []
        for inst in self.instances:
            base_tab = inst.table
            alias = inst.alias
            attrib_list = self.global_all_attribs[base_tab]
            for attrib in attrib_list:
                datatype = self.get_datatype((base_tab, attrib))
                self.extract_filter_on_attrib_set(filter_attribs, query, [(alias, attrib)], datatype)
        return filter_attribs

    def extract_filter_on_attrib_set(self, filter_attribs, query, attrib_list, datatype):
        if datatype == 'str':
            # Group mutation is not implemented for string/text/char/varchar data type
            one_attrib = attrib_list[0]
            tabname, attrib = one_attrib[0], one_attrib[1]
            self.handle_string_filter(attrib, filter_attribs, tabname, query)
        else:
            min_val_domain, max_val_domain = get_min_and_max_val(datatype)
            self.handle_filter_for_nonTextTypes(attrib_list, datatype, filter_attribs, max_val_domain, min_val_domain,
                                                query)

    def handle_filter_for_subrange(self, attrib_list, datatype, filter_attribs,
                                   max_val_domain, min_val_domain, query):
        delta, _ = get_constants_for(datatype)
        min_present = self.checkAttribValueEffect(query, get_format(datatype, min_val_domain),
                                                  attrib_list)  # True implies row
        # was still present
        max_present = self.checkAttribValueEffect(query, get_format(datatype, max_val_domain),
                                                  attrib_list)  # True implies row
        mandatory_attrib = attrib_list[0]
        tabname, attrib = mandatory_attrib[0], mandatory_attrib[1]
        if min_present and not max_present:
            val = self.get_filter_value(query, datatype,
                                        get_cast_value(datatype, min_val_domain),
                                        get_cast_value(datatype, max_val_domain), '<=', attrib_list)
            filter_attribs.append((tabname, attrib, '<=', min_val_domain, val))
        elif not min_present and max_present:
            val = self.get_filter_value(query, datatype,
                                        get_cast_value(datatype, min_val_domain),
                                        get_cast_value(datatype, max_val_domain), '>=', attrib_list)
            filter_attribs.append((tabname, attrib, '>=', val, max_val_domain))
        elif min_present and max_present:
            filter_attribs.append((tabname, attrib, 'range', min_val_domain, max_val_domain))
        else:
            if min_val_domain >= max_val_domain:
                return
            i_min, i_max = get_min_and_max_val(datatype)
            if max_val_domain == i_max or min_val_domain == i_min:
                self.handle_filter_for_nonTextTypes(attrib_list, datatype, filter_attribs,
                                                    max_val_domain, min_val_domain, query)
                return
            self.handle_filter_for_subrange(attrib_list, datatype, filter_attribs,
                                            get_val_plus_delta(datatype, max_val_domain, -1 * delta),
                                            get_val_plus_delta(datatype, min_val_domain, 1 * delta),
                                            query)

    def handle_filter_for_nonTextTypes(self, attrib_list, datatype, filter_attribs,
                                       max_val_domain, min_val_domain, query):
        if datatype in ['int', 'date', 'integer', 'number']:
            self.handle_point_filter(datatype, filter_attribs, query, attrib_list, min_val_domain, max_val_domain)
        elif datatype in ['numeric', 'float']:
            self.handle_precision_filter(filter_attribs, query, attrib_list, min_val_domain, max_val_domain)
        else:
            raise UnmasqueError(ERROR_007, "filter", f"Datatype {datatype}, is Not Handled by Current UNMASQUE...sorry!")

    def checkAttribValueEffect(self, query, val, attrib_list):
        # Phase 4: scope UPDATE to a single alias's witness row when the
        # alias-row dict is populated, so probing one alias doesn't mutate
        # the sibling alias's row on multi-instance D¹. For single-instance
        # tables (or when the alias dict is absent) we fall through to the
        # whole-table UPDATE preserving today's behaviour. Goes through
        # _exec_alias_ctid_update so the row's new (post-MVCC) ctid is
        # captured and the alias dict stays in sync.
        prev_values = self.get_dmin_val_of_attrib_list(attrib_list)
        for tab_attrib in attrib_list:
            identifier, attrib = tab_attrib[0], tab_attrib[1]
            base_tab = self._to_base(identifier)
            datatype = self.get_datatype((base_tab, attrib))
            used_alias = self._exec_alias_ctid_update(identifier, attrib, val,
                                                      is_date=(datatype == 'date'))
            if not used_alias:
                fqn = self.get_fully_qualified_table_name(base_tab)
                if datatype == 'date':
                    self.connectionHelper.execute_sql(
                        [self.connectionHelper.queries.update_sql_query_tab_date_attrib_value(
                            fqn, attrib, val)], self.logger)
                else:
                    self.connectionHelper.execute_sql(
                        [self.connectionHelper.queries.update_tab_attrib_with_value(
                            fqn, attrib, val)], self.logger)
        new_result = self.app.doJob(query)
        self.revert_filter_changes_in_tabset(attrib_list, prev_values)
        return self.app.isQ_result_nonEmpty_nullfree(new_result)

    def revert_filter_changes_in_tabset(self, attrib_list, prev_val_list):
        tab_attrib_set = set()
        for i in range(len(attrib_list)):
            tab_attrib = (attrib_list[i][0], attrib_list[i][1])
            tab_attrib_set.add(tab_attrib)
            val = prev_val_list[i]
            datatype = self.get_datatype(tab_attrib)
            self.mutate_dmin_with_val(datatype, tab_attrib, val)

    def handle_precision_filter(self, filterAttribs, query, attrib_list, min_val_domain, max_val_domain):
        # min_val_domain, max_val_domain = get_min_and_max_val(datatype)
        # NUMERIC HANDLING
        # PRECISION TO BE GET FROM SCHEMA GRAPH
        min_present = self.checkAttribValueEffect(query, min_val_domain,
                                                  attrib_list)  # True implies row was still present
        max_present = self.checkAttribValueEffect(query, max_val_domain,
                                                  attrib_list)  # True implies row was still present
        mandatory_attrib = attrib_list[0]
        tabname, attrib, = mandatory_attrib[0], mandatory_attrib[1]
        dmin_val = self.global_d_plus_value[attrib]
        float_dmin_val = get_cast_value('float', dmin_val)
        # mandatory_attrib[2], mandatory_attrib[3]
        # inference based on flag_min and flag_max
        if not min_present and not max_present:
            equalto_flag = self.get_filter_value(query, 'int', float_dmin_val - .01,
                                                 float_dmin_val + .01, '=', attrib_list)
            if equalto_flag:
                filterAttribs.append((tabname, attrib, '=', float_dmin_val, float_dmin_val))
            else:
                val1 = self.get_filter_value(query, 'float', float_dmin_val, max_val_domain, '<=', attrib_list)
                val2 = self.get_filter_value(query, 'float', min_val_domain, float_dmin_val,
                                             '>=', attrib_list)
                # Emit the contiguous envelope range. Within-attribute holes
                # (OR-of-intervals) are recovered post-Projection by GapPipeLine.
                filterAttribs.append((tabname, attrib, 'range', float(val2), float(val1)))
        elif min_present and not max_present:
            val = self.get_filter_value(query, 'float', float_dmin_val - 5, max_val_domain,
                                        '<=', attrib_list)
            val1 = self.get_filter_value(query, 'float', float(val), float(val) + 0.99, '<=', attrib_list)
            val1 = truncate_value(val1, un_precision)
            filterAttribs.append((tabname, attrib, '<=', float(min_val_domain), float(round_floor(val1, un_precision))))

        elif not min_present and max_present:
            val = self.get_filter_value(query, 'float', min_val_domain, float_dmin_val + 5,
                                        '>=', attrib_list)
            val1 = self.get_filter_value(query, 'float', float(val) - 1, val, '>=', attrib_list)
            val1 = truncate_value(val1, un_precision)
            filterAttribs.append((tabname, attrib, '>=', float(round_ceil(val1, un_precision)), float(max_val_domain)))

    def get_filter_value(self, query, datatype, min_val, max_val, operator, attrib_list):
        # Phase 4: bypass the old query_front_set path. It built SQL via
        # update_sql_query_tab_attribs(tabname, ...) where tabname could be an
        # alias like 'lineitem__a1', producing an UPDATE against a non-existent
        # relation that silently failed — leaving Qh unmutated, so binary
        # search never converged and emitted `range MIN MAX`. The alias-aware
        # update path resolves alias→base table internally.
        delta, while_cut_off = get_constants_for(datatype)

        low = min_val
        high = max_val
        prev_values = self.get_dmin_val_of_attrib_list(attrib_list)

        if operator == '<=':
            while is_left_less_than_right_by_cutoff(datatype, low, high, while_cut_off):
                mid_val, new_result = self.run_app_with_mid_val(datatype, high, low, query, attrib_list)
                if mid_val == low or high == mid_val:
                    break
                if self.app.isQ_result_empty(new_result):
                    high = mid_val
                else:
                    low = mid_val
            self.revert_filter_changes_in_tabset(attrib_list, prev_values)
            return low

        if operator == '>=':
            while is_left_less_than_right_by_cutoff(datatype, low, high, while_cut_off):
                mid_val, new_result = self.run_app_with_mid_val(datatype, high, low, query, attrib_list)
                if mid_val == high or low == mid_val:
                    break
                if self.app.isQ_result_empty(new_result):
                    low = mid_val
                else:
                    high = mid_val
            self.revert_filter_changes_in_tabset(attrib_list, prev_values)
            return high

        else:  # =, i.e. datatype == 'int', date
            is_low = self.run_app_for_a_val(datatype, low, query, attrib_list)
            self.revert_filter_changes_in_tabset(attrib_list, prev_values)
            is_high = self.run_app_for_a_val(datatype, high, query, attrib_list)
            self.revert_filter_changes_in_tabset(attrib_list, prev_values)
            return not is_low and not is_high

    def _update_attrib_list_with_val(self, datatype, val, attrib_list):
        """Phase 4: emit a single-attribute UPDATE for each (alias, attr) in
        attrib_list, routing through the alias-ctid path when the alias dict
        is populated so multi-instance D¹ probes don't mutate sibling rows."""
        formatted = get_format(datatype, val)
        is_date = datatype == 'date'
        for tab_attrib in attrib_list:
            identifier, attrib = tab_attrib[0], tab_attrib[1]
            used_alias = self._exec_alias_ctid_update(identifier, attrib, formatted,
                                                      is_date=is_date,
                                                      raw_val=val)
            if not used_alias:
                base_tab = self._to_base(identifier)
                fqn = self.get_fully_qualified_table_name(base_tab)
                if is_date:
                    sql = self.connectionHelper.queries.update_sql_query_tab_date_attrib_value(
                        fqn, attrib, formatted)
                else:
                    sql = self.connectionHelper.queries.update_tab_attrib_with_value(
                        fqn, attrib, formatted)
                self.connectionHelper.execute_sql([sql])

    def run_app_for_a_val(self, datatype, low, query, attrib_list):
        self._update_attrib_list_with_val(datatype, low, attrib_list)
        new_result = self.app.doJob(query)
        return not self.app.isQ_result_empty(new_result)

    def run_app_with_mid_val(self, datatype, high, low, query, attrib_list):
        mid_val = get_mid_val(datatype, high, low)
        self._update_attrib_list_with_val(datatype, mid_val, attrib_list)
        new_result = self.app.doJob(query)
        return mid_val, new_result

        # mukul

    def handle_point_filter(self, datatype, filterAttribs, query, attrib_list, min_val_domain, max_val_domain):
        # min and max domain values (initialize based on data type)
        # PLEASE CONFIRM THAT DATE FORMAT IN DATABASE IS YYYY-MM-DD
        min_present = self.checkAttribValueEffect(query, get_format(datatype, min_val_domain),
                                                  attrib_list)  # True implies row
        # was still present
        max_present = self.checkAttribValueEffect(query, get_format(datatype, max_val_domain),
                                                  attrib_list)  # True implies row
        mandatory_attrib = attrib_list[0]
        tabname, attrib, = mandatory_attrib[0], mandatory_attrib[1]
        dmin_val = self.global_d_plus_value[attrib]
        int_dmin_val = get_cast_value(datatype, dmin_val)
        # inference based on flag_min and flag_max
        # was still present
        if not min_present and not max_present:
            equalto_flag = self.get_filter_value(query, datatype, get_val_plus_delta(datatype, int_dmin_val, -1),
                                                 get_val_plus_delta(datatype, int_dmin_val, 1),
                                                 '=', attrib_list)
            if equalto_flag:
                filterAttribs.append((tabname, attrib, '=', dmin_val, dmin_val))
            else:
                val1 = self.get_filter_value(query, datatype, int_dmin_val,
                                             get_val_plus_delta(datatype,
                                                                get_cast_value(datatype, max_val_domain), 1), '<=',
                                             attrib_list)
                val2 = self.get_filter_value(query, datatype,
                                             get_val_plus_delta(datatype, get_cast_value(datatype, min_val_domain), -1),
                                             int_dmin_val, '>=', attrib_list)
                # Emit the contiguous envelope range. Within-attribute holes
                # (OR-of-intervals) are recovered post-Projection by GapPipeLine.
                filterAttribs.append((tabname, attrib, 'range', val2, val1))
        elif min_present and not max_present:
            val = self.get_filter_value(query, datatype, int_dmin_val,
                                        get_val_plus_delta(datatype,
                                                           get_cast_value(datatype, max_val_domain), 1), '<=',
                                        attrib_list)
            filterAttribs.append((tabname, attrib, '<=', min_val_domain, val))
        elif not min_present and max_present:
            val = self.get_filter_value(query, datatype,
                                        get_val_plus_delta(datatype, get_cast_value(datatype, min_val_domain), -1),
                                        int_dmin_val, '>=', attrib_list)
            filterAttribs.append((tabname, attrib, '>=', val, max_val_domain))
        # Both-domain-extremes case (e.g. A < 5 OR A > 20): Filter emits no
        # predicate here; GapPipeLine (post-Projection) detects the hole via the
        # Q_E-vs-Qh diff and synthesises the disjunction with a full-domain
        # envelope carrier.

    def handle_string_filter(self, attrib, filterAttribs, tabname, query):
        # STRING HANDLING
        # ESCAPE CHARACTERS IN STRING REMAINING
        if self.checkStringPredicate(query, tabname, attrib):
            # returns true if there is predicate on this string attribute
            val = self.getStrFilterValue(query, tabname, attrib, str(self.global_d_plus_value[attrib]))
            val = val.strip()
            if '%' in val or '_' in val:
                filterAttribs.append((tabname, attrib, 'LIKE', val, val))
            else:
                filterAttribs.append((tabname, attrib, 'equal', val, val))

    def checkStringPredicate(self, query, tabname, attrib):
        prev_values = self.get_dmin_val_of_attrib_list([(tabname, attrib)])
        # update query
        val = 'b' if (self.global_d_plus_value[attrib] is not None and self.global_d_plus_value[attrib][
            0] == 'a') else 'a'
        val_result = self.run_updateQ_with_temp_str(attrib, query, tabname, val)
        empty_result = self.run_updateQ_with_temp_str(attrib, query, tabname, "" "")
        effect = self.app.isQ_result_empty(val_result) \
                 or self.app.isQ_result_empty(empty_result)
        # update table so that result is not empty
        self.revert_filter_changes_in_tabset([(tabname, attrib)], prev_values)
        return effect

    def getStrFilterValue(self, query, tabname, attrib, representative):
        if (tabname, attrib) in self.global_attrib_max_length.keys():
            max_length = self.global_attrib_max_length[(tabname, attrib)]
        else:
            max_length = 100000

        index = 0
        output = ""
        # currently inverted exclaimaination is being used assuming it will not be in the string
        # GET minimal string with _
        while index < len(representative):
            temp = list(representative)
            if temp[index] == 'a':
                temp[index] = 'b'
            else:
                temp[index] = 'a'
            temp = ''.join(temp)
            new_result = self.run_updateQ_with_temp_str(attrib, query, tabname, temp)
            if not self.app.isQ_result_empty(new_result):
                temp = copy.deepcopy(representative)
                temp = temp[:index] + temp[index + 1:]
                new_result = self.run_updateQ_with_temp_str(attrib, query, tabname, temp)
                if not self.app.isQ_result_empty(new_result):
                    representative = representative[:index] + representative[index + 1:]
                else:
                    output = output + "_"
                    representative = list(representative)
                    representative[index] = u"\u00A1"
                    representative = ''.join(representative)
                    index = index + 1
            else:
                output = output + representative[index]
                index = index + 1
        if output == '':
            return output
        # GET % positions
        index = 0
        representative = copy.deepcopy(output)
        if len(representative) < max_length:
            output = ""
            while index < len(representative):
                temp = list(representative)
                if temp[index] == 'a':
                    temp.insert(index, 'b')
                else:
                    temp.insert(index, 'a')
                temp = ''.join(temp)
                new_result = self.run_updateQ_with_temp_str(attrib, query, tabname, temp)
                if not self.app.isQ_result_empty(new_result):
                    output = output + '%'
                output = output + representative[index]
                index = index + 1
            temp = list(representative)
            if temp[index - 1] == 'a':
                temp.append('b')
            else:
                temp.append('a')
            temp = ''.join(temp)
            new_result = self.run_updateQ_with_temp_str(attrib, query, tabname, temp)
            if not self.app.isQ_result_empty(new_result):
                output = output + '%'
        return output

    def run_updateQ_with_temp_str(self, attrib, query, tabname, temp):
        # Phase 4 (Bug #1): on multi-instance D¹, scope the probe UPDATE to a
        # single alias's witness row via ctid. A whole-table UPDATE here would
        # mutate both aliases (collapsing the self-join distinguisher) AND move
        # both rows' ctids under MVCC, leaving every subsequent ctid-scoped
        # probe targeting a stale ctid. _exec_alias_ctid_update refreshes the
        # cached ctid via RETURNING. Single-instance path falls back unchanged.
        prev_values = self.get_dmin_val_of_attrib_list([(tabname, attrib)])
        used_alias = self._exec_alias_ctid_update(tabname, attrib, temp, quoted=True)
        if not used_alias:
            base_tab = self._to_base(tabname)
            up_query = self.connectionHelper.queries.update_tab_attrib_with_quoted_value(
                self.get_fully_qualified_table_name(base_tab), attrib, temp)
            self.connectionHelper.execute_sql([up_query])
        new_result = self.app.doJob(query)
        self.revert_filter_changes_in_tabset([(tabname, attrib)], prev_values)
        return new_result
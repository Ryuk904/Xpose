import ast

from frozenlist._frozenlist import FrozenList

from .dataclass.genPipeline_context import GenPipelineContext
from .row_probe import RowProbe
from ..util.error_codes import ERROR_005
from ..util.error_handling import UnmasqueError

from ...src.core.abstract.GenerationPipeLineBase import GenerationPipeLineBase, get_boundary_value
from ...src.core.abstract.abstractConnection import AbstractConnectionHelper
from ...src.util.utils import get_dummy_val_for, get_val_plus_delta, get_format, get_char
from ..util.constants import COUNT_THERE, CONST_1_THERE, CONST_1_VALUE, NUMBER_TYPES, NON_TEXT_TYPES


def has_attrib_key_condition(attrib, attrib_inner, key_list):
    return attrib_inner == attrib or attrib_inner in key_list


class GroupBy(GenerationPipeLineBase):
    def __init__(self, connectionHelper: AbstractConnectionHelper,
                 genPipelineCtx: GenPipelineContext,
                 pgao_ctx):
        super().__init__(connectionHelper, "Group_By", genPipelineCtx)
        self.projected_attribs = pgao_ctx.projected_attribs
        self.has_groupby = False
        self.group_by_attrib = []
        # Enabler S2: duplicate-row probe used to disambiguate a literal
        # constant 1 from a COUNT()==1 (WI-05).
        self._row_probe = RowProbe(self.connectionHelper, self.app, self.logger)

    def doExtractJob(self, query):
        # Array to check for 1 as constant or count in select clause. (will be used later)
        check_array = [0] * len(self.projected_attribs)

        for tabname in self.core_relations:
            attrib_list = self.global_all_attribs[tabname]

            for attrib in attrib_list:
                self.truncate_core_relations()
                self.logger.debug("Checking for attrib: ", attrib)
                # determine offset values for this attribute
                curr_attrib_value = [0, 1, 1]

                key_list = next((elt for elt in self.global_join_graph if attrib in elt), [])

                # For this table (tabname) and this attribute (attrib), fill all tables now
                for tabname_inner in self.core_relations:
                    attrib_list_inner = self.global_all_attribs[tabname_inner]
                    insert_rows = []
                    no_of_rows = 3 if tabname_inner == tabname else 1
                    key_path_flag = any(val in key_list for val in attrib_list_inner)
                    if tabname_inner != tabname and key_path_flag:
                        no_of_rows = 2

                    attrib_list_str = ",".join(attrib_list_inner)
                    att_order = f"({attrib_list_str})"
                    self.logger.debug(f"for {tabname_inner} insert {no_of_rows} rows")
                    for k in range(no_of_rows):
                        insert_values = []
                        for attrib_inner in attrib_list_inner:
                            datatype = self.get_datatype((tabname_inner, attrib_inner))

                            if has_attrib_key_condition(attrib, attrib_inner, key_list):
                                self.insert_values_for_joined_attribs(attrib_inner, curr_attrib_value, datatype,
                                                                      insert_values, k, tabname_inner)
                            else:
                                self.insert_values_for_single_attrib(attrib_inner, datatype, insert_values,
                                                                     tabname_inner)
                        insert_rows.append(tuple(insert_values))

                    self.insert_attrib_vals_into_table(att_order, attrib_list_inner, insert_rows, tabname_inner)

                self.see_d_min()
                new_result = self.app.doJob(query)

                # Checking for 1 as constant in select clause or count is present.
                for i in range(len(self.projected_attribs)):
                    if self.projected_attribs[i] == '':
                        for j in range(1, len(new_result)):  # skipping the header of result
                            if new_result[j][i] != CONST_1_VALUE:
                                check_array[i] = COUNT_THERE
                            elif new_result[j][i] == CONST_1_VALUE and check_array[i] != COUNT_THERE:
                                check_array[i] = CONST_1_THERE

                if self.app.isQ_result_empty(new_result):
                    self.logger.error('some error in generating new database. '
                                      'Result is empty. Can not identify Grouping')
                    return False
                nonEmpty_rows = self.app.get_all_nullfree_rows(new_result)
                if len(nonEmpty_rows) == 2:
                    self.group_by_attrib.append(attrib)
                    self.has_groupby = True
                elif len(nonEmpty_rows) == 1:
                    # It indicates groupby on at least one attribute
                    self.has_groupby = True

        self.remove_duplicates()

        # const-1 vs COUNT()==1 disambiguation (WI-05).
        #
        # The value heuristic above marks a column CONST_1_THERE when every
        # group it happened to observe showed the literal string '1'. That is
        # *unsound*: a genuine COUNT(*) / COUNT(col) whose value is 1 in every
        # probed group is indistinguishable, by value alone, from a literal 1.
        # It is sound today only because the probe synthesises the grouping
        # column with the repeated delta [0, 1, 1] (groupby_clause:39), which
        # incidentally always materialises a >=2-row group for a real COUNT —
        # an unrelated implementation detail the classifier should not lean on.
        #
        # Resolve each candidate directly with a controlled duplicate-row probe
        # (enabler S2): on the single-group witness instance D¹, duplicate one
        # contributing witness row. A COUNT tracks row multiplicity and rises
        # 1 -> 2; the literal 1 is invariant and stays 1.
        const1_cols = [i for i in range(len(check_array)) if check_array[i] == CONST_1_THERE]
        for i in self._confirm_const1_columns(query, const1_cols):
            self.projected_attribs[i] = CONST_1_VALUE

        return True

    def _confirm_const1_columns(self, query, const1_cols):
        """Confirm, via an S2 duplicate-row probe, which value-heuristic
        const-1 candidates are genuinely the literal constant 1.

        Returns the sublist of ``const1_cols`` that are real literal-1 columns.
        Columns that turn out to be a COUNT stuck at 1 are dropped from the
        returned list, so they are left empty-projected and Aggregation
        renders them as ``COUNT(*)`` (groupby_clause feeds aggregation.py:241).

        Soundness: the literal 1 does not depend on row multiplicity, whereas
        COUNT increases by at least one when a contributing tuple is added.
        Probing on ``D¹`` (a single group) makes the duplicated tuple land in
        that one group, so the group's value — read as the max over the lone
        result row — is a clean signal with no multi-group dilution. The probe
        only runs when there is at least one candidate, and degrades to the
        old heuristic verdict (treat as const-1) whenever it cannot get a
        trustworthy reading, so it never introduces a false positive on a real
        literal 1.
        """
        if not const1_cols:
            return []
        if not self.core_relations:
            return list(const1_cols)

        # Fresh single-witness instance: one group, Qh non-empty & null-free.
        self.do_init()
        base_res = self.app.doJob(query)
        if not self.app.done:
            return list(const1_cols)
        base_rows = self.app.get_all_nullfree_rows(base_res)
        if not base_rows:
            return list(const1_cols)
        base_val = {i: self._max_int_in_col(base_rows, i) for i in const1_cols}

        # Add exactly one contributing tuple: duplicate a single witness row of
        # the first core relation by ctid (targeted, so a k>1 alias table is
        # not over-inserted), then revert it.
        fqn = self.get_fully_qualified_table_name(self.core_relations[0])
        ctids = self._row_probe.list_ctids(fqn)
        new_ctids = self._row_probe.duplicate_rows(fqn, ctids[:1] if ctids else None)
        try:
            after_res = self.app.doJob(query)
            after_rows = self.app.get_all_nullfree_rows(after_res) if self.app.done else []
        finally:
            if new_ctids:
                self._row_probe.delete_rows(fqn, new_ctids)

        genuine = []
        for i in const1_cols:
            b = base_val.get(i)
            a = self._max_int_in_col(after_rows, i) if after_rows else None
            if a is not None and b is not None and a > b:
                self.logger.info(f"GroupBy: column {i} reclassified const-1 -> COUNT "
                                 f"(duplicate-row probe {b} -> {a})")
            else:
                genuine.append(i)
        return genuine

    @staticmethod
    def _max_int_in_col(rows, col_index):
        """Largest integer value in result column ``col_index`` across
        ``rows`` (string-valued result tuples); ``None`` if no row carries an
        integer there."""
        best = None
        for row in rows:
            if col_index >= len(row):
                continue
            try:
                v = int(str(row[col_index]).strip())
            except (ValueError, TypeError):
                continue
            if best is None or v > best:
                best = v
        return best

    def insert_values_for_single_attrib(self, attrib_inner, datatype, insert_values, tabname_inner):
        if datatype in NON_TEXT_TYPES:
            val = self.get_insert_value_for_single_attrib(datatype, attrib_inner, tabname_inner)
            if datatype == 'date':
                insert_values.append(ast.literal_eval(get_format('date', val)))
            else:
                insert_values.append(get_format('int', val))
        else:
            if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
                filtered_val = self.get_s_val_for_textType(attrib_inner, tabname_inner)
                char_val = filtered_val.replace('%', '')
            else:
                char_val = get_char(get_dummy_val_for('char'))
            insert_values.append(char_val)

    def insert_values_for_joined_attribs(self, attrib_inner, curr_attrib_value, datatype, insert_values, k,
                                         tabname_inner):
        delta = curr_attrib_value[k]
        if datatype in NON_TEXT_TYPES:
            val = self.get_insert_value_for_joined_attribs(datatype, attrib_inner,
                                                           delta, tabname_inner)
            if datatype == 'date':
                insert_values.append(ast.literal_eval(get_format('date', val)))
            else:
                insert_values.append(get_format('int', val))
        else:
            plus_val = get_char(get_val_plus_delta('char', get_dummy_val_for('char'), delta))
            if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
                filtered_val = self.get_s_val_for_textType(attrib_inner, tabname_inner)
                if '_' in filtered_val:
                    insert_values.append(filtered_val.replace('_', plus_val))
                else:
                    insert_values.append(filtered_val.replace('%', plus_val, 1))
                insert_values[-1].replace('%', '')
            else:
                insert_values.append(plus_val)

    def get_insert_value_for_single_attrib(self, datatype, attrib_inner, tabname_inner):
        if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
            val = self.filter_attrib_dict[(tabname_inner, attrib_inner)][0]
            val = get_boundary_value(val, is_ub=False)
        else:
            val = get_dummy_val_for(datatype)
        return val

    def get_insert_value_for_joined_attribs(self, datatype, attrib_inner, delta, tabname_inner):
        self.logger.debug(tabname_inner, attrib_inner)
        if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
            val = self.__get_s_plus_k_val(attrib_inner, datatype, delta, tabname_inner)
        else:
            val = get_val_plus_delta(datatype, get_dummy_val_for(datatype), delta)
        self.logger.debug(val)
        return val

    def __get_s_plus_k_val(self, attrib_inner, datatype, delta, tabname_inner):
        if isinstance(self.filter_attrib_dict[(tabname_inner, attrib_inner)], tuple):  # range
            zero_val = self.filter_attrib_dict[(tabname_inner, attrib_inner)][0]
            zero_val = get_val_plus_delta(datatype, zero_val, delta)
            one_val = self.filter_attrib_dict[(tabname_inner, attrib_inner)][1]
            val = min(zero_val, one_val)
        elif isinstance(self.filter_attrib_dict[(tabname_inner, attrib_inner)], FrozenList):  # IN
            zero_val = self.filter_attrib_dict[(tabname_inner, attrib_inner)][0]
            i = 0
            while i <= delta:
                zero_val = self.filter_attrib_dict[(tabname_inner, attrib_inner)][i]
                if not isinstance(zero_val, tuple):
                    i += 1
                    continue
                else:
                    zero_val, zone_val = zero_val[0], zero_val[-1]
                    left = delta - i
                    if datatype == 'date':
                        gap = (zone_val - zero_val).days
                    elif datatype in NUMBER_TYPES:
                        gap = zone_val - zero_val
                    else:
                        self.logger.debug(f"Datatype '{datatype}' is not supported.")
                        raise UnmasqueError(ERROR_005, "groupby_clause", f"Datatype '{datatype}' is not supported.")
                    if gap >= left:
                        zero_val = get_val_plus_delta(datatype, zero_val, left)
                        break
                    else:
                        i = left - gap
            one_val = self.filter_attrib_dict[(tabname_inner, attrib_inner)][-1]
            one_val = get_boundary_value(one_val, is_ub=True)
            val = min(zero_val, one_val)
        else:  # =
            val = self.filter_attrib_dict[(tabname_inner, attrib_inner)]
        return val

    def remove_duplicates(self):
        to_remove = []
        for attrib in self.group_by_attrib:
            if attrib not in self.projected_attribs:
                to_remove.append(attrib)
        for r in to_remove:
            self.group_by_attrib.remove(r)
        self.group_by_attrib.sort()

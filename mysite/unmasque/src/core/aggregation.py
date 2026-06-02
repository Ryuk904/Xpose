import ast
import copy
import math

from frozenlist._frozenlist import FrozenList

from .dataclass.genPipeline_context import GenPipelineContext
from .projection import get_param_values_external
from .row_probe import RowProbe
from ..util.utils import is_number, get_dummy_val_for, get_val_plus_delta, get_format, get_char, get_format_for_agg
from ...src.core.abstract.GenerationPipeLineBase import GenerationPipeLineBase, get_boundary_value
from ..util.constants import NUMBER_TYPES
from ...src.util.constants import SUM, AVG, MIN, MAX, COUNT, COUNT_STAR, COUNT_DISTINCT
from ...src.util.constants import min_int_val, max_int_val


def _max_int_in_result_col(rows, col_index):
    """Largest integer value in result column ``col_index`` across ``rows``
    (string-valued result tuples); ``None`` if no row carries an integer there.

    Used to read a COUNT result column's value off a (possibly multi-group)
    result. On the single-group witness instance the COUNT probes run on, there
    is exactly one result row, so this is just "read the count"; ``max`` keeps it
    robust if the instance ever yields more than one group."""
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


def get_k_value_for_number(a, b):
    if a == b:
        k_value = 1
        if a == 2:
            k_value = 2
        agg_array = [SUM, k_value * a + b, AVG, a, MIN, a, MAX, a, COUNT, k_value + 1]
    else:
        constraint_array = [0, a, b, a - 1, b - 1]
        if a != 0:
            constraint_array.append((a - b) / a)
        if a != 1:
            constraint_array.append((1 - b) / (a - 1))
        if (a - 2) ** 2 - (4 * (1 - b)) >= 0:
            constraint_array.append(((a - 2) + math.sqrt((a - 2) ** 2 - (4 * (1 - b)))) / 2)
        k_value = 2
        while k_value in constraint_array:
            k_value = k_value + 1
        avg = round((k_value * a + b) / (k_value + 1), 2)
        agg_array = [SUM, k_value * a + b, AVG, avg, MIN, min(a, b), MAX, max(a, b), COUNT, k_value + 1]
    return k_value, agg_array


def get_k_value(attrib, filter_attrib_dict, groupby_key_flag, tabname, datatype, c):
    if groupby_key_flag and datatype in NUMBER_TYPES:
        a = b = 3
        k_value = 1
        agg_array = [SUM, k_value * a + b, AVG, a, MIN, a, MAX, a, COUNT, k_value + 1]
    elif (tabname, attrib) in filter_attrib_dict.keys():
        if datatype in NUMBER_TYPES:
            # PRECISION TO BE TAKEN CARE FOR NUMERIC
            lb, ub = filter_attrib_dict[(tabname, attrib)][0], filter_attrib_dict[(tabname, attrib)][1]
            lb = get_boundary_value(lb, is_ub=False)
            ub = get_boundary_value(ub, is_ub=True)
            a = min(max(get_dummy_val_for('int') + c, lb), ub)
            b = min(min(get_dummy_val_for('int') + c, lb), ub)
            b = min(a + 1, b)
            if a == 0:  # swap a and b
                a = b
                b = 0
            k_value, agg_array = get_k_value_for_number(a, b)
        elif datatype == 'date':
            date_lb, date_ub = filter_attrib_dict[(tabname, attrib)][0], filter_attrib_dict[(tabname, attrib)][1]
            date_lb = get_boundary_value(date_lb, is_ub=False)
            date_ub = get_boundary_value(date_ub, is_ub=True)
            a = get_format_for_agg('date', date_lb)
            date_val_plus_1 = get_val_plus_delta('date', date_lb, 1)
            b = get_format_for_agg('date', min(date_val_plus_1, date_ub))
            k_value = 1
            agg_array = [MIN, min(a, b), MAX, max(a, b)]
            #a = ast.literal_eval(a)
            #b = ast.literal_eval(b)
        else:
            # string filter attribute
            if '_' in filter_attrib_dict[(tabname, attrib)]:
                a = filter_attrib_dict[(tabname, attrib)].replace('_', 'a')
                b = filter_attrib_dict[(tabname, attrib)].replace('_', 'b')
            else:
                a = filter_attrib_dict[(tabname, attrib)].replace('%', 'a', 1)
                b = filter_attrib_dict[(tabname, attrib)].replace('%', 'b', 1)
            a = a.replace('%', '')
            b = b.replace('%', '')
            k_value = 1
            agg_array = [MIN, min(a, b), MAX, max(a, b)]
    else:
        if datatype == 'date':
            a = get_format('date', get_dummy_val_for('date'))
            b = get_format('date', get_val_plus_delta('date', get_dummy_val_for('date'), 1))
            k_value = 1
            agg_array = [MIN, min(a, b), MAX, max(a, b)]
        elif datatype in NUMBER_TYPES:
            # Combination which gives all different results for aggregation
            a = 5
            b = 8
            k_value = 2
            agg_array = [SUM, 18, AVG, 6, MIN, 5, MAX, 8, COUNT, 3]
        else:
            # String data type
            a = get_char(get_dummy_val_for('char'))
            b = get_char(get_val_plus_delta('char', get_dummy_val_for('char'), 1))
            k_value = 1
            agg_array = [MIN, min(a, b), MAX, max(a, b)]
    # print(tabname, attrib, a, b)
    return a, agg_array, b, k_value


def get_no_of_rows(attrib_list_inner, k_value, key_list, tabname, tabname_inner, result_index, deps):
    same_tab_flag = False
    local_dep = deps[result_index]
    if tabname_inner == tabname:
        no_of_rows = k_value + 1
        same_tab_flag = True
    else:
        no_of_rows = 1
    key_path_flag = False
    for val in attrib_list_inner:
        if val in key_list:
            key_path_flag = True
            break
    if not same_tab_flag and key_path_flag:
        no_of_rows = 2
    return no_of_rows


class Aggregation(GenerationPipeLineBase):
    def __init__(self, connectionHelper,
                 genPipelineCtx: GenPipelineContext,
                 pgao_Ctx):
        super().__init__(connectionHelper, "Aggregation", genPipelineCtx)
        self.global_aggregated_attributes = None
        self.global_projected_attributes = pgao_Ctx.projected_attribs
        self.has_groupby = pgao_Ctx.has_groupby
        self.global_groupby_attributes = pgao_Ctx.group_by_attrib
        self.dependencies = pgao_Ctx.projection_dependencies
        self.solution = pgao_Ctx.projection_solution
        self.param_list = pgao_Ctx.projection_param_list
        # WI-06: S2 duplicate-row probe used to split COUNT(*) / COUNT(col) /
        # COUNT(DISTINCT col). Gated by the count_distinct feature flag.
        self._row_probe = RowProbe(self.connectionHelper, self.app, self.logger)

    def doExtractJob(self, query):
        # AsSUMing NO DISTINCT IN AGGREGATION

        self.global_aggregated_attributes = [(element, '') for element in self.global_projected_attributes]
        if not self.has_groupby:
            return False
        for tabname in self.core_relations:
            attrib_list = copy.deepcopy(self.global_all_attribs[tabname])
            for c, attrib in enumerate(attrib_list):
                # check if it is a key attribute
                key_list = next((elt for elt in self.global_join_graph if attrib in elt), [])

                self.logger.debug("Group By Attribs", self.global_groupby_attributes)
                self.logger.debug("Key attribs", key_list)
                tc = False
                for _key in key_list:
                    if _key in self.global_groupby_attributes:
                        tc = True
                if tc:
                    continue
                self.logger.debug("Not groupby", attrib)
                # Attribute Filtering
                if attrib in self.global_groupby_attributes:
                    continue

                result_index_list = []

                for j, dep in enumerate(self.dependencies):
                    for d in dep:
                        if attrib in d and self.global_aggregated_attributes[j][1] == '':
                            result_index_list.append(j)
                            break

                groupby_key_flag = False
                if attrib in self.joined_attribs and attrib in self.global_groupby_attributes:
                    groupby_key_flag = True
                for result_index in result_index_list:
                    datatype = self.get_datatype((tabname, attrib))
                    a, agg_array, b, k_value = get_k_value(attrib, self.filter_attrib_dict,
                                                           groupby_key_flag, tabname, datatype, c)

                    self.truncate_core_relations()
                    temp_vals = []
                    max_no_of_rows = self.insert_for_inner(a, attrib, b, k_value, key_list, tabname, temp_vals,
                                                           result_index)
                    self.logger.debug(self.dependencies, result_index,
                                      key_list, tabname, temp_vals, result_index)

                    if len(self.solution[result_index]) > 1:
                        self.logger.debug("Temp values", temp_vals)  # FOR DEBUG
                        s = 0
                        mi = max_int_val
                        ma = min_int_val
                        av = 0
                        temp_ar = []
                        local_sol = self.solution[result_index]
                        for ele in self.dependencies[result_index]:
                            local_tabname = ele[0]
                            local_attrib = ele[1]
                            local_attrib_index = self.global_all_attribs[local_tabname].index(local_attrib)
                            vals_sp = temp_vals[self.core_relations.index(local_tabname)]
                            l = []
                            for row in vals_sp:
                                l.append(row[local_attrib_index])
                            temp_ar.append((local_attrib, l))
                        temp_ar = sorted(temp_ar, key=lambda x: x[0])
                        self.logger.debug("Temp Arr", temp_ar)  # FOR DEBUG
                        for t in range(len(temp_ar)):
                            if len(temp_ar[t][1]) < max_no_of_rows:
                                while len(temp_ar[t][1]) < max_no_of_rows:
                                    temp_ar[t][1].append(temp_ar[t][1][0])
                        for _row in range(max_no_of_rows):
                            inter_val = []
                            eqn = 0
                            for j in range(len(self.dependencies[result_index])):
                                inter_val.append(float(temp_ar[j][1][_row]))
                            n = len(self.dependencies[result_index])

                            temp_arr = get_param_values_external(inter_val)
                            self.logger.debug("Coeffs", temp_arr, local_sol)  # FOR DEBUG
                            inter_val = [0 for j in range(len(self.param_list[result_index]))]
                            for j in range(len(self.param_list[result_index])):
                                inter_val[j] = temp_arr[j]
                            inter_val.append(1)
                            self.logger.debug("Intermediate Values of all", inter_val)  # FOR DEBUG
                            for j, val in enumerate(inter_val):
                                eqn += (val * local_sol[j][0])
                            # print("Expression", eqn) # FOR DEBUG
                            s += eqn
                            mi = eqn if eqn < mi else mi
                            ma = eqn if eqn > ma else ma
                        self.logger.debug("no_of_rows ", max_no_of_rows)
                        av = (s / max_no_of_rows)
                        self.logger.debug("Temp Array", temp_ar)
                        self.logger.debug("SUM, AVG, MIN, MAX", s, av, mi, ma)
                        agg_array = [SUM, s, AVG, av, MIN, mi, MAX, ma, COUNT, max_no_of_rows]
                    new_result = self.app.doJob(query)
                    self.logger.debug("New Result", new_result)  # FOR DEBUG
                    self.logger.debug("Comaparison", agg_array)  # FOR DEBUG
                    if self.app.isQ_result_empty(new_result):
                        self.logger.error('some error in generating new database. '
                                          'Result is empty. Can not identify aggregation')
                        return False
                    nullfree_rows = self.app.get_all_nullfree_rows(new_result)
                    if len(nullfree_rows) > 1:
                        continue

                    self.analyze(agg_array, self.global_projected_attributes[result_index], nullfree_rows, result_index)

        for i in range(len(self.global_projected_attributes)):
            if self.global_projected_attributes[i] == '':
                self.global_aggregated_attributes[i] = ('', COUNT_STAR)

        if getattr(self.connectionHelper.config, 'detect_count_distinct', False):
            self._refine_counts(query)

        return True

    # ------------------------------------------------------------------ WI-06
    def _refine_counts(self, query):
        """Refine each column the pipeline reconstructed as ``COUNT(*)`` into
        one of ``COUNT(*)`` / ``COUNT(col)`` / ``COUNT(DISTINCT col)`` by
        controlled multiplicity probes on the witness instance D¹.

        Why this is needed: a COUNT has no value-dependency (mutating a base
        value never moves a COUNT), so the projection stage leaves a count
        column empty-projected and the loop above blanket-labels it
        ``('', COUNT_STAR)``. That is correct for ``COUNT(*)`` and for
        ``COUNT(col)`` on a non-null column (``count(col) == count(*)`` there),
        but wrong for ``COUNT(DISTINCT col)`` and for ``COUNT(col)`` on a
        nullable column. Those distinctions are *multiplicity* phenomena,
        invisible on a single row, so we recover them on a controlled
        multi-row instance (enabler S2).

        Black-box discipline: nothing here reads the query text. Every verdict
        comes from inserting a crafted row, running Qh, and reading the count.
        """
        count_indexes = [i for i in range(len(self.global_aggregated_attributes))
                         if self.global_aggregated_attributes[i][1] == COUNT_STAR]
        if not count_indexes:
            return
        candidates = self._count_arg_candidates()

        for ri in count_indexes:
            # Baseline count on the single-witness instance (one group -> one
            # result row -> count reads its base value, typically 1).
            self.do_init()
            base_res = self.app.doJob(query)
            if not self.app.done:
                continue
            base_rows = self.app.get_all_nullfree_rows(base_res)
            base_val = _max_int_in_result_col(base_rows, ri)
            if base_val is None:
                continue

            # Step 1 - distinctness. Duplicate one contributing witness row
            # EXACTLY (S2). A non-distinct count (COUNT(*) / COUNT(col)) tracks
            # multiplicity and rises; COUNT(DISTINCT col) sees a repeated value
            # and stays. The exact duplicate is a copy of a surviving row, so it
            # always survives Qh -> this signal is reliable.
            after = self._distinctness_probe(query, ri)
            if after is None:
                continue

            if after == base_val:
                # DISTINCT count: find which column it distinct-counts.
                found = self._identify_distinct_column(query, ri, base_val, candidates)
                if found is not None:
                    tab, col = found
                    self.global_aggregated_attributes[ri] = (col, COUNT_DISTINCT)
                    self.logger.info(f"Aggregation WI-06: result col {ri} -> COUNT(DISTINCT {col})")
                else:
                    self.logger.info(f"Aggregation WI-06: result col {ri} is a DISTINCT count but the "
                                     f"counted column could not be identified (join/group key or "
                                     f"single-valued under filters); leaving COUNT(*).")
            elif after > base_val:
                # NON-DISTINCT. Companion: distinguish COUNT(col) on a nullable
                # column from COUNT(*). Result-equivalent on non-null columns,
                # so this only makes the column-COUNT render (WI-01) observable;
                # if no nullable counted column is found it stays COUNT(*).
                found = self._identify_nonnull_count_column(query, ri, base_val, candidates)
                if found is not None:
                    tab, col = found
                    self.global_aggregated_attributes[ri] = (col, COUNT)
                    self.logger.info(f"Aggregation WI-06: result col {ri} -> COUNT({col})")

        # Leave D¹ clean for downstream stages.
        self.do_init()

    def _count_arg_candidates(self):
        """Base columns that can serve as a COUNT argument and be perturbed
        soundly: exclude GROUP BY keys (perturbing moves the row to another
        group) and equi-join keys (perturbing breaks the join, so the crafted
        row would be silently dropped and read as a false negative)."""
        join_attrs = set()
        for edge in self.global_join_graph:
            for a in edge:
                join_attrs.add(a)
        candidates = []
        for tab in self.core_relations:
            for col in self.global_all_attribs[tab]:
                if col in self.global_groupby_attributes:
                    continue
                if col in join_attrs:
                    continue
                candidates.append((tab, col))
        return candidates

    def _distinctness_probe(self, query, ri):
        """Exact-duplicate one witness row of the driving relation and read the
        count at ``ri``. Returns the post-duplication count (caller compares it
        to the baseline: equal -> DISTINCT, greater -> non-distinct), or None if
        unreadable. Reverts the duplicate so D¹ is left unchanged."""
        fqn = self.get_fully_qualified_table_name(self.core_relations[0])
        ctids = self._row_probe.list_ctids(fqn)
        new_ctids = self._row_probe.duplicate_rows(fqn, ctids[:1] if ctids else None)
        try:
            res = self.app.doJob(query)
            rows = self.app.get_all_nullfree_rows(res) if self.app.done else []
            return _max_int_in_result_col(rows, ri)
        finally:
            if new_ctids:
                self._row_probe.delete_rows(fqn, new_ctids)

    def _identify_distinct_column(self, query, ri, base_val, candidates):
        """For a count already known to be DISTINCT, find the counted column:
        insert one witness-copy row whose only change is a *fresh distinct*
        value in the candidate column. Only the truly distinct-counted column
        adds a new distinct value -> count rises by one; every other column is
        held at its witness value -> count is unchanged."""
        for tab, col in candidates:
            cnt = self._probe_count_with_col(query, ri, tab, col, 'fresh')
            if cnt is not None and cnt == base_val + 1:
                return tab, col
        return None

    def _identify_nonnull_count_column(self, query, ri, base_val, candidates):
        """For a NON-distinct count, distinguish COUNT(col) on a nullable column
        from COUNT(*): inject a NULL into the candidate column on a witness-copy
        row. COUNT(*) counts the row regardless (count rises); COUNT(col) skips
        the null (count unchanged). A survival guard (a non-null fresh value in
        the same column MUST be counted) rules out the false positive where the
        crafted row was simply dropped. Filtered columns are skipped because a
        filter would reject the NULL and drop the row spuriously."""
        for tab, col in candidates:
            if (tab, col) in self.filter_attrib_dict:
                continue
            cnt_null = self._probe_count_with_col(query, ri, tab, col, 'null')
            if cnt_null is None or cnt_null != base_val:
                continue  # null was counted (COUNT(*) / wrong col) or unreadable
            cnt_fresh = self._probe_count_with_col(query, ri, tab, col, 'fresh')
            if cnt_fresh is not None and cnt_fresh == base_val + 1:
                return tab, col  # row survives non-null, but its NULL isn't counted -> COUNT(col)
        return None

    def _probe_count_with_col(self, query, ri, tab, col, mode):
        """Reset to D¹, craft a single witness-copy row of ``tab`` whose only
        change is column ``col`` set to a fresh distinct value (``mode='fresh'``)
        or SQL NULL (``mode='null'``), insert it, run Qh, and return the count at
        result index ``ri``. Returns None if the probe cannot be set up (column
        absent, or no fresh value craftable e.g. an ``=`` filter)."""
        self.do_init()
        attribs = self.global_all_attribs[tab]
        if col not in attribs:
            return None
        idx = attribs.index(col)
        row = [self.get_dmin_val(a, tab) for a in attribs]
        if mode == 'fresh':
            v = row[idx]
            w = self.get_different_s_val(col, tab, v)
            if w == v:
                return None  # cannot craft a distinct value (single-valued under filter)
            row[idx] = w
        else:  # 'null'
            row[idx] = None
        att_order = "(" + ",".join(attribs) + ")"
        self.insert_attrib_vals_into_table(att_order, attribs, [tuple(row)], tab)
        res = self.app.doJob(query)
        rows = self.app.get_all_nullfree_rows(res) if self.app.done else []
        return _max_int_in_result_col(rows, ri)

    def insert_for_inner(self, a, attrib, b, k_value, key_list, tabname, temp_vals, result_index):
        max_no_of_rows = 0
        # For this table (tabname) and this attribute (attrib), fill all tables now
        for tabname_inner in self.core_relations:
            attrib_list_inner = self.global_all_attribs[tabname_inner]
            insert_rows = []
            no_of_rows = get_no_of_rows(attrib_list_inner, k_value, key_list, tabname, tabname_inner, result_index,
                                        self.dependencies)

            if no_of_rows > max_no_of_rows:
                max_no_of_rows = no_of_rows

            self.logger.debug("tabname ", tabname, " tabname_inner ", tabname_inner, " no_of_rows ", no_of_rows)
            attrib_list_str = ",".join(attrib_list_inner)
            att_order = f"({attrib_list_str})"

            for k in range(no_of_rows):
                insert_values = []

                for attrib_inner in attrib_list_inner:
                    datatype = self.get_datatype((tabname_inner, attrib_inner))
                    if (attrib_inner == attrib or attrib_inner in key_list) and k == no_of_rows - 1:
                        insert_values.append(b)
                    elif attrib_inner == attrib or attrib_inner in key_list:
                        insert_values.append(a)
                    elif datatype == 'date':
                        # check for filter
                        if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
                            date_val = self.filter_attrib_dict[(tabname_inner, attrib_inner)][0]
                            date_val = get_boundary_value(date_val, is_ub=False)
                        else:
                            date_val = get_val_plus_delta('date', get_dummy_val_for('date'), 2)
                        insert_values.append(ast.literal_eval(get_format('date', date_val)))
                    elif datatype in NUMBER_TYPES:
                        # check for filter
                        if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
                            number_val = self.filter_attrib_dict[(tabname_inner, attrib_inner)][0]
                            mini, maxi = get_boundary_value(number_val, is_ub=False), get_boundary_value(number_val, is_ub=True)
                            dummy_val = get_dummy_val_for('int')
                            number_val = min(max(mini, dummy_val), maxi)
                        else:
                            number_val = get_dummy_val_for('int')
                        insert_values.append(number_val)
                    else:
                        # check for filter
                        if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
                            attrib_val = self.get_s_val_for_textType(attrib_inner, tabname_inner)
                            plus_val = attrib_val.replace('%', '')
                        else:
                            plus_val = get_char(get_val_plus_delta('char', get_dummy_val_for('char'), 2))
                        insert_values.append(plus_val)
                    self.logger.debug(tabname_inner, attrib_inner, datatype, insert_values[-1])
                insert_rows.append(tuple(insert_values))

            self.logger.debug("Attribute Ordering: ", att_order)  # FOR DEBUG
            self.logger.debug("Rows: ", insert_rows)  # FOR DEBUG
            temp_vals.append(insert_rows)
            self.insert_attrib_vals_into_table(att_order, attrib_list_inner, insert_rows, tabname_inner)
        return max_no_of_rows

    def analyze(self, agg_array, attrib, new_result, result_index):
        self.logger.debug("analyze")
        new_result = list(new_result[0])
        new_result = [x.strip() for x in new_result]
        check_value = round(float(new_result[result_index]), 2) if is_number(new_result[result_index]) \
            else str(new_result[result_index])
        agg_array = [round(x, 2) if isinstance(x, float) else x for x in agg_array]
        j = 0
        while j < len(agg_array) - 1:
            self.logger.debug(str(attrib), " ", agg_array[j])
            self.logger.debug(check_value, " ", agg_array[j + 1])
            if check_value == agg_array[j + 1]:
                self.global_aggregated_attributes[result_index] = (str(attrib), agg_array[j])
                break
            j = j + 2

import ast
import copy
import itertools

import frozenlist as frozenlist

from .dataclass.genPipeline_context import GenPipelineContext
from .dataclass.pgao_context import PGAOcontext
from ...src.core.abstract.GenerationPipeLineBase import GenerationPipeLineBase, get_boundary_value
from ..util.constants import NON_TEXT_TYPES
from ...src.util.utils import get_dummy_val_for, get_val_plus_delta, get_format, get_char


class Limit(GenerationPipeLineBase):
    def __init__(self, connectionHelper, genPipelineCtx: GenPipelineContext,
                 genCtx: PGAOcontext):
        super().__init__(connectionHelper, "Limit", genPipelineCtx)
        self.limit = None
        self.global_groupby_attributes = genCtx.group_by_attrib
        self.joined_attrib_valDict = {}
        self.no_rows = self.connectionHelper.config.limit_limit
        self.rmin_card = genCtx.projection

    def doExtractJob(self, query):
        result = self.doLimitExtractJob(query)
        self.do_init()
        return result

    def doLimitExtractJob(self, query):
        grouping_attribute_values = {}
        pre_assignment = self.__get_pre_assignment()
        gb_tab_attribs = [(self.find_tabname_for_given_attrib(attrib), attrib)
                          for attrib in self.global_groupby_attributes]

        total_combinations = 1
        self.__decide_number_of_rows(gb_tab_attribs, grouping_attribute_values, pre_assignment, total_combinations)

        # When the group-by columns are pre-assigned, the number of *distinct* output rows
        # we can synthesize is hard-bounded by the number of group-key combinations
        # (len(grouping_attribute_values[*]) == total_combinations == self.no_rows). Outside
        # that case (the common no-group-by query) each inserted row is one fresh output row,
        # so the insert budget can run past self.no_rows to confirm a plateau.
        bounded = bool(grouping_attribute_values)

        def probe(m):
            return self.__probe_limit_card(query, m, gb_tab_attribs, grouping_attribute_values)

        self.limit = self.__search_limit(probe, bounded)
        return True

    def __probe_limit_card(self, query, m, gb_tab_attribs, grouping_attribute_values):
        """Black-box probe: reset every working relation to D¹, insert exactly ``m`` matching
        rows into each (so the pre-LIMIT result would have ~m+baseline rows), run Qh and read
        the normalized result cardinality.

        ``fresh = len(result) - rmin_card + 2`` is the same normalization the stage has always
        used: on an ideal D¹ the projection-start result is 2 rows (header + 1 data), and
        union/outer-join may add invalid rows that this offset cancels. With a LIMIT L the
        result saturates at len == L + 1, i.e. fresh == L + 1, so the *plateau value of fresh*
        encodes the limit directly (limit = fresh - 1).

        Returns the normalized cardinality, or ``None`` if the result came back empty.
        """
        # do_init() recreates each relation from the pristine original (dropping every row this
        # stage inserted) and re-lays D¹; it is the only reset that actually removes prior
        # inserts, so it must run before each probe to keep probes independent.
        self.do_init()
        self.joined_attrib_valDict = {}
        for table in self.core_relations:
            attrib_list = self.global_all_attribs[table]
            att_order = f"({','.join(attrib_list)})"
            insert_rows = []
            for k in range(m):
                self.__determine_k_insert_rows(attrib_list, gb_tab_attribs, grouping_attribute_values,
                                               insert_rows, k, table)
            self.insert_attrib_vals_into_table(att_order, attrib_list, insert_rows, table, insert_logger=False)

        new_result = self.app.doJob(query)
        if self.app.isQ_result_empty(new_result):
            return None
        return len(new_result) - self.rmin_card + 2

    def __search_limit(self, probe, bounded):
        """Recover the LIMIT with O(log L) probes.

        Phase 1 (exponential): insert 1, 2, 4, 8, … rows. The normalized cardinality grows
        monotonically with the row count until the LIMIT clips it, after which it plateaus.
        A plateau is confirmed the first time doubling the rows does not raise the cardinality.

        Phase 2 (binary search): between the last still-growing batch and the first plateaued
        batch, binary-search the smallest insert count that already reaches the plateau value.
        This pins the boundary and re-confirms the plateau cardinality.

        The reported limit is ``plateau - 1`` (the existing, validated card→limit formula),
        which is independent of the additive normalization constant. ``None`` means no LIMIT
        was observable within budget.
        """
        ceiling = self.no_rows
        if ceiling < 1:
            return None
        # Insert budget. For the group-bounded case we cannot synthesize more distinct rows than
        # group-key combinations, so the budget equals the ceiling and the top edge is read
        # single-shot. Otherwise we allow one extra doubling (2*ceiling) so a LIMIT as large as
        # the ceiling can still be confirmed by a second, equal probe.
        budget = ceiling if bounded else 2 * ceiling

        sizes = []  # [(m, card)] in growth order; card == -1 marks an empty/degenerate probe
        plateau_card = None
        m = 1
        while True:
            probe_m = min(m, budget)
            card = probe(probe_m)
            self.logger.debug(f"Limit probe: {probe_m} inserted rows -> normalized card {card}")
            if card is not None and sizes and sizes[-1][1] != -1 and card <= sizes[-1][1]:
                # doubling the rows did not increase the result -> the LIMIT is clipping it
                plateau_card = sizes[-1][1]
                sizes.append((probe_m, card))
                break
            sizes.append((probe_m, card if card is not None else -1))
            if probe_m >= budget:
                break
            m *= 2

        if plateau_card is not None:
            hi = sizes[-2][0]                                  # first batch at the plateau
            lo = sizes[-3][0] if len(sizes) >= 3 else 0        # last batch still growing
            while hi - lo > 1:
                mid = (lo + hi) // 2
                card = probe(mid)
                self.logger.debug(f"Limit binary probe: {mid} inserted rows -> normalized card {card}")
                if card is not None and card >= plateau_card:
                    hi = mid
                else:
                    lo = mid
            self.logger.debug(f"Limit plateau card {plateau_card}; clipping boundary at {hi} inserted rows")
            limit_val = plateau_card - 1
        else:
            # Budget exhausted without a confirmed plateau.
            last_card = sizes[-1][1] if sizes else -1
            if bounded and last_card != -1 and (last_card - 1) < budget:
                # Group-bounded edge: fewer output rows came back than the group rows we supplied,
                # so the LIMIT is clipping at the budget boundary.
                limit_val = last_card - 1
                self.logger.debug(f"Limit inferred at group budget edge: {limit_val}")
            else:
                limit_val = None
                self.logger.debug(f"Result kept growing through budget {budget}; "
                                  f"Limit (if any) exceeds the detectable range.")

        # Limits of 1 or 2 are indistinguishable from the degenerate D¹ baseline (fresh < 4),
        # mirroring the stage's long-standing observability floor.
        if limit_val is not None and limit_val >= 3:
            self.logger.debug(f"Finalized Limit {limit_val}")
            return limit_val
        return None

    def __determine_k_insert_rows(self, attrib_list_inner, gb_tab_attribs, grouping_attribute_values, insert_rows, k,
                                  tabname_inner):
        insert_values = []
        for attrib_inner in attrib_list_inner:
            datatype = self.get_datatype((tabname_inner, attrib_inner))
            if attrib_inner in grouping_attribute_values.keys():
                insert_values.append(grouping_attribute_values[attrib_inner][k])
            elif attrib_inner not in self.joined_attribs \
                    and (tabname_inner, attrib_inner) not in gb_tab_attribs:
                insert_values.append(self.get_dmin_val(attrib_inner, tabname_inner))
            elif datatype in NON_TEXT_TYPES:
                self.insert_non_text_attrib(datatype, attrib_inner, insert_values, k, tabname_inner)
            else:
                self.insert_text_attrib(attrib_inner, insert_values, k, tabname_inner)
        insert_rows.append(tuple(insert_values))

    def __decide_number_of_rows(self, gb_tab_attribs, grouping_attribute_values, pre_assignment, total_combinations):
        if pre_assignment:
            # GET LIMITS FOR ALL GROUPBY ATTRIBUTES
            group_lists = []
            for elt in gb_tab_attribs:
                temp = []
                if elt not in self.filter_attrib_dict.keys():
                    pre_assignment = False
                    break
                datatype = self.get_datatype(elt)
                if datatype in NON_TEXT_TYPES:
                    tot_values = self.__compute_total_values(datatype, elt, total_combinations)
                    self.__get_temp_total_values(datatype, elt, temp, tot_values)
                else:
                    if '%' in self.filter_attrib_dict[elt] or '_' in self.filter_attrib_dict[elt]:
                        pre_assignment = False
                        break
                    else:
                        temp = [self.filter_attrib_dict[elt]]
                total_combinations = total_combinations * len(temp)
                group_lists.append(copy.deepcopy(temp))

            # CREATE DIFFERENT PERMUTATIONS OF GROUPBY COLUMNS VALUE ASSIGNMENTS HERE
            if pre_assignment:
                combo_values = list(itertools.product(*group_lists))
                for elt in self.global_groupby_attributes:
                    grouping_attribute_values[elt] = []
                for elt in combo_values:
                    temp = list(elt)
                    for (val1, val2) in zip(self.global_groupby_attributes, temp):
                        grouping_attribute_values[val1].append(val2)
        if pre_assignment:
            self.no_rows = min(self.no_rows, total_combinations)

    def __get_temp_total_values(self, datatype, elt, temp, tot_values):
        if datatype == 'date':
            for k in range(tot_values):
                date_val = get_val_plus_delta('date', self.filter_attrib_dict[elt][0], k)
                temp.append(ast.literal_eval(get_format('date', date_val)))
        else:
            lb = get_boundary_value(self.filter_attrib_dict[elt][0], is_ub=False)
            for k in range(tot_values):
                temp.append(lb + k)

    def __compute_total_values(self, datatype, elt, total_combinations):
        tot_values = 0
        for in_pred in self.filter_in_predicates:
            if (in_pred[0], in_pred[1]) == elt:
                value_range = in_pred[3]
                for v in value_range:
                    if not isinstance(v, tuple):
                        tot_values += 1
                    else:
                        tot_values += v[-1] - v[0]
        if not tot_values:
            if datatype == 'date':
                tot_values = (self.filter_attrib_dict[elt][1] - self.filter_attrib_dict[elt][0]).days + 1
            else:
                tot_values = self.filter_attrib_dict[elt][1] - self.filter_attrib_dict[elt][0] + 1
        if (total_combinations * tot_values) > self.no_rows:
            i = 1
            while (total_combinations * i) < self.no_rows + 1 and i < tot_values:
                i = i + 1
            tot_values = i
        return tot_values

    def insert_text_attrib(self, attrib_inner, insert_values, k, tabname_inner):
        char_val = get_char(get_val_plus_delta('char', get_dummy_val_for('char'), k))  # why need unique values?
        if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
            s_val_text = self.get_s_val_for_textType(attrib_inner, tabname_inner)
            temp = copy.deepcopy(s_val_text)
            if '_' in s_val_text:
                insert_values.append(temp.replace('_', char_val))
            else:
                insert_values.append(temp.replace('%', char_val, 1))
            insert_values[-1].replace('%', '')
        else:
            insert_values.append(char_val)

    def insert_non_text_attrib(self, datatype, attrib_inner, insert_values, k, tabname_inner):
        for edge in self.global_join_graph:
            if attrib_inner in edge:
                edge_key = frozenlist.FrozenList(edge)
                edge_key.freeze()
                if edge_key in self.joined_attrib_valDict.keys() \
                        and k < len(self.joined_attrib_valDict[edge_key]):
                    s_val_plus_k = self.joined_attrib_valDict[edge_key][k]
                    insert_values.append(ast.literal_eval(get_format(datatype, s_val_plus_k)))
                    return

        if datatype != 'date':
            datatype = 'int'
        # check for filter (#MORE PRECISION CAN BE ADDED FOR NUMERIC#)
        s_val = self.get_s_val(attrib_inner, tabname_inner)
        s_val_plus_k = get_val_plus_delta(datatype, s_val, k)
        if (tabname_inner, attrib_inner) in self.filter_attrib_dict.keys():
            one = self.filter_attrib_dict[(tabname_inner, attrib_inner)][1]
            one = get_boundary_value(one, is_ub=True)
            s_val_plus_k = min(s_val_plus_k, one)
        insert_values.append(ast.literal_eval(get_format(datatype, s_val_plus_k)))
        for edge in self.global_join_graph:
            if attrib_inner in edge:
                edge_key = frozenlist.FrozenList(edge)
                edge_key.freeze()
                if edge_key in self.joined_attrib_valDict.keys():
                    self.joined_attrib_valDict[edge_key].append(s_val_plus_k)
                else:
                    self.joined_attrib_valDict[edge_key] = [s_val_plus_k]

    def __get_pre_assignment(self):
        pre_assignment = True
        for elt in self.global_groupby_attributes:
            if elt in self.joined_attribs:
                pre_assignment = False
                break
        if not self.global_groupby_attributes:
            pre_assignment = False
        return pre_assignment

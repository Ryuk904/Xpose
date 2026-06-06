import copy
from _decimal import Decimal

from frozenlist._frozenlist import FrozenList

from .aoa_utils import remove_item_from_list, find_tables_from_predicate, get_tab, get_attrib, \
    get_LB, get_op, get_UB, add_item_to_list
from ..core.factory.ExecutableFactory import ExecutableFactory
from ..util.Log import Log
from ..util.constants import COUNT, COUNT_STAR, COUNT_DISTINCT, SUM, max_str_len, AVG, MIN, MAX, ORPHAN_COLUMN
from ..util.utils import get_format, get_min_and_max_val


def append_clause(output, clause, param):
    if param is not None and param != '':
        output = f"{output} \n {clause} {param}"
    return output


class QueryDetails:
    def __init__(self):
        self.core_relations = []
        # Phase 6: alias-aware FROM rendering. When `instances` is set, the FROM
        # clause is emitted as `base alias` per instance; otherwise we fall back
        # to bare `core_relations` (single-instance behaviour, unchanged).
        self.instances = None
        self.alias_to_table = None
        # Phase 6+ : columns visible under each synthetic alias, used to
        # qualify bare SELECT cols when multiple aliases of the same base
        # table are in FROM (Postgres would otherwise flag the col as
        # ambiguous). {alias_name -> tuple_of_col_names}.
        self.cols_by_alias = None

        self.eq_join_predicates = []
        self.filter_in_predicates = []
        self.arithmetic_filters = []
        self.filter_not_in_predicates = []
        self.aoa_less_thans = []
        self.all_aoa = []
        self.join_edges = []
        self.or_predicates = []
        # (tab, attr) -> [(lb1, ub1), (lb2, ub2), ...] for gap-aware extraction.
        # Set from Filter.disjunctive_ranges before render. When non-empty for
        # a given (tab, attr), render emits an OR of BETWEEN clauses from this
        # dict instead of the (envelope-collapsed) tuple in arithmetic_filters.
        self.disjunctive_ranges = {}

        # WI-36: uncorrelated EXISTS / NOT EXISTS gates. Each entry is
        # {'tab': <relation>, 'kind': 'EXISTS'|'NOT EXISTS'}. The pipeline pulls
        # the gate relation OUT of core_relations / instances (so it never reaches
        # FROM) and declares it here; render emits a `<kind> (SELECT 1 FROM tab
        # [WHERE <inner preds>])` conjunct in the WHERE clause and excludes the
        # gate relation's own filter predicates from the outer WHERE.
        self.exists_gates = []

        self.projection_names = []
        self.global_projected_attributes = []
        self.global_groupby_attributes = []
        self.global_aggregated_attributes = []
        self.global_key_attributes = []

        self.select_op = ''
        self.from_op = ''
        self.where_op = ''
        self.group_by_op = ''
        self.order_by_op = ''
        self.limit_op = ''

    def makeCopy(self, other):
        self.core_relations = other.core_relations
        self.eq_join_predicates = other.eq_join_predicates
        self.filter_in_predicates = other.filter_in_predicates
        self.arithmetic_filters = other.arithmetic_filters
        self.all_aoa = other.all_aoa
        self.join_edges = other.join_edges
        self.projection_names = other.projection_names
        self.global_projected_attributes = other.global_projected_attributes
        self.global_groupby_attributes = other.global_groupby_attributes
        self.global_aggregated_attributes = other.global_aggregated_attributes

    def add_to_where_op(self, predicate):
        if self.where_op and predicate not in self.where_op:
            self.where_op = f'{self.where_op} and {predicate}'
        else:
            self.where_op = predicate

    def assembleQuery(self):
        output = ""
        output = append_clause(output, "Select", self.select_op)
        output = append_clause(output, "From", self.from_op)
        output = append_clause(output, "Where", self.where_op)
        output = append_clause(output, "Group By", self.group_by_op)
        output = append_clause(output, "Order By", self.order_by_op)
        output = append_clause(output, "Limit", self.limit_op)
        output = f"{output};"
        return output

    def optimize_arithmetic_filters(self):
        to_remove = []
        for ar_fil in self.arithmetic_filters:
            for fl_in in self.filter_in_predicates:
                if get_tab(ar_fil) == get_tab(fl_in) and get_attrib(ar_fil) == get_attrib(fl_in):
                    op = get_op(ar_fil)
                    if op in ['=', 'equal'] and get_LB(ar_fil) in get_LB(fl_in):
                        to_remove.append(ar_fil)
                    elif op == 'range' and (get_LB(ar_fil), get_UB(ar_fil)) in get_LB(fl_in):
                        to_remove.append(ar_fil)
        for t_r in to_remove:
            self.arithmetic_filters.remove(t_r)


def get_formatted_value(datatype, value):
    if isinstance(value, FrozenList):
        v_list = list(value)
        f_value = f"{', '.join(v_list)}"
        if len(v_list) > 1:
            f_value = f"({f_value})"
    elif isinstance(value, list):
        f_value = f"{', '.join(value)}"
        if len(value) > 1:
            f_value = f"({f_value})"
    else:
        f_value = get_format(datatype, value)
    return f_value


def get_join_nodes_from_edge(edge):
    nodes = edge.split("=")
    left_node = nodes[0].split(".")
    right_node = nodes[1].split(".")
    left = (left_node[0].strip(), left_node[1].strip())
    right = (right_node[0].strip(), right_node[1].strip())
    return left, right


class QueryStringGenerator:
    ROJ = ' RIGHT OUTER JOIN '
    LOJ = ' LEFT OUTER JOIN '
    join_map = {('l', 'l'): ' INNER JOIN ', ('l', 'h'): ROJ,
                ('h', 'l'): LOJ, ('h', 'h'): ' FULL OUTER JOIN '}

    AGGREGATES = [SUM, AVG, MIN, MAX, COUNT, COUNT_DISTINCT]

    def __init__(self, connectionHelper):
        self.connectionHelper = connectionHelper
        exeFactory = ExecutableFactory()
        self.app = exeFactory.create_exe(self.connectionHelper)
        self.__get_datatype = None
        self._queries = {}
        self._workingCopy = QueryDetails()
        self.logger = Log("Query String Generator", connectionHelper.config.log_level)

    def reset(self):
        self._workingCopy = QueryDetails()

    @property
    def filter_predicates(self):
        return self._workingCopy.arithmetic_filters

    @filter_predicates.setter
    def filter_predicates(self, value):
        if value not in self._workingCopy.arithmetic_filters:
            self._workingCopy.arithmetic_filters.append(value)

    @property
    def disjunctive_ranges(self):
        return self._workingCopy.disjunctive_ranges

    @disjunctive_ranges.setter
    def disjunctive_ranges(self, value):
        if value:
            self._workingCopy.disjunctive_ranges = dict(value)
        else:
            self._workingCopy.disjunctive_ranges = {}

    @property
    def exists_gates(self):
        return self._workingCopy.exists_gates

    @exists_gates.setter
    def exists_gates(self, value):
        # WI-36: list of {'tab', 'kind'} declared by the pipeline after it
        # reclassifies a non-projecting / non-joining / non-scaling core
        # relation as an uncorrelated EXISTS gate.
        self._workingCopy.exists_gates = list(value) if value else []

    @property
    def select_op(self):
        return self._workingCopy.select_op

    @select_op.setter
    def select_op(self, value):
        self._workingCopy.select_op = value

    @property
    def from_op(self):
        return self._workingCopy.from_op

    @from_op.setter
    def from_op(self, value):
        self._workingCopy.from_op = value

    @property
    def where_op(self):
        return self._workingCopy.where_op

    @where_op.setter
    def where_op(self, value):
        self._workingCopy.where_op = value

    @property
    def get_datatype(self):
        return self.__get_datatype

    @get_datatype.setter
    def get_datatype(self, func):
        self.__get_datatype = func

    @property
    def from_clause(self):
        return self._workingCopy.core_relations

    @from_clause.setter
    def from_clause(self, core_relations):
        self._workingCopy.core_relations = core_relations
        self._workingCopy.core_relations.sort()

    @property
    def instances(self):
        return self._workingCopy.instances

    @instances.setter
    def instances(self, value):
        # Phase 6: receive the alias-aware FROM model from the pipeline.
        self._workingCopy.instances = list(value) if value else None

    @property
    def alias_to_table(self):
        return self._workingCopy.alias_to_table

    @alias_to_table.setter
    def alias_to_table(self, value):
        self._workingCopy.alias_to_table = dict(value) if value else None

    @property
    def cols_by_alias(self):
        return self._workingCopy.cols_by_alias

    @cols_by_alias.setter
    def cols_by_alias(self, value):
        # Pipeline derives this from global_alias_row_dict; render uses it
        # to qualify SELECT cols belonging to multi-instance tables.
        self._workingCopy.cols_by_alias = dict(value) if value else None

    @property
    def algebraic_predicates(self):
        return NotImplementedError

    @algebraic_predicates.setter
    def algebraic_predicates(self, aoa):
        self._workingCopy.eq_join_predicates = aoa.algebraic_eq_predicates
        for pred in aoa.aoa_less_thans:
            self._workingCopy.all_aoa.append((pred[0], '<', pred[1]))
        for pred in aoa.aoa_predicates:
            self._workingCopy.all_aoa.append((pred[0], '<=', pred[1]))
        self._workingCopy.arithmetic_filters = aoa.arithmetic_eq_predicates + aoa.arithmetic_ineq_predicates

    @property
    def limit(self):
        return NotImplementedError

    @limit.setter
    def limit(self, lm_obj):
        self._workingCopy.limit_op = str(lm_obj.limit) if lm_obj.limit is not None else ''

    @property
    def pgaoCtx(self):
        return NotImplementedError

    @pgaoCtx.setter
    def pgaoCtx(self, value):
        self._workingCopy.global_key_attributes = value.joined_attribs
        self._workingCopy.projection_names = value.projection_names
        self._workingCopy.global_aggregated_attributes = value.aggregated_attributes
        self._workingCopy.global_groupby_attributes = value.group_by_attrib
        self._workingCopy.global_projected_attributes = value.projected_attribs
        self._workingCopy.order_by_op = value.orderby_string

    @property
    def arithmetic_disjunctions(self):
        return NotImplementedError

    @arithmetic_disjunctions.setter
    def arithmetic_disjunctions(self, remnants):
        if isinstance(remnants, tuple):
            if remnants not in self._workingCopy.filter_in_predicates:
                self._workingCopy.filter_in_predicates.append(remnants)
                tab, attrib, op, neg_val = remnants[0], remnants[1], remnants[2], remnants[3]
                self._workingCopy.arithmetic_filters.remove((tab, attrib, 'range',
                                                             min(neg_val[0][0], neg_val[1][0]),
                                                             max(neg_val[1][1], neg_val[0][1])))
        else:
            for pred in remnants.filter_in_predicates:
                if pred not in self._workingCopy.filter_in_predicates:
                    self._workingCopy.filter_in_predicates.append(pred)
        self._workingCopy.optimize_arithmetic_filters()

    @property
    def all_arithmetic_filters(self):
        uniq_preds = list(set(self._workingCopy.arithmetic_filters
                              + self._workingCopy.filter_in_predicates
                              + self._workingCopy.filter_not_in_predicates))
        return uniq_preds

    @all_arithmetic_filters.setter
    def all_arithmetic_filters(self, value):
        raise NotImplementedError

    @property
    def algebraic_inequalities(self):
        return self._workingCopy.all_aoa

    @algebraic_inequalities.setter
    def algebraic_inequalities(self, value):
        raise NotImplementedError

    @property
    def join_edges(self):
        return self._workingCopy.join_edges

    @join_edges.setter
    def join_edges(self, value):
        self._workingCopy.join_edges = value
        self._workingCopy.eq_join_predicates.clear()  # when join edges are assigned directly, old equi join
        # predicates are obsolete

    def rectify_projection(self, replace_dict):
        for key in replace_dict.keys():
            if key not in self._workingCopy.global_groupby_attributes:
                continue
            self._workingCopy.global_groupby_attributes[self._workingCopy.global_groupby_attributes.index(key)] \
                = replace_dict[key]
            self._workingCopy.order_by_op.replace(key, replace_dict[key])
            self._workingCopy.global_projected_attributes[self._workingCopy.global_projected_attributes.index(key)] \
                = replace_dict[key]

        agg_replace_dict = {}
        for i, agg_tuple in enumerate(self._workingCopy.global_aggregated_attributes):
            attrib = agg_tuple[0]
            if attrib in replace_dict.keys():
                replace_attrib = replace_dict[attrib]
                agg_replace_dict[i] = (replace_attrib, agg_tuple[1])
        for key in agg_replace_dict.keys():
            self._workingCopy.global_aggregated_attributes[key] = agg_replace_dict[key]

        return self._workingCopy.global_projected_attributes, self._workingCopy.global_groupby_attributes, \
            self._workingCopy.global_aggregated_attributes, self._workingCopy.order_by_op

    def updateExtractedQueryWithNEPVal(self, query, val):
        for elt in val:
            tab, attrib, op, neg_val = elt[0], elt[1], elt[2], elt[3]
            datatype = self.get_datatype((tab, attrib))
            if op == '<>':
                predicate, predicate_tuple = self._extract_nep_op_info(attrib, datatype, elt, neg_val, op, query, tab)
                self.filter_predicates = predicate_tuple
            elif op == 'IN':
                predicate = f"{tab}.{attrib} between {neg_val[0][0]} and {neg_val[0][1]} OR {tab}.{attrib} between {neg_val[1][0]} and {neg_val[1][1]}"
                self.arithmetic_disjunctions = elt
            else:
                predicate = ""

            self._workingCopy.add_to_where_op(predicate)

        Q_E = self.write_query()
        return Q_E

    def _extract_nep_op_info(self, attrib, datatype, elt, neg_val, op, query, tab):
        format_val = get_format(datatype, neg_val)
        if datatype == 'str':
            output = self._getStrFilterValue(query, elt[0], elt[1], elt[3], max_str_len)
            self.logger.debug(output)
            if '%' in output or '_' in output:
                predicate = f"{tab}.{attrib} NOT LIKE '{str(output)}' "
                self._remove_exact_NE_string_predicate(elt)
                predicate_tuple = (tab, attrib, 'NOT LIKE', output)
            else:
                predicate = f"{tab}.{attrib} {str(op)} \'{str(output)}\' "
                predicate_tuple = (tab, attrib, str(op), output)
        else:
            predicate = f"{tab}.{attrib} {str(op)} {format_val}"
            predicate_tuple = (tab, attrib, str(op), format_val)
        return predicate, predicate_tuple

    def __consolidate_nep_filters_for_not_in(self):
        t_remove = []
        nep_dict = {}
        for elt in self._workingCopy.arithmetic_filters:
            if elt[2] not in ['!=', '<>']:
                continue
            key = (elt[0], elt[1], elt[2])
            value = elt[3]
            if key not in nep_dict.keys():
                nep_dict[key] = [value]
            else:
                nep_dict[key].append(value)
        for key in nep_dict.keys():
            value = nep_dict[key]
            datatype = self.get_datatype((key[0], key[1]))
            if len(value) > 1:
                self.logger.debug("NOT IN FOUND..")
                for v in value:
                    t_remove.append((key[0], key[1], key[2], v))
                f_value = FrozenList([get_format(datatype, v) for v in value])
                f_value.freeze()
                self._workingCopy.filter_not_in_predicates.append((key[0], key[1], 'NOT IN', f_value, f_value))
                self.logger.debug(self._workingCopy.filter_not_in_predicates)

        for pred in t_remove:
            self._workingCopy.arithmetic_filters.remove(pred)
        return t_remove

    def rewrite_for_NEP(self):
        t_remove = self.__consolidate_nep_filters_for_not_in()
        for pred in t_remove:
            self._remove_exact_NE_string_predicate(pred)
        for notInPred in self._workingCopy.filter_not_in_predicates:
            pred = self.formulate_predicate_from_filter(notInPred)
            self._workingCopy.add_to_where_op(pred)
        self._workingCopy.where_op = self.__generate_where_clause()
        Q_E = self.write_query()
        return Q_E

    def updateWhereClause(self, predicate):
        self._workingCopy.add_to_where_op(predicate)
        return self.write_query()

    def regenerate_with_disjunctions(self, disjunctive_ranges, carriers=None):
        """Apply within-attribute OR-of-intervals discovered by the post-
        Projection gap pass and re-render the query.

        ``disjunctive_ranges`` is ``{(tab, attr): [(lb, ub), ...]}``. The render
        path (__generate_arithmetic_pure_conjunctions) swaps the enveloping
        'range' carrier tuple in arithmetic_filters for an OR of BETWEEN
        clauses. ``carriers`` is a list of ``(tab, attr, lo, hi)`` envelopes to
        synthesise when Filter emitted none (the both-domain-extremes case,
        e.g. ``A < 5 OR A > 20``) -- without a carrier tuple the swap has
        nothing to replace. Regenerates where_op from structured state (like
        rewrite_for_NEP) so the OR expansion runs, then re-writes the query."""
        for carrier in (carriers or []):
            tab, attr, lo, hi = carrier
            if not self._has_range_carrier(tab, attr):
                self._workingCopy.arithmetic_filters.append((tab, attr, 'range', lo, hi))
        self._workingCopy.disjunctive_ranges = dict(disjunctive_ranges) if disjunctive_ranges else {}
        self._workingCopy.where_op = self.__generate_where_clause()
        return self.write_query()

    def _has_range_carrier(self, tab, attr):
        for p in self._workingCopy.arithmetic_filters:
            if (isinstance(p, (tuple, list)) and len(p) >= 5
                    and p[0] == tab and p[1] == attr
                    and str(p[2]).strip().lower() == 'range'):
                return True
        return False

    def __generate_where_clause(self) -> str:
        predicates = []
        if not len(self._workingCopy.join_edges):
            self.__generate_algebraice_eualities(predicates)
        else:
            predicates.extend(self._workingCopy.join_edges)
        self.__generate_algebraic_inequalities(predicates)

        self.__generate_arithmetic_pure_conjunctions(predicates)

        # WI-36: append `<kind> (SELECT 1 FROM gate [WHERE <inner preds>])` for
        # each reclassified uncorrelated EXISTS / NOT EXISTS gate. The gate's
        # own filter predicates were excluded from the outer conjunctions above
        # (they belong inside the subquery) and are rendered here instead.
        self.__generate_exists_gate_clauses(predicates)

        where_clause = "\n and ".join(predicates)
        self.logger.debug(where_clause)
        return where_clause

    def __gate_tables(self) -> set:
        return {g['tab'] for g in (self._workingCopy.exists_gates or [])}

    def __generate_exists_gate_clauses(self, predicates) -> None:
        gates = self._workingCopy.exists_gates or []
        for gate in gates:
            tab = gate['tab']
            kind = gate.get('kind', 'EXISTS')
            inner_preds = self.__collect_gate_inner_predicates(tab)
            sub = f"SELECT 1 FROM {tab}"
            if inner_preds:
                sub = f"{sub} WHERE " + " and ".join(inner_preds)
            predicates.append(f"{kind} ({sub})")

    def __collect_gate_inner_predicates(self, tab) -> list:
        # The gate relation's filter tuples ARE its subquery's WHERE clause.
        # Render them with the same per-predicate renderer used for the outer
        # WHERE so `region.r_regionkey >= 3` resolves against the subquery FROM.
        rendered = []
        source = (self._workingCopy.filter_in_predicates
                  + self._workingCopy.arithmetic_filters
                  + self._workingCopy.filter_not_in_predicates)
        for pred in source:
            if not (isinstance(pred, (tuple, list)) and len(pred) >= 2):
                continue
            if pred[0] != tab:
                continue
            p = self.formulate_predicate_from_filter(pred)
            if p:
                rendered.append(p)
        return rendered

    def formulate_query_string(self):
        # Phase 6: emit `base alias` syntax when the pipeline supplied alias-
        # aware instances. For single-instance tables alias == base so the
        # rendered FROM is identical to today's `", ".join(core_relations)`.
        instances = self._workingCopy.instances
        if instances:
            parts = []
            for inst in instances:
                table, alias = inst.table, inst.alias
                parts.append(table if alias == table else f"{table} {alias}")
            self._workingCopy.from_op = ", ".join(parts)
        else:
            self._workingCopy.from_op = ", ".join(self._workingCopy.core_relations)
        self._workingCopy.where_op = self.__generate_where_clause()
        self.generate_groupby_select()
        eq = self.write_query()
        return eq

    def generate_groupby_select(self):
        self.__generate_group_by_clause()
        self.__generate_select_clause()

    def backup_query_before_new_generation(self, ref_query=None):  # make new query from the last memory
        lastQueryDetails = QueryDetails()
        lastQueryDetails.makeCopy(self._workingCopy)
        last_query = self.formulate_query_string()  # take backup of current working copy
        if ref_query is not None:
            ref_details = self._queries[hash(ref_query)][1]
            self._workingCopy.makeCopy(ref_details)
        return last_query

    def write_query(self) -> str:
        self.logger.debug(f"Select: {self._workingCopy.select_op}")
        self.logger.debug(f"From: {self._workingCopy.from_op}")
        self.logger.debug(f"Where: {self._workingCopy.where_op}")
        self.logger.debug(f"Group by: {self._workingCopy.group_by_op}")
        self.logger.debug(f"Order by: {self._workingCopy.order_by_op}")
        self.logger.debug(f"Limit: {self._workingCopy.limit_op}")

        query_string = self._workingCopy.assembleQuery()
        key = hash(query_string)
        self.logger.debug("hash key: ", key)
        if key not in self._queries:
            self._queries[key] = (query_string, copy.deepcopy(self._workingCopy))
        self.logger.debug("query_dict: ", self._queries)
        return query_string

    def __generate_predicate_string_for_in_operator(self, tab, attrib, values):
        datatype = self.get_datatype((tab, attrib))
        predicates = []
        single_value_set = []
        for v in values:
            if isinstance(v, tuple):
                elt = [tab, attrib, 'range', v[0], v[1]]
                predicates.append(self.formulate_predicate_from_filter(elt))
            else:
                single_value_set.append(get_format(datatype, v))
        f_values = get_formatted_value(datatype, single_value_set)
        op = 'IN' if len(single_value_set) > 1 else '='
        if len(single_value_set):
            predicates.append(f"{tab}.{attrib} {op} {f_values}")
        return " OR ".join(predicates)

    def formulate_predicate_from_filter(self, elt):
        tab, attrib, op, lb, ub = elt[0], elt[1], str(elt[2]).strip().lower(), elt[3], elt[-1]
        if op == 'in':
            predicate = self.__generate_predicate_string_for_in_operator(tab, attrib, lb)
            predicate = f"({predicate})" if "OR" in predicate else predicate
            return predicate
        datatype = self.get_datatype((tab, attrib))
        f_lb = get_formatted_value(datatype, lb)
        f_ub = get_formatted_value(datatype, ub)

        if op == 'range':
            predicate = ''
            i_min, i_max = get_min_and_max_val(datatype)
            if datatype == 'numeric':
                f_lb, f_ub = round(Decimal(lb), 2), round(Decimal(ub), 2)
                i_min, i_max = round(Decimal(i_min), 2), round(Decimal(i_max), 2)
            if lb == ub:
                predicate = f"{tab}.{attrib} = {f_lb}"
            if lb <= i_min:
                predicate = f"{tab}.{attrib} <= {f_ub}"
            if ub >= i_max:
                predicate = f"{tab}.{attrib} >= {f_lb}"
            if predicate == '':
                predicate = f"{tab}.{attrib} between {f_lb} and {f_ub}"
            if lb <= i_min and ub >= i_max:
                predicate = ''
        elif op == '>=':
            predicate = f"{tab}.{attrib} {op} {f_lb}"
        elif op in ['<=', '=', 'equal', 'like', 'not like', '<>', '!=', 'not in']:
            predicate = f"{tab}.{attrib} {str(op.replace('equal', '=')).upper()} {f_ub}"
        else:
            predicate = ''
        return predicate

    def __generate_algebraice_eualities(self, predicates):
        for eq_join in self._workingCopy.eq_join_predicates:
            self.logger.debug(f"Creating join clause for {eq_join}")
            join_edge = list(f"{item[0]}.{item[1]}" for item in eq_join if len(item) == 2)
            join_edge.sort()
            predicates.extend(f"{join_edge[i]} = {join_edge[i + 1]}" for i in range(len(join_edge) - 1))
        self.logger.debug(predicates)
        self._workingCopy.join_edges = copy.deepcopy(predicates)

    def __generate_algebraic_inequalities(self, predicates):
        for aoa in self._workingCopy.all_aoa:
            predicates.append(self.get_aoa_string(aoa))

    def __generate_arithmetic_pure_conjunctions(self, predicates):
        # self._workingCopy.optimize_arithmetic_filters()

        apc_predicates = self._workingCopy.filter_in_predicates \
                         + self._workingCopy.arithmetic_filters \
                         + self._workingCopy.filter_not_in_predicates

        # For (tab, attr) with a gap-aware disjunction recorded in
        # disjunctive_ranges, swap the envelope range tuple in apc_predicates
        # for an OR of BETWEEN clauses built from the per-interval list.
        # Everything else renders the standard way.
        disj_ranges = getattr(self._workingCopy, 'disjunctive_ranges', None) or {}
        rendered_disjunctions = set()

        # WI-36: a reclassified EXISTS-gate relation's own filter predicates
        # belong inside its subquery, not in the outer WHERE (the relation is
        # no longer in FROM, so an outer `gate.col ...` reference would be an
        # invalid missing-FROM-entry). Skip them here; __generate_exists_gate_clauses
        # renders them inside the EXISTS subquery.
        gate_tabs = self.__gate_tables()

        for a_eq in apc_predicates:
            if gate_tabs and isinstance(a_eq, (tuple, list)) and len(a_eq) >= 1 and a_eq[0] in gate_tabs:
                continue
            is_range_tuple = (isinstance(a_eq, (tuple, list)) and len(a_eq) >= 5
                              and str(a_eq[2]).strip().lower() == 'range')
            key = (a_eq[0], a_eq[1]) if is_range_tuple else None
            if key is not None and key in disj_ranges and key not in rendered_disjunctions:
                rendered_disjunctions.add(key)
                parts = []
                for (lb, ub) in disj_ranges[key]:
                    sub_tuple = (a_eq[0], a_eq[1], 'range', lb, ub)
                    p = self.formulate_predicate_from_filter(sub_tuple)
                    if p:
                        parts.append(p)
                if not parts:
                    continue
                pred = parts[0] if len(parts) == 1 else '(' + ' OR '.join(parts) + ')'
                add_item_to_list(pred, predicates)
                continue
            if key is not None and key in rendered_disjunctions:
                continue  # skip the envelope tuple for an already-rendered disjunction
            pred = self.formulate_predicate_from_filter(a_eq)
            add_item_to_list(pred, predicates)

    def __optimize_group_by_attributes(self):
        for i in range(len(self._workingCopy.global_projected_attributes)):
            attrib = self._workingCopy.global_projected_attributes[i]
            if (attrib in self._workingCopy.global_key_attributes
                    and attrib in self._workingCopy.global_groupby_attributes):
                agg_op = self._workingCopy.global_aggregated_attributes[i][1]
                if agg_op not in self.AGGREGATES:
                    self._workingCopy.global_aggregated_attributes[i] = (
                        self._workingCopy.global_aggregated_attributes[i][0], '')
        temp_list = copy.deepcopy(self._workingCopy.global_groupby_attributes)
        for attrib in temp_list:
            if attrib not in self._workingCopy.global_projected_attributes:
                remove_item_from_list(attrib, self._workingCopy.global_groupby_attributes)
                continue
            remove_flag = True
            for elt in self._workingCopy.global_aggregated_attributes:
                if elt[0] == attrib and elt[1] not in self.AGGREGATES:
                    remove_flag = False
                    break
            if remove_flag:
                remove_item_from_list(attrib, self._workingCopy.global_groupby_attributes)

    def __generate_select_clause(self):
        # When multi-instance aliases are in play (self-join), an unqualified
        # SELECT col is ambiguous to Postgres because the same column exists
        # in multiple FROM-aliases. Build attr→alias from any alias-tagged
        # predicate already in this query (equi-join groups, filters) and use
        # the first match to qualify bare SELECT cols.
        alias_for_attr = self._build_alias_for_attr_map()

        for i in range(len(self._workingCopy.global_projected_attributes)):
            elt = self._workingCopy.global_projected_attributes[i]
            if self._workingCopy.global_aggregated_attributes[i][1] != '':
                if self._workingCopy.global_aggregated_attributes[i][1] == COUNT_STAR:
                    # COUNT(*) is stored as the literal 'Count(*)' (aggregation.py sets
                    # ('', COUNT_STAR) for an empty projected attribute) and is already
                    # valid SQL, so emit it verbatim.
                    elt = self._workingCopy.global_aggregated_attributes[i][1]
                elif self._workingCopy.global_aggregated_attributes[i][1] == COUNT_DISTINCT:
                    # WI-06: COUNT(DISTINCT col). The counted column lives on the
                    # aggregate tuple ([0]); the projected attribute is empty (a
                    # COUNT has no value-dependency), so we take the column from
                    # the aggregate tuple rather than from `elt`.
                    elt = 'Count(distinct ' + self._workingCopy.global_aggregated_attributes[i][0] + ')'
                else:
                    # Every other aggregate wraps its column, INCLUDING column-COUNT whose
                    # label is the bare 'Count' (COUNT constant): e.g. Count(o_orderkey),
                    # Sum(l_quantity). The previous `COUNT in label` substring test wrongly
                    # matched 'Count(*)' too, and for column-COUNT it short-circuited here,
                    # dropping the column and emitting an invalid bare 'Count'.
                    #
                    # WI-06: a column-COUNT (COUNT(col), detected by the nullable-column
                    # null-injection probe) has an EMPTY projected attribute, because a
                    # COUNT has no value-dependency for projection to discover. The
                    # counted column is carried on the aggregate tuple ([0]) instead, so
                    # fall back to it when `elt` is empty. (For SUM/AVG/MIN/MAX the
                    # projected attribute is populated and equals [0], so this is a no-op.)
                    col = elt if elt else self._workingCopy.global_aggregated_attributes[i][0]
                    elt = self._workingCopy.global_aggregated_attributes[i][1] + '(' + col + ')'
            elif elt and elt in alias_for_attr:
                # Bare attribute (no aggregate); qualify with an alias of one
                # of the FROM-instances that has this attribute, so Postgres
                # can resolve it unambiguously.
                elt = f"{alias_for_attr[elt]}.{elt}"

            if elt != self._workingCopy.projection_names[i] and self._workingCopy.projection_names[i] != '':
                if self._workingCopy.projection_names[i] == ORPHAN_COLUMN:
                    elt = elt
                else:
                    elt = elt + ' as ' + self._workingCopy.projection_names[i]
            self._workingCopy.select_op = elt if not i else f'{self._workingCopy.select_op}, {elt}'

    def _build_alias_for_attr_map(self) -> dict:
        alias_for_attr: dict = {}
        ata = self._workingCopy.alias_to_table or {}
        if not any(a != t for a, t in ata.items()):
            return alias_for_attr  # no synthetic aliases active

        def consider(alias, attr):
            if alias in ata and attr and attr not in alias_for_attr:
                alias_for_attr[attr] = alias

        # Primary source: alias dict's schema info — every column of every
        # synthetic alias gets mapped to that alias (first one wins for
        # cross-alias columns, which is fine because the cols are identical
        # between aliases of the same base table).
        cba = self._workingCopy.cols_by_alias or {}
        for alias, cols in cba.items():
            if alias not in ata or ata[alias] == alias:
                continue  # not a synthetic alias
            for col in cols:
                consider(alias, col)

        # Fallback / tightening: also pull from alias-tagged predicates so
        # the first alias we map an attribute to matches the alias used in
        # WHERE-clause references (cosmetic, but keeps emitted SQL tidy).
        for grp in (self._workingCopy.eq_join_predicates or []):
            for member in grp:
                if isinstance(member, tuple) and len(member) == 2:
                    consider(member[0], member[1])
        for pred in (self._workingCopy.arithmetic_filters or []):
            if len(pred) >= 2:
                consider(pred[0], pred[1])
        for pred in (self._workingCopy.filter_in_predicates or []):
            if len(pred) >= 2:
                consider(pred[0], pred[1])
        return alias_for_attr

    def __generate_group_by_clause(self):
        self.__optimize_group_by_attributes()
        for i in range(len(self._workingCopy.global_groupby_attributes)):
            elt = self._workingCopy.global_groupby_attributes[i]
            # UPDATE OUTPUTS
            self._workingCopy.group_by_op = elt if not i else f'{self._workingCopy.group_by_op}, {elt}'

    def _remove_exact_NE_string_predicate(self, elt):
        while elt[1] in self._workingCopy.where_op:
            where_parts = self._workingCopy.where_op.split()
            attrib_index = where_parts.index(f"{elt[0]}.{elt[1]}")

            val = where_parts[attrib_index + 2]
            self.logger.debug(f"=== val: {val} to delete ===")
            if val.startswith("'") and val.endswith("'"):
                where_parts.pop(attrib_index + 2)
            elif val.startswith("'"):
                start_idx = attrib_index + 2
                check_idx = start_idx + 1
                while not where_parts[check_idx].endswith("'"):
                    check_idx += 1
                while check_idx >= start_idx:
                    where_parts.pop(check_idx)
                    check_idx -= 1

            where_parts.pop(attrib_index + 1)  # <>
            where_parts.pop(attrib_index)

            if "and" in where_parts[attrib_index - 1]:
                where_parts.pop(attrib_index - 1)  # for and
            self._workingCopy.where_op = " ".join(where_parts)

    def _getStrFilterValue(self, query, tabname, attrib, representative, max_length):
        representative = self.__get_minimal_representative_str(attrib, query, representative, tabname)
        output = self.__handle_for_wildcard_char_underscore(attrib, query, representative, tabname)
        self.logger.debug(f"rep: {representative}, handling _: {output}")
        if output == '':
            return output
        output = self.__handle_for_wildcard_char_perc(attrib, max_length, output, query, tabname)
        return output

    def __handle_for_wildcard_char_perc(self, attrib, max_length, output, query, tabname):
        # GET % positions
        index = 0
        representative = copy.deepcopy(output)
        self.logger.debug(representative)
        if len(representative) < max_length:
            output = ""

            while index < len(representative):
                temp = list(representative)
                if temp[index] == 'a':
                    temp.insert(index, 'b')
                else:
                    temp.insert(index, 'a')
                temp = ''.join(temp)
                output = self.__try_with_temp(attrib, output, query, tabname, temp)
                output = output + representative[index]
                index = index + 1

            temp = list(representative)
            if temp[index - 1] == 'a':
                temp.append('b')
            else:
                temp.append('a')
            temp = ''.join(temp)
            output = self.__try_with_temp(attrib, output, query, tabname, temp)
        return output

    def __try_with_temp(self, attrib, output, query, tab, temp):
        tabname = f"{self.connectionHelper.config.schema}.{tab}"
        u_query = self.connectionHelper.queries.update_tab_attrib_with_quoted_value(tabname, attrib, temp)
        try:
            self.connectionHelper.execute_sql([u_query], self.logger)
            new_result = self.app.doJob(query)
            if self.app.isQ_result_no_full_nullfree_row(new_result):
                output = output + '%'
        except Exception as e:
            self.logger.debug(e)
            self.connectionHelper.rollback_transaction()
        return output

    def __handle_for_wildcard_char_underscore(self, attrib, query, representative, tab):
        tabname = f"{self.connectionHelper.config.schema}.{tab}"
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
            u_query = self.connectionHelper.queries.update_tab_attrib_with_quoted_value(tabname, attrib, temp)

            try:
                self.connectionHelper.execute_sql([u_query])
                new_result = self.app.doJob(query)
                if self.app.isQ_result_empty(new_result):
                    temp = copy.deepcopy(representative)
                    temp = temp[:index] + temp[index + 1:]

                    u_query = self.connectionHelper.queries.update_tab_attrib_with_quoted_value(tabname, attrib, temp)
                    try:
                        self.connectionHelper.execute_sql([u_query])
                        new_result = self.app.doJob(query)
                        if self.app.isQ_result_no_full_nullfree_row(new_result):
                            representative = representative[:index] + representative[index + 1:]
                        else:
                            output = output + "_"
                            representative = list(representative)
                            representative[index] = u"\u00A1"
                            representative = ''.join(representative)
                    except Exception as e:
                        self.logger.debug(e)
                        output = output + "_"
                        representative = list(representative)
                        representative[index] = u"\u00A1"
                        representative = ''.join(representative)
                else:
                    output = output + representative[index]
            except Exception as e:
                self.logger.debug(e)
                output = output + representative[index]

            index = index + 1
        return output

    def __get_minimal_representative_str(self, attrib, query, representative, tab):
        tabname = f"{self.connectionHelper.config.schema}.{tab}"
        index = 0
        output = ""
        temp = list(representative)
        while index < len(representative):
            temp[index] = ''
            temp_str = ''.join(temp)
            u_query = self.connectionHelper.queries.update_tab_attrib_with_quoted_value(tabname, attrib, temp_str)
            try:
                self.connectionHelper.execute_sql([u_query])
                new_result = self.app.doJob(query)
                if self.app.isQ_result_no_full_nullfree_row(new_result):
                    pass
                else:
                    output = output + representative[index]
                    temp[index] = representative[index]
            except Exception as e:
                self.logger.debug(e)
                output = output + representative[index]
                temp[index] = representative[index]
            index = index + 1
        return output

    def generate_where_clause(self, fp_where):
        for elt in fp_where:
            predicate = self.__generate_where_clause_predicate_str(elt)
            if len(predicate):
                self.where_op = predicate if self.where_op == '' else self.where_op + " and " + predicate

    def generate_from_on_clause(self, edge, fp_on, imp_t1, imp_t2, table1, table2):
        flag_first = True if self._workingCopy.from_op == '' else False
        type_of_join = self.join_map.get((imp_t1, imp_t2))
        join_condition = f"\n\t ON {edge[0][1]}.{edge[0][0]} = {edge[1][1]}.{edge[1][0]}"
        relevant_tables = [table2] if not flag_first else [table1, table2]
        join_part = f"\n{type_of_join} {table2} {join_condition}"
        self.from_op += f" {table1} {join_part}" if flag_first else "" + join_part
        flag_first = False
        for fp in fp_on:
            tables = find_tables_from_predicate(fp)
            if all(tab in relevant_tables for tab in tables):
                predicate = self.__generate_where_clause_predicate_str(fp)
                if len(predicate):
                    self.from_op += "\n\t and " + predicate
        return flag_first

    def __generate_where_clause_predicate_str(self, fp):
        predicate = ''
        if len(fp) == 3:
            predicate = self.get_aoa_string(fp)
        elif len(fp) >= 4:
            predicate = self.formulate_predicate_from_filter(fp)
        return predicate

    def clear_from_where_ops(self):
        self._workingCopy.from_op = ''
        self._workingCopy.where_op = ''

    def get_aoa_string(self, aoa):
        lesser, op, greater = aoa[0], aoa[1], aoa[2]
        tab_attrib = lesser if isinstance(lesser, tuple) else greater
        datatype = self.get_datatype(tab_attrib)
        f_str = ""
        for tup in aoa:
            if isinstance(tup, tuple):
                f_str += f"{tup[0]}.{tup[1]}"
            elif tup in ['<', '<=']:
                f_str += f" {tup} "
            else:
                self.logger.debug(tup)
                f_val = get_format(datatype, tup)
                self.logger.debug(f_val)
                f_str += f_val
        self.logger.debug(f_str)
        return f_str

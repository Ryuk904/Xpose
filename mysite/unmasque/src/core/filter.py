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
        # Side channel for gap-aware extraction: when an attribute's hidden
        # predicate is a disjunction of intervals, filter_predicates carries
        # only the envelope range (so AOA / equi_join see a single contiguous
        # range, which their reasoning assumes), and the actual sub-intervals
        # are recorded here keyed by (tab, attr). Render consults this dict to
        # OR them back together. Empty when no disjunctions are found.
        self.disjunctive_ranges = {}

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
                if self._gap_aware_enabled():
                    intervals = self._refine_with_gap_search(tabname, attrib, 'numeric',
                                                             float(val2), float(val1), query,
                                                             filterAttribs)
                    self._emit_range_intervals(filterAttribs, tabname, attrib,
                                               [(float(lb), float(ub)) for (lb, ub) in intervals])
                else:
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

    # -------------------------------------------------------------------------
    # Gap-aware disjunction refinement.
    #
    # When the hidden predicate on attribute A is a union of disjoint intervals
    # (e.g. A in [10,20] OR A in [30,40]), the standard binary search in
    # get_filter_value over-approximates by jumping over the inner gap and
    # converging on a single envelope [~10,~40]. When both domain endpoints
    # satisfy, handle_point_filter currently emits no predicate at all.
    #
    # The user-proposed Re-Rh witness oracle solves this by materializing the
    # hidden result, then anti-joining trial Re results against it. We use the
    # algorithmically equivalent but operationally simpler approach: recursive
    # midpoint Pop-probing on the single-row D¹. Each "Pop = false" midpoint is
    # a gap witness; gap edges are then found by ordinary binary search.
    # Recursion is depth-bounded against pathological cases where the predicate
    # is true across the full domain.
    # -------------------------------------------------------------------------

    # Explore depth (branching when mid is satisfying — pure speculative search
    # for hidden gaps). Each "sat-mid" recursion forks both halves, so the leaf
    # count is 2^explore_depth. Keep small; 6 → 64 leaves max, sufficient to
    # detect gaps that occupy at least ~1/64 of the searched interval.
    _gap_search_explore_depth = 6
    # Discovery depth (after a gap witness has been found and edges located).
    # Recursing on each remaining sub-interval can surface additional gaps;
    # branching is bounded by the number of true disjuncts, not depth, so we
    # allow more levels here.
    _gap_search_discover_depth = 16

    def _gap_aware_enabled(self):
        return getattr(self.connectionHelper.config, 'detect_gap_aware', False)

    def _refine_with_gap_search(self, tab, attr, datatype, lo, hi, query,
                                filter_predicates_so_far=None):
        """Discover the disjunction structure of A's predicate inside [lo, hi].

        Resolution strategy (v2 → v1 fallback chain):
        1. NEP-style witness loop (`_refine_by_nep_witness`): clone full D
           into working schema, run Re-Rh diff, ctid-bisect to a witness row,
           bisect outward for gap edges, iterate. Robust to attributes not in
           Qh's projection because it reads A from the base-table row.
        2. v1 data-witness sampling (`_refine_by_data_witness`): cheaper when
           full-D access isn't available; fragile when distinct values in D
           don't cover the gap range.
        3. v1 midpoint-bisection (`_find_gaps_recursive`): depth-bounded
           speculative search, last resort.

        `filter_predicates_so_far` is the running list of (tab, attr, op, lb, ub)
        tuples that earlier iterations within this Filter pass have committed.
        It's used to tighten Qe's WHERE so cross-attribute slop doesn't
        masquerade as gaps in A's predicate.
        """
        if not self._gap_aware_enabled():
            return [(lo, hi)]
        try:
            delta, cutoff = get_constants_for(datatype)
        except UnmasqueError:
            return [(lo, hi)]

        # v2 primary: NEP-style witness loop. Returns intervals if at least
        # one gap was discovered; None if full-D access failed, [(lo,hi)] if
        # diff was empty (no gap).
        nep_intervals = self._refine_by_nep_witness(
            tab, attr, datatype, lo, hi, query,
            delta, cutoff, filter_predicates_so_far)
        if nep_intervals is not None and len(nep_intervals) > 1:
            return nep_intervals

        # v1 fallback: data-sampling. Per the witness-in-D assumption (A5-W),
        # gaps in the hidden predicate are represented by actual A-values in D.
        sampled_intervals = self._refine_by_data_witness(tab, attr, datatype, lo, hi, query, delta, cutoff)
        if sampled_intervals is not None and len(sampled_intervals) > 1:
            return sampled_intervals

        # v1 final fallback: midpoint bisection. Bounded recursion so a
        # gap-free interval (or one we can't subdivide finely enough) does
        # not blow up.
        intervals = []
        self._find_gaps_recursive(tab, attr, datatype, lo, hi, query,
                                  intervals, delta, cutoff,
                                  explore_depth=0, discover_depth=0)
        coalesced = self._coalesce_intervals(intervals, datatype, delta)
        if sampled_intervals is not None and len(coalesced) <= 1:
            return sampled_intervals
        if nep_intervals is not None and len(coalesced) <= 1:
            return nep_intervals
        if not coalesced:
            return [(lo, hi)]
        return coalesced

    def _refine_by_nep_witness(self, tab, attr, datatype, lo, hi, query,
                                delta, cutoff, filter_predicates_so_far):
        """v2 entry: NEP-style witness loop. Returns None if full-D access
        isn't available (caller falls through to v1). Returns [(lo,hi)] if no
        gap was found. Otherwise returns the discovered intervals.

        Caller's running filter_predicates_so_far may include ranges for OTHER
        attributes — those tighten Qh's effective domain but don't change our
        envelope on `attr`. We do NOT splice them into Qe today: Qe uses
        SELECT * over the FROM clause and the cross-attribute correlation
        comes implicitly via Qh itself (Qh's WHERE rejects rows that fail
        other attribs). If a future change wants per-attribute envelope
        refinement to be tighter, here is where to AND those into Qe.
        """
        # Bail entirely if the connection/app harness isn't present (offline
        # algorithm tests with stubbed checkAttribValueEffect, or any caller
        # that doesn't construct Filter through the normal pipeline). v1
        # fallbacks will pick up the work.
        try:
            cfg = self.connectionHelper.config
            _ = (cfg.schema, cfg.user_schema)
            _ = self.app
        except Exception:
            return None

        try:
            from ..core.gap_witness import GapWitnessFinder
        except Exception as e:
            try:
                self.logger.debug(f"gap_witness import failed: {e}")
            except Exception:
                pass
            return None

        try:
            finder = GapWitnessFinder(
                self.connectionHelper, self.logger, self.app,
                tab, attr, datatype, self.core_relations)
        except Exception as e:
            try:
                self.logger.debug(f"GapWitnessFinder __init__ failed: {e}")
            except Exception:
                pass
            return None

        try:
            if not finder.setup(query):
                return None
        except Exception as e:
            try:
                self.logger.debug(f"GapWitnessFinder.setup raised: {e}")
            except Exception:
                pass
            try:
                finder.teardown()
            except Exception:
                pass
            return None

        try:
            intervals = [(lo, hi)]
            max_iterations = 16
            for _ in range(max_iterations):
                try:
                    witness = finder.find_witness_value(intervals)
                except Exception as e:
                    self.logger.debug(f"find_witness_value raised: {e}")
                    witness = None
                if witness is None:
                    break
                try:
                    w = get_cast_value(datatype, witness)
                except Exception:
                    break
                new_intervals = self._split_interval_on_witness(
                    intervals, tab, attr, datatype, w, query, cutoff)
                if new_intervals is None or new_intervals == intervals:
                    break
                intervals = new_intervals
            coalesced = self._coalesce_intervals(intervals, datatype, delta)
            return coalesced if coalesced else [(lo, hi)]
        finally:
            try:
                finder.teardown()
            except Exception:
                pass

    def _split_interval_on_witness(self, intervals, tab, attr, datatype,
                                    witness_val, query, cutoff):
        """Find the interval containing witness_val, bisect outward to find
        the gap's [left_sat, right_sat] edges, replace that interval with
        the two surrounding sub-intervals.

        Pre-check: verify Pop(witness_val) is FALSE on D¹ before bisecting.
        The witness comes from a base-table row that the comparator diff
        flagged as "in Qe, not in Qh", but the gap may be in some OTHER
        attribute of that row, not this one. If Pop(witness_val on D¹) is
        true, this attribute doesn't constrain Qh at this value — abort.
        """
        # Locate the interval containing the witness.
        idx = None
        for i, (lb, ub) in enumerate(intervals):
            if (not is_left_less_than_right_by_cutoff(datatype, witness_val, lb, cutoff) and
                not is_left_less_than_right_by_cutoff(datatype, ub, witness_val, cutoff)):
                idx = i
                break
        if idx is None:
            return None
        lb, ub = intervals[idx]

        # Reject false witnesses: the comparator diff finds rows where Qe
        # over-approximates Qh, but a witness row's "gap" attribution may
        # not be on THIS attribute. Mutate D¹'s `attr` to witness_val and
        # check Pop — if Qh still accepts, this attribute is uninvolved.
        try:
            if self.checkAttribValueEffect(query, get_format(datatype, witness_val),
                                            [(tab, attr)]):
                return None
        except Exception:
            return None

        # Largest v in [lb, witness_val] with Pop(v) true → right edge of
        # left sub-interval. Smallest v in [witness_val, ub] with Pop(v) true
        # → left edge of right sub-interval.
        gap_left = self._binsearch_last_sat(tab, attr, datatype, lb, witness_val, query, cutoff)
        gap_right = self._binsearch_first_sat(tab, attr, datatype, witness_val, ub, query, cutoff)
        if gap_left is None or gap_right is None:
            return None
        if not is_left_less_than_right_by_cutoff(datatype, gap_left, gap_right, cutoff):
            # Couldn't bracket a gap — bail out so we don't loop forever.
            return None
        new_intervals = list(intervals)
        del new_intervals[idx]
        new_intervals.insert(idx, (gap_right, ub))
        new_intervals.insert(idx, (lb, gap_left))
        # Drop any zero-width or inverted intervals.
        new_intervals = [(a, b) for (a, b) in new_intervals
                         if not is_left_less_than_right_by_cutoff(datatype, b, a, cutoff)]
        return new_intervals

    def _refine_by_data_witness(self, tab, attr, datatype, lo, hi, query, delta, cutoff):
        """Sample distinct A-values from the original D within [lo, hi],
        probe each on D¹, and build maximal contiguous satisfying intervals
        from the observed sat/unsat pattern. Returns None if no data is
        available; otherwise a list of intervals.

        Bounds are emitted as (min_sat_in_run, max_sat_in_run) for each
        contiguous run of satisfying samples — this is conservative against
        the sampled data, not the true predicate boundary, but does not require
        the precondition-fragile binary-search edge anchoring that previously
        produced inverted intervals when Pop(lo)/Pop(hi) were satisfying."""
        sample_vals = self._fetch_distinct_values(tab, attr, datatype, lo, hi, limit=200)
        if not sample_vals:
            return None
        try:
            sample_vals = sorted(set(sample_vals))
        except TypeError:
            return None

        sat_flags = []
        for v in sample_vals:
            try:
                f_v = get_format(datatype, v)
            except Exception:
                sat_flags.append(False)
                continue
            sat_flags.append(self.checkAttribValueEffect(query, f_v, [(tab, attr)]))

        if not any(sat_flags):
            return [(lo, hi)]
        if all(sat_flags):
            return [(lo, hi)]

        intervals = []
        run_start = None
        run_end = None
        for v, ok in zip(sample_vals, sat_flags):
            if ok:
                if run_start is None:
                    run_start = v
                run_end = v
            else:
                if run_start is not None:
                    intervals.append((run_start, run_end))
                    run_start = None
                    run_end = None
        if run_start is not None:
            intervals.append((run_start, run_end))

        coalesced = self._coalesce_intervals(intervals, datatype, delta)
        return coalesced if coalesced else [(lo, hi)]

    def _fetch_distinct_values(self, tab, attr, datatype, lo, hi, limit=200):
        """Query the original D (pre-minimization) for up to `limit` distinct
        values of `attr` within `[lo, hi]`. Tries each known backup-table
        naming convention; returns [] if none succeed."""
        candidates = self._distinct_value_table_candidates(tab)
        try:
            f_lo = get_format(datatype, lo)
            f_hi = get_format(datatype, hi)
        except Exception:
            return []
        for table_qualified in candidates:
            sql = (f"SELECT DISTINCT {attr} FROM {table_qualified} "
                   f"WHERE {attr} IS NOT NULL "
                   f"AND {attr} >= {f_lo} AND {attr} <= {f_hi} "
                   f"ORDER BY {attr} LIMIT {limit}")
            try:
                res, _ = self.connectionHelper.execute_sql_fetchall(sql)
            except Exception:
                continue
            if res and len(res) > 0:
                return [r[0] for r in res if r[0] is not None]
        return []

    def _distinct_value_table_candidates(self, tab):
        cfg = self.connectionHelper.config
        return [
            f"{cfg.user_schema}.{tab}_unmasque_FromClause",
            f"{cfg.schema}.{tab}_unmasque_View_Minimizer",
            f"{cfg.user_schema}.{tab}",
            f"{cfg.schema}.{tab}",
        ]

    def _find_gaps_recursive(self, tab, attr, datatype, lo, hi, query,
                             intervals, delta, cutoff, explore_depth, discover_depth):
        # Precondition: Pop is true at both `lo` and `hi`.
        if (explore_depth >= self._gap_search_explore_depth or
                discover_depth >= self._gap_search_discover_depth or
                not is_left_less_than_right_by_cutoff(datatype, lo, hi, cutoff)):
            intervals.append((lo, hi))
            return
        mid = get_mid_val(datatype, hi, lo)
        if mid == lo or mid == hi:
            intervals.append((lo, hi))
            return
        mid_sat = self.checkAttribValueEffect(query, get_format(datatype, mid), [(tab, attr)])
        if mid_sat:
            # No positive evidence of a gap here. Increment explore_depth so the
            # speculative both-halves branching is strictly bounded — otherwise
            # an attribute with no actual disjunction would recurse exponentially.
            self._find_gaps_recursive(tab, attr, datatype, lo, mid, query,
                                      intervals, delta, cutoff,
                                      explore_depth + 1, discover_depth)
            self._find_gaps_recursive(tab, attr, datatype, mid, hi, query,
                                      intervals, delta, cutoff,
                                      explore_depth + 1, discover_depth)
            return
        # mid lies in a gap. Find gap edges via standard bisection, then recurse
        # on each surviving side under discover_depth (a real gap was found, so
        # more gaps are more plausible — reset explore_depth).
        gap_left = self._binsearch_last_sat(tab, attr, datatype, lo, mid, query, cutoff)
        gap_right = self._binsearch_first_sat(tab, attr, datatype, mid, hi, query, cutoff)
        if gap_left is not None and is_left_less_than_right_by_cutoff(datatype, lo, gap_left, cutoff):
            self._find_gaps_recursive(tab, attr, datatype, lo, gap_left, query,
                                      intervals, delta, cutoff,
                                      explore_depth=0, discover_depth=discover_depth + 1)
        elif gap_left is not None:
            intervals.append((lo, gap_left))
        if gap_right is not None and is_left_less_than_right_by_cutoff(datatype, gap_right, hi, cutoff):
            self._find_gaps_recursive(tab, attr, datatype, gap_right, hi, query,
                                      intervals, delta, cutoff,
                                      explore_depth=0, discover_depth=discover_depth + 1)
        elif gap_right is not None:
            intervals.append((gap_right, hi))

    def _binsearch_last_sat(self, tab, attr, datatype, sat_lo, unsat_hi, query, cutoff):
        # Largest v in [sat_lo, unsat_hi] with Pop(v) = true.
        lo, hi = sat_lo, unsat_hi
        while is_left_less_than_right_by_cutoff(datatype, lo, hi, cutoff):
            mid = get_mid_val(datatype, hi, lo)
            if mid == lo or mid == hi:
                break
            if self.checkAttribValueEffect(query, get_format(datatype, mid), [(tab, attr)]):
                lo = mid
            else:
                hi = mid
        return lo

    def _binsearch_first_sat(self, tab, attr, datatype, unsat_lo, sat_hi, query, cutoff):
        # Smallest v in [unsat_lo, sat_hi] with Pop(v) = true.
        lo, hi = unsat_lo, sat_hi
        while is_left_less_than_right_by_cutoff(datatype, lo, hi, cutoff):
            mid = get_mid_val(datatype, hi, lo)
            if mid == lo or mid == hi:
                break
            if self.checkAttribValueEffect(query, get_format(datatype, mid), [(tab, attr)]):
                hi = mid
            else:
                lo = mid
        return hi

    def _coalesce_intervals(self, intervals, datatype, delta):
        if not intervals:
            return []
        sorted_intervals = sorted(intervals, key=lambda iv: iv[0])
        merged = [sorted_intervals[0]]
        for lo, hi in sorted_intervals[1:]:
            prev_lo, prev_hi = merged[-1]
            adj = get_val_plus_delta(datatype, prev_hi, delta)
            if adj is not None and lo <= adj:
                merged[-1] = (prev_lo, hi if hi > prev_hi else prev_hi)
            else:
                merged.append((lo, hi))
        return merged

    def _emit_range_intervals(self, filter_attribs, tabname, attrib, intervals):
        if not intervals:
            return
        if len(intervals) == 1:
            lb, ub = intervals[0]
            filter_attribs.append((tabname, attrib, 'range', lb, ub))
            return
        # Multiple sub-intervals → record the disjunction in the side channel
        # and emit only the envelope (min lb, max ub) into filter_predicates.
        # AOA / equi_join consume filter_predicates and assume one contiguous
        # range per (tab, attr); they would corrupt multiple range tuples via
        # intersection. The render path consults disjunctive_ranges to
        # reconstruct the original OR-form for the final extracted SQL.
        normalized = sorted(intervals, key=lambda iv: iv[0])
        env_lb = normalized[0][0]
        env_ub = max(iv[1] for iv in normalized)
        filter_attribs.append((tabname, attrib, 'range', env_lb, env_ub))
        self.disjunctive_ranges[(tabname, attrib)] = normalized

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
                if self._gap_aware_enabled():
                    intervals = self._refine_with_gap_search(tabname, attrib, datatype, val2, val1, query,
                                                             filterAttribs)
                    self._emit_range_intervals(filterAttribs, tabname, attrib, intervals)
                else:
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
        elif self._gap_aware_enabled():
            # Both domain endpoints satisfy. Without gap-aware extraction, the
            # current code silently drops this case (treats it as "no predicate
            # on A"), which is wrong when the hidden predicate is a disjunction
            # spanning both extremes (e.g. A < 10 OR A >= 50). Run gap search
            # across the full domain to surface the gap(s).
            intervals = self._refine_with_gap_search(
                tabname, attrib, datatype,
                get_cast_value(datatype, min_val_domain),
                get_cast_value(datatype, max_val_domain),
                query,
                filterAttribs,
            )
            if len(intervals) > 1:
                self._emit_range_intervals(filterAttribs, tabname, attrib, intervals)

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
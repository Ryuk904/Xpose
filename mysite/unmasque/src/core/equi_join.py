import copy

from ...src.core.abstract.abstractConnection import AbstractConnectionHelper
from ...src.core.abstract.filter_holder import FilterHolder
from ...src.core.filter import Filter
from ...src.util.aoa_utils import get_op, get_tab, get_attrib, merge_equivalent_paritions
from ...src.util.utils import get_format

from typing import List, Tuple


class U2EquiJoin(FilterHolder):

    def __init__(self, connectionHelper: AbstractConnectionHelper,
                 core_relations: List[str],
                 filter_predicates: list,
                 filter_extractor: Filter,
                 global_min_instance_dict: dict):
        super().__init__(connectionHelper, core_relations, global_min_instance_dict, filter_extractor, "Equi_Join")
        self.algebraic_eq_predicates = []
        self.arithmetic_eq_predicates = []
        self.filter_predicates = filter_predicates
        self.pending_predicates = None
        # Per-table Qh-referenced columns surfaced by CardinalityProbe via
        # filter_extractor; used by the cross-alias self-join resolver to pick
        # a marker column. Empty dict if CardinalityProbe didn't run.
        self.qh_cols_by_table = getattr(filter_extractor, 'qh_cols_by_table', {}) or {}

    def is_it_equality_op(self, op):
        return op in [self.TEXT_EQUALITY_OP, self.MATH_EQUALITY_OP]

    def doActualJob(self, args=None):
        query = super().doActualJob(args)
        partition_eq_dict, ineqaoa_preds = self.algo2_preprocessing()
        self.logger.debug(partition_eq_dict)

        # §16: peel off self-join groups (multi-alias members on the same base
        # table). The generic partitioner in handle_higher_eq_groups picks the
        # first partition for which _extract_filter_on_attrib_set returns no
        # filter, which on a self-join's duplicated D¹ silently prefers intra-
        # alias splits (e.g. `a1.regionkey = a1.nationkey`) over the actual
        # cross-alias edge (`a1.regionkey = a2.nationkey`). Route those groups
        # through a dedicated resolver instead.
        self_join_keys = [k for k, g in partition_eq_dict.items()
                          if self._is_self_join_group(g)]
        self_join_groups = {k: partition_eq_dict.pop(k) for k in self_join_keys}

        self.algo3_find_eq_joinGraph(query, partition_eq_dict, ineqaoa_preds)

        for group in self_join_groups.values():
            edges = self._resolve_self_join_group(group, query)
            if edges:
                for edge in edges:
                    self.algebraic_eq_predicates.append(list(edge))
            else:
                # No cross-alias edge held — fall back to the raw group so the
                # downstream renderer at least emits *something*.
                self.logger.warning(
                    f"Self-join resolver: no holding edge for group {group}; "
                    f"emitting raw group as fallback"
                )
                self.algebraic_eq_predicates.append(list(group))

        self.pending_predicates = ineqaoa_preds  # pending predicates
        self.logger.debug(self.pending_predicates)
        self.logger.debug(self.algebraic_eq_predicates)
        self.logger.debug(self.arithmetic_eq_predicates)
        return True

    def algo3_find_eq_joinGraph(self, query: str, partition_eq_dict: dict, ineqaoa_preds: list) -> None:
        self.logger.debug(partition_eq_dict)
        while partition_eq_dict:
            check_again_dict = {}
            for key in partition_eq_dict.keys():
                equi_join_group = partition_eq_dict[key]
                if not len(equi_join_group):
                    continue
                if len(equi_join_group) <= 3:
                    self.handle_unit_eq_group(equi_join_group, query)
                else:
                    done = self.handle_higher_eq_groups(equi_join_group, query)
                    remaining_group = [eq for eq in equi_join_group if eq not in done]
                    check_again_dict[key] = remaining_group
            partition_eq_dict = check_again_dict
        to_remove = []
        for i, el_eq in enumerate(self.algebraic_eq_predicates):
            for j, pred in enumerate(el_eq):
                if len(pred) > 2:
                    ineqaoa_preds.append(pred)
                    to_remove.append((i, j))
        for tup in to_remove:
            del self.algebraic_eq_predicates[tup[0]][tup[1]]

    def handle_unit_eq_group(self, equi_join_group, query) -> bool:
        filter_attribs = []
        datatype = self.get_datatype(equi_join_group[0])
        self._extract_filter_on_attrib_set(filter_attribs, query, equi_join_group, datatype)
        self.logger.debug("join group check", equi_join_group, filter_attribs)
        if len(filter_attribs) > 0:
            equi_join_group.extend(filter_attribs)
        if not len(filter_attribs) or filter_attribs[0][2] != '=':
            self.algebraic_eq_predicates.append(equi_join_group)
        else:
            return False
        return True

    def handle_higher_eq_groups(self, equi_join_group, query):
        seq = list(range(len(equi_join_group)))
        t_all_paritions = merge_equivalent_paritions(seq)
        for part in t_all_paritions:
            check_part = min(part, key=len)
            attrib_list = [equi_join_group[i] for i in check_part]
            check = self.handle_unit_eq_group(attrib_list, query)
            if check:
                self.algebraic_eq_predicates.append(attrib_list)
                return attrib_list
        self.algebraic_eq_predicates.append(equi_join_group)
        return equi_join_group

    def algo2_preprocessing(self) -> Tuple[dict, list]:
        eq_groups_dict = {}
        ineq_filter_predicates = []
        for pred in self.filter_predicates:
            if self.is_it_equality_op(get_op(pred)):
                dict_key = pred[3]
                if dict_key in eq_groups_dict:
                    eq_groups_dict[dict_key].append((pred[0], pred[1]))
                else:
                    eq_groups_dict[dict_key] = [(pred[0], pred[1])]
            else:
                ineq_filter_predicates.append(pred)

        for key in eq_groups_dict.keys():
            if len(eq_groups_dict[key]) == 1:
                op = self.TEXT_EQUALITY_OP if isinstance(key, str) else self.MATH_EQUALITY_OP
                self.arithmetic_eq_predicates.append((get_tab(eq_groups_dict[key][0]),
                                                      get_attrib(eq_groups_dict[key][0]), op, key, key))
        eqJoin_group_dict = {key: value for key, value in eq_groups_dict.items() if len(value) > 1}
        # Phase 5: surface self-equi-joins for diagnostics. A group containing
        # two members whose alias->base maps to the same base table is exactly
        # a self-equi-join (e.g. lineitem__a1.l_orderkey = lineitem__a2.l_orderkey).
        if self.alias_to_table:
            for key, group in eqJoin_group_dict.items():
                bases = [self._to_base(a) for a, _ in group]
                if len(bases) != len(set(bases)):
                    self.logger.info(
                        f"Self-equi-join candidate on constant {key}: "
                        f"{[(a, attr) for a, attr in group]}"
                    )
        return eqJoin_group_dict, ineq_filter_predicates

    # ------------------------------------------------------------------
    # §16: cross-alias self-join resolver
    # ------------------------------------------------------------------

    def _is_self_join_group(self, group: list) -> bool:
        """A group is a self-join group if two of its aliases map to the same
        base table — i.e. the seeded equi-join is between multiple instances
        of one relation rather than a regular join across distinct relations."""
        if not group or not self.alias_to_table:
            return False
        bases = [self._to_base(a) for (a, _c) in group]
        return len(bases) != len(set(bases))

    def _resolve_self_join_group(self, group, query):
        """Replace a self-join group with cross-alias edge predicate(s).

        Group shape: [(alias_i, col_p), ...] where multiple aliases share a
        base table. For k=2 we generate every cross-alias edge candidate
        ((a1, x), (a2, y)) and probe each by isolating it on D¹: set the two
        members of the candidate to a shared value while every other group
        member gets a distinct unused value, then run Qh. Edges that keep
        Qh non-empty are the join's actual edges.

        For SJ3 (n_regionkey/n_nationkey), two cross-column edges are swap-
        symmetric and both 'hold'. A marker-column probe — picking a Qh-
        referenced column outside the equi-join group (e.g. n_name), tagging
        a1 and a2 with distinct values, and observing which tag Qh's output
        echoes — picks the orientation aligned with the QSG's default
        (cols_by_alias maps each col to a1 first).
        """
        by_alias = {}
        for (alias, col) in group:
            by_alias.setdefault(alias, []).append(col)
        aliases = sorted(by_alias.keys())
        if len(aliases) != 2:
            # §16 strawman: only k=2 supported. k>=3 is out of scope per §7.
            self.logger.debug(
                f"Self-join resolver: k={len(aliases)} not supported for group {group}"
            )
            return None
        a1, a2 = aliases
        cols_a1 = list(dict.fromkeys(by_alias[a1]))  # preserve order, dedupe
        cols_a2 = list(dict.fromkeys(by_alias[a2]))

        same_col_edges = [((a1, c), (a2, c)) for c in cols_a1 if c in cols_a2]
        cross_col_edges = [((a1, x), (a2, y)) for x in cols_a1 for y in cols_a2 if x != y]

        # Mutate sites = every distinct (alias, col) that appears in the group.
        # The isolate-and-probe routine mutates all of them; we save once and
        # revert once per candidate to keep ctids in sync.
        sites = list(dict.fromkeys((a, c) for (a, c) in group))

        holding = []
        for edge in same_col_edges + cross_col_edges:
            if self._isolate_and_probe_edge(edge, sites, query):
                holding.append(edge)

        self.logger.info(
            f"Self-join resolver: group {group} → holding edges {holding}"
        )

        if not holding:
            return None

        same_holding = [e for e in holding if e in same_col_edges]
        if same_holding:
            # Same-column equi-join (e.g. SJ2-style). One edge suffices; pick
            # the first deterministically.
            return [same_holding[0]]

        # All holding are cross-column. Two of them are usually swap-symmetric
        # (edges that match (a1, a2) pairs versus edges that match the swapped
        # (a2, a1) pairs). Disambiguate via the marker probe so the chosen
        # edge's matching pair has a1 as n1 — matching the QSG default that
        # qualifies bare SELECT cols with the first alias in cols_by_alias.
        if len(holding) == 1:
            return holding[:1]

        chosen = self._pick_marker_aligned_edge(holding, sites, query, a1, a2)
        return [chosen] if chosen else holding[:1]

    def _isolate_and_probe_edge(self, edge, sites, query) -> bool:
        """Set sites so only the candidate edge has matching values, run Qh."""
        (a_i, col_x), (a_j, col_y) = edge
        saved = self._snapshot_alias_cols(sites)
        try:
            assignments = self._chain_assignments_for_edge(edge, sites)
            if not self._apply_alias_col_vals(assignments):
                return False
            result = self.app.doJob(query)
            return self.app.isQ_result_nonEmpty_nullfree(result)
        finally:
            self._restore_alias_cols(saved)

    def _pick_marker_aligned_edge(self, cross_holding, sites, query, a1, a2):
        """Among swap-symmetric cross-alias edges, pick the one whose matching
        pair has a1 as n1 (n1 = the side QSG uses for bare SELECT col
        qualification by default)."""
        marker_col = self._find_marker_col(a1, sites)
        if marker_col is None:
            self.logger.debug(
                "Self-join resolver: no marker column available; defaulting to first holding edge"
            )
            return cross_holding[0]

        base = self._to_base(a1)
        datatype = self.get_datatype((base, marker_col))
        a1_marker, a2_marker = self._marker_values_for(datatype)
        marker_sites = sites + [(a1, marker_col), (a2, marker_col)]
        saved = self._snapshot_alias_cols(marker_sites)
        try:
            for edge in cross_holding:
                assignments = self._chain_assignments_for_edge(edge, sites)
                assignments.append((a1, marker_col, a1_marker))
                assignments.append((a2, marker_col, a2_marker))
                if not self._apply_alias_col_vals(assignments):
                    continue
                result = self.app.doJob(query)
                if self._result_contains(result, a1_marker):
                    self.logger.info(
                        f"Self-join resolver: marker probe picked {edge} "
                        f"(marker col={marker_col})"
                    )
                    return edge
        finally:
            self._restore_alias_cols(saved)
        # Marker probe couldn't pick. Fall back to first.
        return cross_holding[0]

    def _find_marker_col(self, alias, sites):
        """A Qh-referenced column on alias's base that is not part of the
        self-join group. Prefer string-typed for cleaner discrimination."""
        base = self._to_base(alias)
        qh_cols = self.qh_cols_by_table.get(base, set())
        group_cols = {c for (a, c) in sites if self._to_base(a) == base}
        candidates = [c for c in qh_cols if c not in group_cols]
        if not candidates:
            return None
        # Prefer string-typed marker — distinct dummy strings rarely collide
        # with the int chain values used to isolate edges.
        for c in candidates:
            try:
                if self.get_datatype((base, c)) == 'str':
                    return c
            except Exception:
                continue
        return sorted(candidates)[0]

    def _chain_assignments_for_edge(self, edge, sites):
        """Build (alias, col, value) assignments: edge members share K_BASE,
        every other site gets a distinct value above the base."""
        (a_i, col_x), (a_j, col_y) = edge
        K_BASE = 1_000_003  # well above any TPC-H column value
        edge_set = {(a_i, col_x), (a_j, col_y)}
        assignments = []
        offset = 1
        for (a, c) in sites:
            if (a, c) in edge_set:
                assignments.append((a, c, K_BASE))
            else:
                assignments.append((a, c, K_BASE + offset))
                offset += 1
        return assignments

    def _apply_alias_col_vals(self, assignments) -> bool:
        """Apply (alias, col, value) mutations via the alias-ctid path."""
        for (alias, col, val) in assignments:
            base = self._to_base(alias)
            try:
                datatype = self.get_datatype((base, col))
            except Exception:
                return False
            formatted = self._format_for_datatype(datatype, val)
            quoted = datatype == 'str'
            ok = self._exec_alias_ctid_update(
                alias, col, formatted,
                quoted=quoted, is_date=(datatype == 'date'), raw_val=val,
            )
            if not ok:
                self.logger.debug(
                    f"Self-join resolver: alias-ctid UPDATE failed for {alias}.{col}"
                )
                return False
        return True

    def _snapshot_alias_cols(self, sites):
        """Read current (alias, col) -> value from the alias dict."""
        snap = {}
        for (alias, col) in sites:
            entry = (self.global_alias_row_dict or {}).get(alias)
            if not entry:
                continue
            cols = entry.get("cols", ())
            row = entry.get("row", ())
            if col in cols:
                snap[(alias, col)] = row[cols.index(col)]
        return snap

    def _restore_alias_cols(self, snap):
        for (alias, col), val in snap.items():
            base = self._to_base(alias)
            try:
                datatype = self.get_datatype((base, col))
            except Exception:
                continue
            formatted = self._format_for_datatype(datatype, val)
            quoted = datatype == 'str'
            self._exec_alias_ctid_update(
                alias, col, formatted,
                quoted=quoted, is_date=(datatype == 'date'), raw_val=val,
            )

    def _format_for_datatype(self, datatype, val):
        """get_format wraps str/date in quotes, but the quoted-ctid template
        adds its own quotes. Strip quotes here so the templates receive a raw
        literal in every case; _exec_alias_ctid_update chooses the right one."""
        if datatype == 'str':
            return str(val)
        if datatype == 'date':
            return str(val)
        return get_format(datatype, val)

    def _marker_values_for(self, datatype):
        if datatype == 'str':
            return 'XPOSEMARKA', 'XPOSEMARKB'
        if datatype == 'date':
            # Use two distinct, far-apart dates to avoid TPC-H overlap.
            return '1900-01-01', '1900-01-02'
        # numeric/int
        return 8_888_801, 8_888_802

    def _result_contains(self, result, marker_val) -> bool:
        """Scan app.doJob output (a list whose 0th elt is the header) for the
        marker value."""
        if not isinstance(result, list) or len(result) < 2:
            return False
        target = str(marker_val).strip()
        for row in result[1:]:
            try:
                cells = row
            except Exception:
                continue
            for cell in cells:
                if cell is None:
                    continue
                if str(cell).strip() == target:
                    return True
        return False

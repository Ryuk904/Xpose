"""
Disjunction refinement
======================

The boundary searches used to extract a range predicate on a numeric attribute do a
plain binary search and therefore assume that the satisfying values form *one* solid
block.  For a disjunction (``a BETWEEN 10 AND 20 OR a BETWEEN 30 AND 40``) they form
several blocks with gaps in between, and a small gap can be stepped over - so we extract
the over-approximated ``a BETWEEN 10 AND 40`` and the disjunction loop never recovers
the real structure.

This module corrects that *after* the rest of the pipeline has produced an extracted
query ``Q_E``.  It is gated by the existing ``[feature] or`` flag (``config.detect_or``).

Approach (see ``docs/disjunction_refinement.md`` for the full write-up):

  1. Compare ``Q_H`` with ``Q_E`` on the database.  If they already match, do nothing.
  2. Otherwise, for every numeric ``range`` / ``IN`` atom and every ``LIKE`` atom of
     ``Q_E``, list the *distinct data values of that column that the atom currently
     keeps*, and probe each one against the hidden query using the single-tuple
     ``D_min`` mutation oracle (re-using the ``Filter`` extractor's helpers).  The
     values that the hidden query rejects mark gaps; split the atom accordingly.
  3. Rebuild ``Q_E`` and repeat (bounded number of rounds).

Safety: the refiner can only ever *tighten* ``Q_E`` (it removes data values that the
hidden query rejects), and at the very end it keeps the refined query *only if* it now
matches the hidden query exactly - otherwise it returns the query it was given.  So it
can never regress an extraction that already worked.

Assumption: every value that matters to the hidden query has a "signature" row in the
database (otherwise no black-box method could observe the gap at all).
"""

import time

from .filter import Filter
from .nep import NepComparator
from ..util.Log import Log
from ..util.aoa_utils import get_attrib, get_op, get_tab
from ..util.utils import get_format


def _runs_of_satisfying(values, good_flags):
    """Group a sorted list of (value, is_satisfying) into maximal runs of satisfying
    values. Returns a list of (run_first_value, run_last_value) pairs."""
    runs, run = [], None
    for v, ok in zip(values, good_flags):
        if ok:
            if run is None:
                run = [v, v]
            else:
                run[1] = v
        else:
            if run is not None:
                runs.append((run[0], run[1]))
                run = None
    if run is not None:
        runs.append((run[0], run[1]))
    return runs


class DisjunctionRefiner:

    REFINE_CUTOFF = 8            # max refinement rounds
    MAX_DATA_VALUES = 15000      # skip refining an atom whose believed range holds more
                                 # distinct data values than this (keeps the cost sane)

    def __init__(self, connectionHelper, core_relations, q_generator, filter_extractor):
        self.connectionHelper = connectionHelper
        self.core_relations = core_relations
        self.q_generator = q_generator
        self.filter_extractor = filter_extractor
        self.logger = Log("DisjunctionRefiner", connectionHelper.config.log_level)
        self.local_elapsed_time = 0
        self.app_calls = 0

    # --------------------------------------------------------------------- #
    # entry point
    # --------------------------------------------------------------------- #
    def refine(self, query, eq):
        start = time.time()
        original_eq = eq
        try:
            if not self.connectionHelper.config.detect_or:
                return eq
            if eq is None or not str(eq).strip():
                return eq
            if self._matches(query, eq):
                self.logger.debug("Extracted query already matches the hidden query; no disjunction refinement needed.")
                return eq

            for _round in range(self.REFINE_CUTOFF):
                changed = self._one_round(query)
                if not changed:
                    break
                eq = self.q_generator.rebuild_after_predicate_change()
                self.logger.debug("After a refinement round, extracted query is:\n", eq)
                if self._matches(query, eq):
                    break

            if self._matches(query, eq):
                self.logger.debug("Disjunction refinement produced an exact query.")
                return eq
            self.logger.info("Disjunction refinement did not reach an exact query; keeping the earlier extracted query.")
            return original_eq
        except Exception as e:
            self.logger.error("Disjunction refinement failed; keeping the earlier extracted query.", str(e))
            return original_eq
        finally:
            self.local_elapsed_time += time.time() - start

    # --------------------------------------------------------------------- #
    # comparison oracle (Q_H vs Q_E on the database)
    # --------------------------------------------------------------------- #
    def _matches(self, query, eq):
        comparator = NepComparator(self.connectionHelper, self.core_relations)
        try:
            matched, _restore = comparator.doJob(query, eq)
        except Exception as e:
            self.logger.debug("Comparator raised while checking Q_E:", str(e))
            return False
        self.app_calls += 1
        return matched is True

    # --------------------------------------------------------------------- #
    # one refinement round
    # --------------------------------------------------------------------- #
    def _one_round(self, query):
        # bring D_min (one representative tuple per relation) back into the working schema
        self.filter_extractor.restore_d_min_from_dict()
        changed = False
        for atom in self._suspect_atoms():
            try:
                if self._reverify_atom(query, atom):
                    changed = True
            except Exception as e:
                self.logger.debug(f"Could not re-verify atom {atom}:", str(e))
        return changed

    def _suspect_atoms(self):
        wc = self.q_generator._workingCopy
        suspects = []
        for f in list(wc.arithmetic_filters):
            op = str(get_op(f)).strip().lower()
            tab, attrib = get_tab(f), get_attrib(f)
            dt = self._dt(tab, attrib)
            if op == 'range' and dt != 'str':
                suspects.append(('range', tab, attrib, dt, (f[3], f[4])))
            elif op in ('like',) and dt == 'str':
                suspects.append(('like', tab, attrib, dt, f[3]))
        for f in list(wc.filter_in_predicates):
            tab, attrib = get_tab(f), get_attrib(f)
            dt = self._dt(tab, attrib)
            if dt == 'str':
                continue
            intervals = self._intervals_of_in_atom(f[3])
            if intervals:
                suspects.append(('in', tab, attrib, dt, intervals))
        return suspects

    @staticmethod
    def _intervals_of_in_atom(value_list):
        intervals = []
        for v in list(value_list):
            if isinstance(v, tuple) and len(v) == 2:
                intervals.append((v[0], v[1]))
            else:
                intervals.append((v, v))
        return intervals

    # --------------------------------------------------------------------- #
    # re-verify one atom
    # --------------------------------------------------------------------- #
    def _reverify_atom(self, query, atom):
        kind, tab, attrib, dt, payload = atom
        if kind == 'like':
            return self._reverify_like(query, tab, attrib, dt, payload)
        # numeric: payload is a single (lo, hi) tuple ('range') or a list of intervals ('in').
        # Process each original interval *separately* so we never bridge an original gap.
        orig_intervals = [tuple(payload)] if kind == 'range' else [tuple(iv) for iv in payload]
        new_intervals = []
        any_bad = False
        for (lo, hi) in orig_intervals:
            values = self._distinct_data_values_numeric(tab, attrib, dt, [(lo, hi)])
            if values is None:                              # could not enumerate / too many - leave it
                new_intervals.append((lo, hi))
                continue
            if not values:                                 # no data here: nothing the query could discriminate
                new_intervals.append((lo, hi))
                continue
            good = [self._is_satisfying(query, tab, attrib, dt, v) for v in values]
            if all(good):
                new_intervals.append((lo, hi))
                continue
            if not any(good):
                # whole interval rejected - that is a missing/over-counted clause, not a gap; leave it.
                self.logger.info(f"{tab}.{attrib}: data values in [{lo}, {hi}] all rejected; leaving this interval.")
                new_intervals.append((lo, hi))
                continue
            any_bad = True
            runs = _runs_of_satisfying(values, good)
            new_intervals.extend(self._tighten_runs_in_interval(query, tab, attrib, dt, values, runs, lo, hi))
        if not any_bad:
            return False
        new_intervals = self._normalize_intervals(new_intervals)
        if not new_intervals:
            return False
        if [tuple(iv) for iv in new_intervals] == [tuple(iv) for iv in orig_intervals]:
            return False
        self.q_generator.replace_range_with_intervals(tab, attrib, new_intervals)
        self.logger.debug(f"{tab}.{attrib}: refined {orig_intervals} -> {new_intervals}")
        return True

    def _tighten_runs_in_interval(self, query, tab, attrib, dt, values, runs, orig_lo, orig_hi):
        """Turn the runs of satisfying *data* values (all inside [orig_lo, orig_hi]) into
        clean intervals. A run edge that touches the original interval bound keeps that
        bound; an edge adjacent to a gap is sharpened with a binary search over the gap -
        safe because between two consecutive distinct data values there is no other data
        value, hence at most one boundary in that stretch."""
        first_idx = {}
        for i, v in enumerate(values):
            first_idx.setdefault(v, i)
        out = []
        for ri, (lo, hi) in enumerate(runs):
            lo_i, hi_i = first_idx[lo], first_idx[hi]
            new_lo, new_hi = lo, hi
            if ri == 0 and lo == values[0]:
                new_lo = orig_lo if orig_lo <= lo else lo
            elif lo_i > 0:
                edge = self._binary_edge(query, tab, attrib, dt, values[lo_i - 1], lo, '>=')
                if edge is not None and edge <= lo:
                    new_lo = edge
            if ri == len(runs) - 1 and hi == values[-1]:
                new_hi = orig_hi if orig_hi >= hi else hi
            elif hi_i < len(values) - 1:
                edge = self._binary_edge(query, tab, attrib, dt, hi, values[hi_i + 1], '<=')
                if edge is not None and edge >= hi:
                    new_hi = edge
            if new_lo <= new_hi:
                out.append((new_lo, new_hi))
        return out

    @staticmethod
    def _normalize_intervals(intervals):
        items = sorted((iv for iv in intervals if iv[0] <= iv[1]), key=lambda iv: (iv[0], iv[1]))
        merged = []
        for lo, hi in items:
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        return merged

    # --------------------------------------------------------------------- #
    # text (LIKE) atom
    # --------------------------------------------------------------------- #
    def _reverify_like(self, query, tab, attrib, dt, pattern):
        values = self._distinct_data_values_like(tab, attrib, pattern)
        if values is None or not values:
            return False
        leaks = []
        for v in values:
            if not self._is_satisfying(query, tab, attrib, 'str', v):
                leaks.append(v)
        if not leaks:
            return False
        self.q_generator.add_not_in_predicate(tab, attrib, leaks)
        self.logger.debug(f"{tab}.{attrib}: LIKE {pattern!r} leaks {leaks}; added NOT IN.")
        return True

    # --------------------------------------------------------------------- #
    # mutation oracle: is value `v` satisfying for column (tab, attrib) ?
    # --------------------------------------------------------------------- #
    def _is_satisfying(self, query, tab, attrib, dt, v):
        self.app_calls += 1
        return bool(self.filter_extractor.checkAttribValueEffect(query, get_format(dt, v), [(tab, attrib)]))

    def _binary_edge(self, query, tab, attrib, dt, low_v, high_v, operator):
        """Re-use Filter.get_filter_value to locate the predicate boundary inside the
        (data-value-free) stretch (low_v, high_v). `operator` is '<=' (return the largest
        satisfying value, i.e. the upper edge of a run) or '>=' (return the smallest
        satisfying value, i.e. the lower edge of a run)."""
        if dt not in ('int', 'date', 'numeric'):
            return None
        try:
            self.app_calls += 1
            return self.filter_extractor.get_filter_value(query, dt, low_v, high_v, operator, [(tab, attrib)])
        except Exception as e:
            self.logger.debug(f"binary edge search failed for {tab}.{attrib}:", str(e))
            return None

    # --------------------------------------------------------------------- #
    # distinct data values from the (real) database
    # --------------------------------------------------------------------- #
    def _user_table(self, tab):
        return f"{self.connectionHelper.config.user_schema}.{tab}"

    def _distinct_data_values_numeric(self, tab, attrib, dt, intervals):
        clauses = []
        for lo, hi in intervals:
            f_lo, f_hi = get_format(dt, lo), get_format(dt, hi)
            if lo == hi:
                clauses.append(f"{attrib} = {f_lo}")
            else:
                clauses.append(f"{attrib} between {f_lo} and {f_hi}")
        where = " or ".join(f"({c})" for c in clauses) if clauses else "true"
        sql = (f"select distinct {attrib} from {self._user_table(tab)} "
               f"where ({where}) and {attrib} is not null order by {attrib};")
        return self._fetch_first_column(sql)

    def _distinct_data_values_like(self, tab, attrib, pattern):
        sql = (f"select distinct {attrib} from {self._user_table(tab)} "
               f"where {attrib} like {get_format('str', pattern)} and {attrib} is not null;")
        return self._fetch_first_column(sql)

    def _fetch_first_column(self, sql):
        try:
            res, _desc = self.connectionHelper.execute_sql_fetchall(sql)
        except Exception as e:
            self.logger.debug("Could not list distinct data values:", str(e))
            return None
        if res is None:
            return None
        if len(res) > self.MAX_DATA_VALUES:
            self.logger.info(f"Skipping refinement for a column with {len(res)} distinct candidate values "
                             f"(> MAX_DATA_VALUES={self.MAX_DATA_VALUES}).")
            return None
        return [row[0] for row in res]

    # --------------------------------------------------------------------- #
    def _dt(self, tab, attrib):
        return self.filter_extractor.get_datatype((tab, attrib))


def make_filter_for_refiner(connectionHelper, core_relations, global_min_instance_dict):
    """Build a fresh Filter extractor for use by the refiner (only its mutation/check
    helpers are used). Kept as a function so the import surface in the pipelines stays
    small."""
    f = Filter(connectionHelper, core_relations, global_min_instance_dict)
    f.do_init()
    return f

"""Witness finder for within-attribute gap (OR-of-intervals) extraction.

This is a thin specialization of ``NepMinimizer``. Both share the same
strategy that NEP pioneered:

    restore full D into the working schema -> diff the *reconstructed* Q_E
    against the hidden Qh -> ctid-bisect the offending table down to the
    single base row that still causes the diff (the "witness").

Because the diff is run against the **reconstructed** Q_E (whose projection
the Projection stage already recovered to match Qh exactly), it is meaningful
for *any* Qh projection -- bare columns, scalar expressions and aggregates --
which is the whole point of relocating gap extraction to sit beside NEP.

The ONLY behavioural difference from ``NepMinimizer`` is the per-half test
``check_result_for_half``. ``NepMinimizer`` decides which ctid half retains the
witness using *result-cardinality* heuristics (``row_count_r_e`` vs
``row_count_r_h``). Those are blind to aggregate queries: ``SELECT count(*)``
always returns exactly one result row, so both counts are 1 regardless of how
many base rows fall in a gap, and the heuristic can never pick the right half.

``GapMinimizer`` instead keeps a half iff the comparator reports a genuine
*mismatch* over it (``match() is False``), which is projection-agnostic: a
count/sum delta on a single result row is a mismatch just as much as a missing
row is. ``match()`` returns ``True`` (bag-equal), ``False`` (differ) or
``None`` (Q_E malformed); only ``False`` means "a witness lives in this half".
"""

from .nep import NepMinimizer


class GapMinimizer(NepMinimizer):

    def __init__(self, connectionHelper, core_relations, all_sizes):
        super().__init__(connectionHelper, core_relations, all_sizes)
        self.name = "Gap_Minimizer"

    def check_result_for_half(self, start_ctid, end_ctid, tab, view, query):
        # Materialise the candidate half as a view shadowing <tab> by ctid
        # range, exactly like the parent, then run the comparator diff.
        self.connectionHelper.execute_sql(
            [self.connectionHelper.queries.drop_view(
                self.get_fully_qualified_table_name(view)),
             self.connectionHelper.queries.create_view_as_select_star_where_ctid(
                 end_ctid, start_ctid,
                 self.get_fully_qualified_table_name(view),
                 self.get_fully_qualified_table_name(tab))],
            self.logger)
        found = self.nep_comparator.match(query, self.Q_E)
        # A half "works" (retains a witness) iff Q_E and Qh genuinely disagree
        # over it AND Q_E actually produced rows there.
        #
        # found is False  -> mismatch (witness here);  True/None -> no witness.
        #
        # The r_e-non-empty guard is load-bearing for row-preserving Qh: the
        # comparator runs in HASH mode, where an EMPTY r_e hashes to NULL and is
        # reported as "not matched" -- a false mismatch. Without this guard the
        # bisection would keep empty halves (those containing no Q_E rows) and
        # converge on a non-witness row. NepMinimizer guards the same way
        # (`elif not row_count_r_e: return False`). For aggregate Qh r_e is
        # always one row, so the guard is a no-op there.
        return (found is False) and bool(self.nep_comparator.row_count_r_e)

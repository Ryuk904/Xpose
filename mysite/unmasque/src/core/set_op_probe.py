"""WI-14 — UNION vs UNION ALL (set-operator dedup) discrimination probe.

``UNION`` (set) and ``UNION ALL`` (bag) differ only in whether duplicate
output rows are collapsed. On the single-witness instance ``D¹`` that the
View Minimizer leaves behind, each branch yields one output row, so the
distinction is *invisible* — there is nothing to dedup. WI-14 makes the
bit observable with a controlled within-branch duplicate (enabler S2,
:class:`~mysite.unmasque.src.core.row_probe.RowProbe`):

  1. Reset this branch's relations to ``D¹`` (one contributing witness
     row). The :class:`UnionPipeLine` loop has already nullified every
     *other* branch's relations, so running ``Qh`` returns only this
     branch's contribution under the hidden set operator.
  2. Count ``Qh``'s null-free output rows -> ``c0``.
  3. Duplicate one contributing witness row of the branch's first
     relation. For a *plain-projection* branch (no GROUP BY, no aggregate)
     this is guaranteed to add an identical row to the branch's pre-dedup
     output (one extra feeding tuple -> one extra projected row; a join
     just multiplies it, which still grows the bag).
  4. Re-run ``Qh`` -> ``c1``, then revert the duplicate.

Signal:

* ``c1 > c0``  -> the duplicate survived  -> **UNION ALL**. This can only
  happen under bag semantics: an exact-duplicate projected row can never
  *grow* a set-deduped result, so growth is sound, unconditional proof of
  ``UNION ALL`` (it does not even require the plain-projection guarantee).
* ``c1 == c0`` -> the duplicate was collapsed -> **UNION**. Trustworthy
  only on a plain-projection branch, where step 3 is guaranteed to have
  produced a pre-dedup duplicate; the caller enforces that.
* otherwise (``Qh`` unreadable / empty witness / duplicate failed) ->
  undecided (``None``).

The caller defaults to ``UNION ALL`` (the historical behaviour, and the
honest choice when ``D`` exposes no duplicate) whenever no branch yields a
decisive signal, so a failed probe never regresses a real ``UNION ALL``;
the only deviation from the default is a *positively observed* dedup.

No new oracle: the duplicate/revert go through ``RowProbe`` (which shares
the stage connection, so the uncommitted INSERT is visible to the next
``Qh`` run and the DELETE reverts it), and the count reuses the same
``app`` executable the whole pipeline runs ``Qh`` with.
"""

from .row_probe import RowProbe
from .abstract.GenerationPipeLineBase import GenerationPipeLineBase

# SQL set-operator tokens (distinct from the ``UNION`` *pipeline-state*
# constant in util.constants, which is a progress label, not SQL).
SET_OP_UNION_ALL = "UNION ALL"
SET_OP_UNION = "UNION"


class SetOpProbe(GenerationPipeLineBase):
    """Decide ``UNION`` vs ``UNION ALL`` from one isolated branch on ``D¹``."""

    def __init__(self, connectionHelper, genCtx):
        super().__init__(connectionHelper, "SetOp_Probe", genCtx)
        # Enabler S2: duplicate-by-ctid + revert.
        self._row_probe = RowProbe(self.connectionHelper, self.app, self.logger)

    def probe_branch(self, query):
        """Return ``SET_OP_UNION_ALL`` / ``SET_OP_UNION`` / ``None``.

        The caller MUST have (a) nullified every other branch's relations
        and (b) verified this branch is a plain projection before trusting
        a ``UNION`` verdict (a GROUP BY / aggregate would absorb the
        duplicate regardless of the operator, masquerading as ``UNION``).
        """
        if not self.core_relations:
            return None
        # Make sure Qh runs against the working (mutated) schema, where this
        # branch is about to become D¹ and the other branches are empty.
        self.set_data_schema()
        self.do_init()  # this branch -> D¹; other branches stay nullified/empty

        c0 = self._count_qh(query)
        if not c0:  # None (Qh unreadable) or 0 (empty witness)
            return None

        fqn = self.get_fully_qualified_table_name(self.core_relations[0])
        ctids = self._row_probe.list_ctids(fqn)
        new_ctids = self._row_probe.duplicate_rows(fqn, ctids[:1] if ctids else None)
        if not new_ctids:
            self.logger.debug("SetOpProbe: could not duplicate a witness row; undecided.")
            return None
        try:
            c1 = self._count_qh(query)
        finally:
            self._row_probe.delete_rows(fqn, new_ctids)

        if c1 is None:
            return None
        if c1 > c0:
            self.logger.debug(f"SetOpProbe: duplicate survived ({c0} -> {c1}) => UNION ALL")
            return SET_OP_UNION_ALL
        if c1 == c0:
            self.logger.debug(f"SetOpProbe: duplicate collapsed ({c0} -> {c1}) => UNION")
            return SET_OP_UNION
        # c1 < c0 cannot happen (a duplicate only adds rows); treat as noise.
        return None

    def _count_qh(self, query):
        """Number of null-free rows ``Qh`` returns now; ``None`` on failure."""
        res = self.app.doJob(query)
        if not self.app.done:
            return None
        return len(self.app.get_all_nullfree_rows(res))

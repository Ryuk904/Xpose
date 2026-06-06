"""WI-36 — Uncorrelated EXISTS-gate non-scaling probe.

An *uncorrelated* ``EXISTS`` gate in the WHERE clause, e.g.

    SELECT n_name FROM nation WHERE EXISTS (SELECT 1 FROM region WHERE r_regionkey > 2)

references a gate relation (``region``) that supplies **no projected output
column** and **no join edge** to the outer query. It only gates the whole
result on its (filtered) non-emptiness — a 0/1 switch.

The framework's FROM-clause stage classifies a relation as *core* by emptying
it and checking whether ``Qh`` goes empty (``from_clause.get_core_relations_by_void``).
For an EXISTS gate, emptying ``region`` makes the subquery empty -> ``EXISTS``
false -> ``Qh`` returns 0 rows -> ``region`` is (correctly) swept into
``core_relations``. But it is then emitted as an unconstrained extra FROM table,
i.e. a *wrong cross join*. WI-36 intercepts and reclassifies the gate.

Four black-box conditions identify an uncorrelated EXISTS gate ``T`` among the
core relations:

  (1) **Load-bearing** — emptying ``T`` flips ``Pop`` to empty. Already true:
      that is precisely why ``from_clause`` kept ``T`` (core membership).
  (2) **Non-projecting** — no projected output column is attributed to ``T``
      (read ``Projection.dependencies``). A gate has none.
  (3) **Non-joining** — no equi-join edge and no AOA/theta edge touches ``T``
      (read the join graph + AOA predicates). A gate has none.
  (4) **NON-SCALING** — *the decisive discriminator vs a CROSS JOIN*, which
      ALSO empties the result when emptied. Duplicate one contributing row of
      ``T`` on ``D¹`` (enabler S2, :class:`RowProbe`) and recount ``Qh``:

        * a cross join multiplies the result by ~(rows added) -> ``|Qh|`` GROWS;
        * an EXISTS gate is a 0/1 switch that was already satisfied and stays
          satisfied -> ``|Qh|`` is UNCHANGED.

This module owns condition (4) — the live row-duplication probe. Conditions
(2) and (3) are cheap pre-filters computed by the caller from already-extracted
state; (1) is core membership.

Signal (mirrors :class:`SetOpProbe`, inverted):

* ``c1 == c0``  (unchanged) -> **gate** (return ``True``).
* ``c1 >  c0``  (grew)      -> cross/inner join (return ``False``).
* otherwise (``Qh`` unreadable / empty witness / duplicate failed) -> undecided
  (``None``); the caller fails closed and keeps ``T`` in ``core_relations``.

No new oracle: the duplicate/revert go through :class:`RowProbe` (which shares
the stage connection, so the uncommitted INSERT is visible to the next ``Qh``
run and the DELETE reverts it), and the count reuses the same ``app`` the whole
pipeline runs ``Qh`` with. ``do_init()`` first resets the working schema to
``D¹``, so a prior stage's leftover mutations (e.g. the Limit probe's inserts)
do not perturb the measurement.
"""

from .row_probe import RowProbe
from .abstract.GenerationPipeLineBase import GenerationPipeLineBase


class ExistsGateProbe(GenerationPipeLineBase):
    """Decide gate (non-scaling) vs cross/inner join (scaling) for one relation."""

    def __init__(self, connectionHelper, genCtx):
        super().__init__(connectionHelper, "Exists_Gate_Probe", genCtx)
        # Enabler S2: duplicate-by-ctid + revert.
        self._row_probe = RowProbe(self.connectionHelper, self.app, self.logger)

    def is_nonscaling_gate(self, query, tab):
        """Return ``True`` (gate) / ``False`` (scaling join) / ``None`` (undecided).

        ``tab`` MUST be in ``core_relations`` and MUST already have passed the
        caller's non-projecting (2) and non-joining (3) pre-filters; this method
        only decides condition (4).
        """
        if tab not in self.core_relations:
            return None
        # Run Qh against the working (mutated) schema, reset to D¹.
        self.set_data_schema()
        self.do_init()

        c0 = self._count_qh(query)
        if not c0:  # None (Qh unreadable) or 0 (empty witness) -> can't decide
            return None

        fqn = self.get_fully_qualified_table_name(tab)
        ctids = self._row_probe.list_ctids(fqn)
        new_ctids = self._row_probe.duplicate_rows(fqn, ctids[:1] if ctids else None)
        if not new_ctids:
            self.logger.debug(f"ExistsGateProbe: could not duplicate a row of {tab}; undecided.")
            return None
        try:
            c1 = self._count_qh(query)
        finally:
            self._row_probe.delete_rows(fqn, new_ctids)

        if c1 is None:
            return None
        if c1 == c0:
            self.logger.debug(
                f"ExistsGateProbe: {tab} duplicate left |Qh| unchanged ({c0} -> {c1}) => EXISTS gate")
            return True
        if c1 > c0:
            self.logger.debug(
                f"ExistsGateProbe: {tab} duplicate scaled |Qh| ({c0} -> {c1}) => cross/inner join")
            return False
        # c1 < c0 cannot happen (a duplicate only adds rows); treat as noise.
        return None

    def _count_qh(self, query):
        """Number of null-free rows ``Qh`` returns now; ``None`` on failure."""
        res = self.app.doJob(query)
        if not self.app.done:
            return None
        return len(self.app.get_all_nullfree_rows(res))

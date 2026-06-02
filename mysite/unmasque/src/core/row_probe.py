"""Enabler S2 — controlled duplicate-row probe primitive.

Many SQL constructs are *multiplicity* phenomena: the literal constant 1
vs ``COUNT()==1``, ``COUNT(DISTINCT col)``, ``SELECT DISTINCT``, ``UNION``
vs ``UNION ALL``, ``HAVING``. They are invisible on the single witness row
``D¹`` that the View Minimizer leaves behind, so a detector must make them
observable on a *controlled* multi-row instance: add a known number of
duplicate tuples, run ``Qh``, read the result-shape change, then undo the
duplication so ``D`` is left byte-for-byte unchanged.

The three operations that experiment needs — duplicate-by-ctid,
delete-by-ctid, and count — already existed, copy-pasted, inside the
stages that happened to need them first:

* :meth:`CardinalityProbe._insert_duplicate` /
  :meth:`CardinalityProbe._delete_rows_at_ctids`
  (``mysite/unmasque/src/core/cardinality_probe.py``)
* :meth:`MultiplicityProbe._count_rows`
  (``mysite/unmasque/src/core/multiplicity_probe.py``)

This module factors them into one reusable helper so any stage can run a
``duplicate → count → revert`` experiment without re-implementing — or
subtly diverging on — the SQL and the header/None handling.

No new oracle is introduced. Duplication and deletion go through the
existing ``connectionHelper``; the count reuses the existing ``app``
executable. Because ``app`` shares the calling stage's ``connectionHelper``
(one connection, therefore one transaction), an *uncommitted* ``INSERT`` is
already visible to the very next ``app.doJob``, and the matching ``DELETE``
reverts it — leaving ``D`` (and its ctid set) exactly as it was.
"""

from typing import List, Optional


class RowProbe:
    """duplicate-by-ctid + delete-by-ctid + count, leaving ``D`` unchanged.

    Parameters
    ----------
    connectionHelper
        Runs the duplicate / delete DML and the ctid read-back.
    app
        The executable used to run ``Qh`` for the row count.
    logger
        Optional; debug / error breadcrumbs.
    """

    def __init__(self, connectionHelper, app, logger=None):
        self.connectionHelper = connectionHelper
        self.app = app
        self.logger = logger

    # ------------------------------------------------------------------ count
    def count_rows(self, query: str) -> int:
        """Run ``query`` via the app and return its data-row count (header
        excluded). Returns ``-1`` if the query could not be executed and
        ``0`` for an empty / ``None`` result.

        This is the verbatim behaviour of the original
        ``MultiplicityProbe._count_rows`` (the header is the column-name
        tuple ``app.doJob`` prepends). A new consumer that needs to tell a
        genuine error apart from a degenerate result should additionally
        check ``app.done`` after calling, because ``app.doJob`` returns an
        error *string* (not a list) on failure.
        """
        try:
            result = self.app.doJob(query)
        except Exception as e:
            if self.logger is not None:
                self.logger.debug(f"RowProbe: query execution failed: {e}")
            return -1
        if result is None:
            return 0
        try:
            n = len(result)
            if n and isinstance(result[0], (list, tuple)) and \
                    all(isinstance(c, str) for c in result[0]):
                return max(0, n - 1)
            return n
        except TypeError:
            return -1

    # -------------------------------------------------------------- duplicate
    def duplicate_rows(self, fqn: str, ctids: Optional[List[str]] = None) -> List[str]:
        """``INSERT`` a copy of rows of ``fqn`` and return the new rows' ctids.

        With ``ctids=None`` every current row is duplicated (the
        ``CardinalityProbe`` self-join case). With an explicit ``ctids`` list
        only those rows are duplicated — the targeted multiplicity probe used
        by COUNT/DISTINCT/HAVING detectors. The ``SELECT`` is snapshotted
        before the ``INSERT``, so duplicating from the same table never feeds
        on its own freshly-inserted output.
        """
        if ctids:
            ctid_list = ", ".join(f"'{c}'" for c in ctids)
            where = f" WHERE ctid IN ({ctid_list})"
        else:
            where = ""
        sql = f"INSERT INTO {fqn} SELECT * FROM {fqn}{where} RETURNING ctid::text;"
        try:
            res, _ = self.connectionHelper.execute_sql_fetchall(sql, self.logger)
        except Exception as e:
            if self.logger is not None:
                self.logger.debug(f"RowProbe: INSERT…RETURNING failed for {fqn}: {e}")
            return []
        if not res:
            return []
        return [str(row[0]) for row in res]

    # ----------------------------------------------------------- delete/revert
    def delete_rows(self, fqn: str, ctids: List[str]) -> None:
        """``DELETE`` rows of ``fqn`` at the given ctids (reverts a duplicate)."""
        for ctid in ctids:
            sql = f"DELETE FROM {fqn} WHERE ctid = '{ctid}';"
            try:
                self.connectionHelper.execute_sql([sql], self.logger)
            except Exception as e:
                if self.logger is not None:
                    self.logger.error(f"RowProbe: failed to delete row at {ctid}: {e}")

    # --------------------------------------------------------------- ctid read
    def list_ctids(self, fqn: str) -> List[str]:
        """Return the ctids currently present in ``fqn`` (``ORDER BY ctid``).

        Used to target a specific row for duplication and to assert the ctid
        set is restored after a ``duplicate → revert`` round-trip.
        """
        try:
            res, _ = self.connectionHelper.execute_sql_fetchall(
                self.connectionHelper.queries.select_ctid_star_from(fqn), self.logger)
        except Exception as e:
            if self.logger is not None:
                self.logger.debug(f"RowProbe: list_ctids failed for {fqn}: {e}")
            return []
        return [str(row[0]) for row in res] if res else []

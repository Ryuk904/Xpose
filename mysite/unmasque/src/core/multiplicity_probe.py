"""Phase 7: cardinality-scaling verification probe.

Catches multi-instance / self-join structures that ViewMinimizer's floor signal
missed. The most common case is a self-join inside an aggregation, where the
aggregate flattens the result-cardinality shape that would otherwise expose the
floor as `min_card[T] > 1`.

Approach: after the main extraction yields a candidate Q_E, run both Q_E and
the original Qh on the unmodified DB and compare row counts. If Qh produces
materially more rows than Q_E for a query where both share the same FROM
projection, that's evidence the extraction missed a multiplicity. The probe
does NOT attempt to repair the extraction — it just reports the discrepancy
so callers can decide whether to escalate (warn, raise, etc.).
"""

from typing import Dict, List, Optional

from .row_probe import RowProbe


class MultiplicityProbe:
    """Compares Qh vs Q_E result cardinality as a sanity check."""

    def __init__(self, connectionHelper, app, logger=None):
        self.connectionHelper = connectionHelper
        self.app = app
        self.logger = logger
        # Per-table multiplicity heuristic from min_card; surfaced for the
        # report. Populated by run().
        self.detected_multiplicity: Dict[str, int] = {}
        self.qh_rows: Optional[int] = None
        self.qe_rows: Optional[int] = None
        # Enabler S2: shared count helper (header-stripping logic factored out).
        self._row_probe = RowProbe(connectionHelper, app, logger)

    def _count_rows(self, query: str) -> int:
        # Drop the header row if present (app.doJob returns [header, *rows]).
        return self._row_probe.count_rows(query)

    def run(self,
            qh: str,
            qe: Optional[str],
            min_card: Optional[Dict[str, int]] = None,
            instances: Optional[List] = None) -> Dict:
        """Compare Qh and Q_E cardinalities; record any divergence.

        Returns a dict suitable for logging / pipeline.info."""
        report = {
            "qh_rows": None,
            "qe_rows": None,
            "ratio": None,
            "min_card": dict(min_card) if min_card else {},
            "multi_instance_tables": [],
            "warnings": [],
        }

        # 1. min_card-based detection (already done in Phase 1; surfaced here).
        if min_card:
            for t, k in min_card.items():
                if k > 1:
                    report["multi_instance_tables"].append({"table": t, "min_card": k})
                    if k > 2:
                        report["warnings"].append(
                            f"min_card[{t}] = {k} > 2 — only k <= 2 is supported by "
                            f"this phase; extracted query may be incorrect."
                        )

        # 2. Cardinality comparison. Only meaningful if both queries are present.
        if not qe:
            return report

        self.qh_rows = self._count_rows(qh)
        self.qe_rows = self._count_rows(qe)
        report["qh_rows"] = self.qh_rows
        report["qe_rows"] = self.qe_rows

        if self.qh_rows < 0 or self.qe_rows < 0:
            report["warnings"].append("Could not count one or both queries' result rows.")
            return report

        if self.qe_rows == 0:
            if self.qh_rows > 0:
                report["warnings"].append(
                    f"Extracted query returned 0 rows while Qh returned {self.qh_rows}. "
                    f"Extraction likely incomplete."
                )
            report["ratio"] = None
            return report

        ratio = self.qh_rows / float(self.qe_rows)
        report["ratio"] = ratio
        # A ratio >= 2 is a heuristic signal that Qh produces a cross-product
        # the extracted query missed — often a self-join. The threshold of 2
        # matches the Phase plan's k = 2 scope.
        if ratio >= 2.0 - 1e-9 and not report["multi_instance_tables"]:
            report["warnings"].append(
                f"Qh returned {self.qh_rows} rows vs extracted Q_E {self.qe_rows} "
                f"(ratio {ratio:.2f}). Possible missed multi-instance / self-join "
                f"masked by aggregation or DISTINCT."
            )
        return report

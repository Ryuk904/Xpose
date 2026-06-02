import logging
import math
import unittest

from mysite.unmasque.src.core.limit import Limit


def make_oracle(limit, baseline=1):
    """Synthetic black-box cardinality oracle.

    Models the real probe signal: inserting ``m`` matching rows makes the pre-LIMIT result
    grow linearly, and a LIMIT L clips it at L. The stage's normalization is
    ``fresh = len(result) - rmin_card + 2``; on an ideal D1 that is ``min(m, L) + baseline``
    where ``baseline`` is the header/D1 offset. A query with no LIMIT never plateaus.

    Returns (probe_fn, calls) where ``calls`` records every probed insert-count so the test
    can assert the probe budget is O(log L).
    """
    calls = []

    def probe(m):
        calls.append(m)
        effective = m if limit is None else min(m, limit)
        return effective + baseline

    return probe, calls


def make_search_only_limit(no_rows):
    """Build a Limit object without touching the DB plumbing, wired just enough to run the
    pure search core (`__search_limit`). Only `no_rows` and `logger` are consulted there."""
    lm = Limit.__new__(Limit)
    lm.no_rows = no_rows
    lm.logger = logging.getLogger("LimitSearchTest")
    lm.logger.addHandler(logging.NullHandler())
    return lm


def run_search(lm, probe, bounded=False):
    # name-mangled private method
    return lm._Limit__search_limit(probe, bounded)


class LimitSearchTest(unittest.TestCase):

    def test_small_limit(self):
        lm = make_search_only_limit(no_rows=1000)
        probe, calls = make_oracle(limit=10)
        self.assertEqual(10, run_search(lm, probe, bounded=False))

    def test_limit_at_legacy_cap(self):
        # The old linear scan dropped LIMIT == no_rows to None; the plateau search recovers it.
        lm = make_search_only_limit(no_rows=1000)
        probe, calls = make_oracle(limit=1000)
        self.assertEqual(1000, run_search(lm, probe, bounded=False))

    def test_limit_well_past_legacy_cap_5000(self):
        # The headline WI-02 case: a LIMIT far above the default 1000 cap, detected because the
        # budget for the unbounded (no-group-by) case runs to 2*no_rows.
        lm = make_search_only_limit(no_rows=5000)
        probe, calls = make_oracle(limit=5000)
        self.assertEqual(5000, run_search(lm, probe, bounded=False))
        # O(log L): exponential (~log2 2L) + binary (~log2 L) probes, nowhere near inserting 5000.
        self.assertLess(len(calls), 40)
        self.assertLessEqual(max(calls), 2 * lm.no_rows)

    def test_no_limit_returns_none(self):
        lm = make_search_only_limit(no_rows=2000)
        probe, calls = make_oracle(limit=None)  # cardinality grows forever
        self.assertIsNone(run_search(lm, probe, bounded=False))

    def test_limit_beyond_budget_returns_none(self):
        # LIMIT larger than what the budget (2*no_rows) can confirm -> not observable, None.
        lm = make_search_only_limit(no_rows=1000)
        probe, calls = make_oracle(limit=9000)
        self.assertIsNone(run_search(lm, probe, bounded=False))

    def test_group_bounded_edge_single_shot(self):
        # Grouped query: at most `no_rows` distinct group rows. A LIMIT just under that bound is
        # read single-shot at the budget edge (no confirming second probe is possible).
        lm = make_search_only_limit(no_rows=1000)
        probe, calls = make_oracle(limit=950)
        self.assertEqual(950, run_search(lm, probe, bounded=True))

    def test_group_bounded_no_limit_is_none(self):
        # Grouped, LIMIT >= number of groups: indistinguishable from no LIMIT for this D -> None.
        lm = make_search_only_limit(no_rows=1000)
        probe, calls = make_oracle(limit=1000)
        self.assertIsNone(run_search(lm, probe, bounded=True))

    def test_tiny_limit_below_floor_is_none(self):
        # Limits of 1/2 collapse into the D1 baseline (fresh < 4); preserved observability floor.
        lm = make_search_only_limit(no_rows=1000)
        probe, calls = make_oracle(limit=2)
        self.assertIsNone(run_search(lm, probe, bounded=False))

    def test_probe_count_is_logarithmic(self):
        # Across a sweep of limits the probe count stays logarithmic in L.
        for L in [16, 64, 500, 3000, 7000]:
            lm = make_search_only_limit(no_rows=8192)
            probe, calls = make_oracle(limit=L)
            self.assertEqual(L, run_search(lm, probe, bounded=False))
            # generous logarithmic bound: exp + binary phases
            self.assertLess(len(calls), 6 * math.ceil(math.log2(L + 2)) + 10,
                            f"probe count {len(calls)} not logarithmic for L={L}")


if __name__ == '__main__':
    unittest.main()

# Gap-Aware Disjunction Extraction — Handoff

Notes for the next chat working on this branch. Read [CLAUDE.md](CLAUDE.md) first
for repo orientation, then this file for what we built and why.

> **v2 update (2026-05-25):** the NEP-style witness loop the original handoff
> filed as "Next-chat starting point 1" is now implemented in
> [src/core/gap_witness.py](mysite/unmasque/src/core/gap_witness.py) and is the
> primary path inside `_refine_with_gap_search`. v1's data-witness sampling and
> midpoint bisection are retained as fallbacks. See the **v2 design** section
> below.

## Problem we set out to solve

When the hidden predicate on an attribute `A` is a union of disjoint intervals
(e.g. `A∈[10,20] ∨ A∈[30,40]`), the existing WHERE-clause extractor in
[mysite/unmasque/src/core/filter.py](mysite/unmasque/src/core/filter.py) does
one of two wrong things:

1. **Over-approximation** — the binary search in
   [`get_filter_value`](mysite/unmasque/src/core/filter.py#L219) bisects from a
   satisfying dmin value outward; mid-points can land inside the other interval,
   so it converges to a single envelope like `[~10, ~40]` and silently merges
   the gap.
2. **Silent drop** — when both domain extremes satisfy (e.g. `A < 10 OR A ≥ 50`,
   so `Pop(A = MIN_INT) = Pop(A = MAX_INT) = true`), `handle_point_filter` falls
   through with no `elif` branch and emits *no predicate at all* on that
   attribute.

## What the user proposed and what we verified

The user proposed a `Re−Rh` witness-oracle algorithm with full pseudocode plus
optimizations (cache `Rh` once per clause, short-circuit at first witness,
incremental `Re`, push-down range predicate, parallel attribute attribution).
See the plan file at `~/.claude/plans/hey-i-want-you-cheerful-allen.md`
(Part B) for the per-section verification.

Quick summary:

- **Correct**: `Re − Rh` produces gap witnesses; PVSA probe attributes the gap
  to the right attribute; gap-edge bisection is O(log domain); termination is
  guaranteed; all optimizations are sound.
- **Needed adjustment**: witness-attribute access breaks under joins/aggregation
  (the witness row may not project A); A5-W (witness-in-D) needs a fallback;
  NULL substitution requires nullable columns; predicate representation needs
  to accommodate AOA/render consumers; initial `Qe` shape matters.

## What we actually built

**Deviation from user's pseudocode**: instead of materializing `Rh` in a temp
table and running anti-join probes (the user's `Re − Rh` machinery), we use
**data-witness D¹ Pop-probing** — sample up to N distinct A-values from the
original D, probe each on the minimized D¹, and build maximal contiguous
satisfying intervals from the observed SAT/UNSAT pattern. Same A5-W principle,
no temp-table infrastructure needed. Full Re-Rh remains a clean v2 if
witness-in-D ever fails.

### Files changed

| File | Change |
|---|---|
| [mysite/config.ini](mysite/config.ini) | Added `gap_aware = no` under `[feature]`. |
| [mysite/unmasque/src/util/constants.py](mysite/unmasque/src/util/constants.py) | Added `DETECT_GAP_AWARE = "gap_aware"`. |
| [mysite/unmasque/src/util/configParser.py](mysite/unmasque/src/util/configParser.py) | Parses `gap_aware` into `config.detect_gap_aware` (default False). |
| [mysite/unmasque/src/core/filter.py](mysite/unmasque/src/core/filter.py) | New methods: `_refine_with_gap_search`, `_refine_by_data_witness`, `_fetch_distinct_values`, `_distinct_value_table_candidates`, `_find_gaps_recursive` (depth-bounded speculative midpoint bisection as fallback), `_binsearch_first_sat`, `_binsearch_last_sat`, `_coalesce_intervals`, `_emit_range_intervals`. Wired into `handle_point_filter`'s `not min, not max` branch AND a new `elif self._gap_aware_enabled():` for the silently-dropped both-extremes case; also into `handle_precision_filter`. Added `self.disjunctive_ranges = {}` (the side channel). |
| [mysite/unmasque/src/util/QueryStringGenerator.py](mysite/unmasque/src/util/QueryStringGenerator.py) | New `disjunctive_ranges` field on `QueryDetails` + property/setter on `QueryStringGenerator`. `__generate_arithmetic_pure_conjunctions` checks `disjunctive_ranges` per (tab, attr) and renders `(p1 OR p2 OR ...)` from the stored sub-intervals; falls back to standard `formulate_predicate_from_filter` when no disjunction is recorded. |
| [mysite/unmasque/src/pipeline/ExtractionPipeLine.py](mysite/unmasque/src/pipeline/ExtractionPipeLine.py) | Sets `q_generator.disjunctive_ranges = filter_extractor.disjunctive_ranges` immediately before `formulate_query_string()`. |
| [mysite/unmasque/src/core/aoa.py](mysite/unmasque/src/core/aoa.py) | `__what_is_concrete_bound_val` returns the envelope bound (`max(ub)` for UB, `min(lb)` for LB) when multiple range tuples on the same (tab, attr) exist — defensive even though Filter now emits a single envelope. |
| [CLAUDE.md](CLAUDE.md) | New project-level notes; documents the within-attribute OR-of-intervals handling. |
| [mysite/pkfkrelations_lineitem.csv](mysite/pkfkrelations_lineitem.csv) | **Test-only.** Lineitem-flavored PK/FK file. Needed because the checked-in `pkfkrelations.csv` references `web_lineitem`, which is not in the local TPC-H install. Point `config.pkfk` to this file when testing. |

### Why the side-channel dict (critical context)

We originally tried emitting multiple `(tab, A, 'range', lb, ub)` tuples per
attribute (one per sub-interval). This corrupted in AOA:
[`put_into_aoa_dict`](mysite/unmasque/src/util/aoa_utils.py) takes the
*intersection* (`max(lbs), min(ubs)`) when it sees multiple bounds on the same
attribute, turning `[(0,4), (21,24)]` into the inverted tuple `(21, 4)`. The
final rendered SQL came out as `between 21 and 4`.

Fix: Filter emits the **envelope tuple** `(t, A, 'range', 0, 24)` to
`filter_predicates` so AOA's intersection reasoning sees a single contiguous
range, and stores `disjunctive_ranges[(t, A)] = [(0, 4), (21, 24)]` separately.
ExtractionPipeLine plumbs the side channel into `q_generator`, and the render
path consults it to emit `(t.A between 0 and 4 OR t.A between 21 and 24)`.

### Gap-search algorithm details

`_refine_with_gap_search(tab, attr, datatype, lo, hi, query)`:

1. **First pass — data-witness sampling** ([`_refine_by_data_witness`](mysite/unmasque/src/core/filter.py)):
   - Query up to 200 distinct A-values from the original D in `[lo, hi]`. Tries
     `public.<tab>_unmasque_FromClause` first (the FromClause-renamed original),
     then `public.<tab>`, then `unmasque.<tab>`.
   - Probe each sampled value via `checkAttribValueEffect` on D¹.
   - Walk the sorted samples; each maximal run of SAT-flagged values becomes
     `(min_sat_in_run, max_sat_in_run)`. Coalesce adjacent intervals.
   - Returns a list of intervals OR `None` if no data was found.
2. **Fallback — speculative midpoint bisection** ([`_find_gaps_recursive`](mysite/unmasque/src/core/filter.py)):
   - Depth-bounded: `_gap_search_explore_depth = 6` for "mid is satisfying"
     branching (2⁶ = 64 leaves max), `_gap_search_discover_depth = 16` after a
     gap witness has been found.
   - Used only when sampling returns no useful disjunction.
3. **Emission** ([`_emit_range_intervals`](mysite/unmasque/src/core/filter.py)):
   - 1 interval → just `(t, A, 'range', lb, ub)`.
   - >1 intervals → envelope tuple to `filter_predicates`, full list to
     `disjunctive_ranges[(t, A)]`.

## Verification status

### Algorithmic (offline, stubbed Pop oracle)

| Predicate | Search range | Result | Probes |
|---|---|---|---|
| `A<10 ∨ A∈[24,42] ∨ A≥50` (DQ3-shape) | `[1, 60]` | `[(1,9), (24,42), (50,60)]` | 51 |
| `(124<A<135) ∨ (235<A<370) ∨ A>460` (DQ7-shape) | `[125, 600]` | `[(125,134), (236,369), (461,600)]` | 97 |
| `A∈[10,20] ∨ A∈[30,40]` | `[10, 40]` | `[(10,20), (30,40)]` | 27 |
| `A ≠ 25` in `[1, 100]` (singleton gap) | `[1, 100]` | `[(1,24), (26,100)]` | 87 |
| `A ∈ [50, 150]` (no gaps) | `[50, 150]` | `[(50, 150)]` | 63 |
| `A∈[1,10] ∨ A∈[90,100]` (near edges) | `[1, 100]` | `[(1,10), (90,100)]` | 29 |
| Always true | `[MIN_INT, MAX_INT]` | `[(MIN_INT, MAX_INT)]` | 63 (no runaway) |

### End-to-end on TPC-H (Postgres)

| Hidden query | `gap_aware` | Result | Status |
|---|---|---|---|
| `n_nationkey < 5 OR n_nationkey > 20` | yes | `(n_nationkey between 0 and 4 OR n_nationkey between 21 and 24)` | ✓ Disjunction recovered |
| same | no | (no WHERE) | ✓ Silent-drop preserved (no regression) |
| `n_nationkey BETWEEN 5 AND 15` | yes | `n_nationkey between 5 and 15` | ✓ No spurious disjunction |
| `n_nationkey > 10` | yes | `n_nationkey >= 11` | ✓ One-sided predicate unchanged |

## Known v1 limitations

1. **Bisection-converges-to-single-disjunct case is not handled by gap-aware
   alone.** When the initial binary search trajectory converges to one disjunct
   entirely (e.g. dmin lands such that lower bisection's mid-trajectory
   skips over `[0,5]` and lands at `[20,24]`), gap-aware has no envelope to
   refine and finds nothing. The existing `detect_or` (Sumang-style
   row-subtraction loop, gated by `or = yes`) is the *complementary* mechanism
   for that case — use both flags together for full coverage.
2. **Data-witness depends on original-D access.** The helper tries
   `public.<tab>_unmasque_FromClause`, `public.<tab>`, `unmasque.<tab>` in
   order. Works for the standard ExtractionPipeLine. The Union and OuterJoin
   pipelines may rename tables differently — not verified.
3. **String IN-list disjunctions** (e.g. `p_brand IN ('Brand#52','Brand#12')`)
   are out of v1 scope — go through the existing `handle_string_filter` and
   `DisjunctionPipeLine`.
4. **Speculative midpoint bisection fallback** can miss gaps narrower than
   ~1/64 of the searched interval. Acceptable for the test cases we ran;
   would matter if data-sampling returned no useful witness.
5. **Test setup workaround**: `pkfkrelations_lineitem.csv` was created because
   the checked-in `pkfkrelations.csv` references `web_lineitem` which doesn't
   exist in the local TPC-H. Pass `conn.config.pkfk = "pkfkrelations_lineitem.csv"`
   when running tests.

## Reproducible verification commands

From [mysite/](mysite/), with `.venv` set up and Postgres reachable:

```bash
# Algorithm-level (no DB needed):
../.venv/bin/python <<'EOF'
import sys; sys.path.insert(0, '.')
from unmasque.src.core.filter import Filter
class Fake(Filter):
    def __init__(self, fn):
        self._predicate = fn
        class _C:
            class config: detect_gap_aware = True
        self.connectionHelper = _C()
    def checkAttribValueEffect(self, q, val, al):
        v = val.strip("'") if isinstance(val, str) else val
        try: v = int(v)
        except: v = float(v)
        return self._predicate(v)
ff = Fake(lambda v: v < 10 or 24 <= v <= 42 or v >= 50)
print(ff._refine_with_gap_search('t','A','int',1,60,None))  # → [(1,9),(24,42),(50,60)]
EOF

# End-to-end (requires TPC-H Postgres at localhost:5432, db=tpch):
../.venv/bin/python <<'EOF'
import sys; sys.path.insert(0, '.')
from unmasque.src.util.ConnectionFactory import ConnectionHelperFactory
from unmasque.src.core.factory.PipeLineFactory import PipeLineFactory
from unmasque.src.pipeline.abstract.TpchSanitizer import TpchSanitizer
san = ConnectionHelperFactory().createConnectionHelper()
san.config.pkfk = "pkfkrelations_lineitem.csv"
san.connectUsingParams(); TpchSanitizer(san).sanitize(); san.closeConnection()
conn = ConnectionHelperFactory().createConnectionHelper()
conn.config.pkfk = "pkfkrelations_lineitem.csv"
conn.config.detect_gap_aware = True
Q = "SELECT n_name FROM nation WHERE n_nationkey < 5 OR n_nationkey > 20;"
f = PipeLineFactory(); t = f.init_job(conn, Q); f.doJob(Q, t)
print(f.result)
EOF
```

## v2 design (now implemented)

### Algorithm

For each attribute A with a discovered envelope `[lo, hi]`:

1. **Swap to full D.** Rename working_schema `<tab>` aside as
   `<tab>_unmasque_GapWitness_bkp`, clone `user_schema.<tab>` into
   working_schema `<tab>`. Repeat for every from-clause table so multi-table
   Qh sees full row populations.
2. **Capture Qh's projection columns** by running Qh once and reading the
   result header. Validate they are bare column names; bail to v1 fallback
   if Qh projects an expression we can't safely splice into Re.
3. **Build Re** = `SELECT <qh_cols> FROM <from> AS aliases WHERE A IN <intervals>`
   so the EXCEPT ALL diff against Qh's result is meaningful (matching schemas).
4. **Witness search** = `count(Re EXCEPT ALL Rh) > 0`. If empty, no gap →
   return current intervals. Else ctid-bisect working `<tab>` until a single
   witness row remains; read its A-value directly.
5. **Pop pre-check** — guard against false witnesses: the comparator diff
   tells us "this base-table row is in Re but not in Rh", but the row's gap
   may belong to a *different* attribute. Run `checkAttribValueEffect` with
   `A = witness_val` on the now-degraded working table; if Pop is still true,
   this attribute doesn't constrain Qh at this value — abort the split.
6. **Bisect outward** for gap edges via the existing `_binsearch_last_sat`
   and `_binsearch_first_sat`. Replace the containing interval with the two
   surrounding sub-intervals.
7. **Loop** — run step 3 again with the refined intervals. Repeats until the
   diff is empty (no more gaps inside discovered intervals). Cross-attribute
   missed disjuncts are NOT v2's concern — they're handled by Sumang's
   negation loop in [`DisjunctionPipeLine`](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py).
8. **Teardown** — drop the swapped tables and rename the D-min backups back,
   restoring Filter's D¹ state.

### Files changed for v2

| File | Change |
|---|---|
| [src/core/gap_witness.py](mysite/unmasque/src/core/gap_witness.py) | **New.** `GapWitnessFinder` class: full-D swap, Qh projection capture, comparator diff, ctid-bisection, witness-attr read, teardown. |
| [src/core/filter.py](mysite/unmasque/src/core/filter.py) | Added `_refine_by_nep_witness` (witness loop driver) and `_split_interval_on_witness` (Pop precheck + edge bisection + interval splitting). Modified `_refine_with_gap_search` to call v2 first, then fall through to v1's sampling and midpoint bisection. Threaded `filter_predicates_so_far` through the three call sites. |

### Why the projection capture matters

`Comparator.match` creates `r_h LIKE r_e` and inserts Qh's result rows into
it. If Re is `SELECT *` but Qh projects fewer columns, the inserted r_h rows
have NULLs for the missing columns. `Re EXCEPT ALL Rh` then never matches
any row (Re's are populated, Rh's are mostly NULL) so the diff is the whole
of Re, and ctid bisection lands on whatever row happens to be first —
producing a false witness. v2's first cut hit this bug and emitted a spurious
gap on `n_regionkey` for the canonical `n_nationkey < 5 OR n_nationkey > 20`
test. Capturing Qh's projection and matching Re's SELECT list to it fixes the
diff. The Pop-precheck step is the additional safety net.

### Verification (re-run)

| Hidden query | `gap_aware` | Result | Status |
|---|---|---|---|
| `n_nationkey < 5 OR n_nationkey > 20` | yes | `(n_nationkey <= 4 OR n_nationkey >= 21)` | ✓ Disjunction recovered |
| same | no | (no WHERE) | ✓ Silent-drop preserved |
| `n_nationkey BETWEEN 5 AND 15` | yes | `n_nationkey between 5 and 15` | ✓ No spurious disjunction |
| `n_nationkey > 10` | yes | `n_nationkey >= 11` | ✓ One-sided unchanged |

Run via `mysite/` venv with the snippet in **Reproducible verification commands**
below (now applies to v2 since it's the primary path).

## Next-chat starting points (v3 ideas)

1. **Done in v2:** Full `Re − Rh` materialization, now in
   [gap_witness.py](mysite/unmasque/src/core/gap_witness.py).
2. **Dropped (per user direction 2026-05-25):** "Trajectory-converges-to-
   single-disjunct" outside-envelope sampling. Sumang's negation (the
   existing falsify-and-rerun loop in
   [DisjunctionPipeLine](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py))
   already covers this — if bisection lands inside one disjunct, negating
   the discovered union and re-running the minimizer surfaces a witness in
   the missed disjunct.
3. **String IN-list disjunctions** — still future work. Could be tackled
   directly by Sumang's negation (per user), or by extending
   `_refine_by_data_witness` with string-aware sampling.
4. **Cleanup**: `handle_filter_for_subrange` at
   [filter.py:110-143](mysite/unmasque/src/core/filter.py#L110) is dead
   code per [CLAUDE.md](CLAUDE.md). Could be removed.
5. **Multi-table v2 hardening:** v2's `_capture_qh_cols` bails on aggregate
   queries because Re can't mirror Qh's GROUP BY without parsing. For
   aggregate Qh, v1 sampling currently picks up the work. A v3 could
   sidestep this by running the comparator diff over `<from>.*` (full base
   table projection) on both Re and Qh wrapped as `SELECT base_cols FROM
   (Qh) WHERE TRUE` — requires Qh's result to include join keys, which is
   often false.
6. **Pop-precheck destructiveness:** during v2's bisect-outward step,
   `checkAttribValueEffect` UPDATEs the working table's attr column
   uniformly. Subsequent bisection iterations see this degraded state for
   the attr. Today this is fine because (a) we restore D¹ on teardown per
   attribute and (b) Qh's WHERE on the canonical test query only references
   the attribute under refinement. Multi-attribute Qh queries could be
   affected — worth re-verifying as test coverage grows.

## Pointers

- Plan file (with original verification): `~/.claude/plans/hey-i-want-you-cheerful-allen.md`
- Sample disjunction test queries: [mysite/unmasque/test/disjunction/test_queries.sql](mysite/unmasque/test/disjunction/test_queries.sql) (DQ1–DQ9)
- The existing cross-predicate OR loop (complementary to gap-aware): [DisjunctionPipeLine.\_\_run_extraction_loop](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py#L191)

# Gap-Aware Disjunction Extraction in Xpose: a v2 Re−Rh Witness Oracle

**Project:** Xpose (UNMASQUE-style hidden-query extraction)
**Branch:** `master`
**Author:** Abhishek
**Date:** 2026-05-25

---

## Abstract

Xpose is a black-box SQL extraction framework: given a hidden query `Q_h`
that can be run against a database but whose text is unknown, it
reconstructs `Q_h` stage by stage. Its Filter stage discovers WHERE-clause
constants by binary-searching mutated values on a minimized one-row
instance `D¹`. When the hidden predicate on an attribute `A` is a union of
disjoint intervals (e.g. `A ∈ [10,20] ∪ [30,40]`), the binary search either
over-approximates (collapses the gap) or silently drops the predicate
entirely.

The original handoff document for this branch proposed a `Re − Rh` witness
oracle to recover such disjunctions. v1 of that work used distinct-value
sampling on `D¹` as a tractable approximation, but the approach was
fragile: it required custom backup-view access and could not recover gaps
in attributes that did not appear in `Q_h`'s SELECT clause.

This report describes **v2**, which implements the full `Re − Rh` oracle
by reusing the existing NEP (Not-Equal Predicate) machinery — specifically
the Comparator's `EXCEPT ALL` diff and the Minimizer's `ctid` bisection —
to locate a single witness row whose attribute value lies inside the gap.
Because the bisection operates on the base table rather than on `Q_h`'s
result, the witness's `A`-value is read directly from the base row, so
`A` need not appear in `Q_h`'s projection. A Pop pre-check rejects false
witnesses whose actual gap belongs to a different attribute.

v2 has been integrated into Filter as the primary resolution path, with
the v1 sampling and midpoint-bisection methods retained as fallbacks. All
four pre-existing TPC-H regression cases continue to pass; the canonical
`n_nationkey < 5 OR n_nationkey > 20` test now resolves to the correct
`(n_nationkey ≤ 4 OR n_nationkey ≥ 21)` via the v2 path.

---

## 1. Introduction

### 1.1 Xpose

Xpose is a hidden-query extraction framework following the UNMASQUE
methodology. The framework is given:

- A black-box query `Q_h` (text unknown, but executable).
- A working schema with the same tables as the schema `Q_h` references.

It reconstructs `Q_h` by running `Q_h` against various mutated database
states and observing the result (specifically, whether the result is
non-empty — the *Pop* oracle). Each pipeline stage extracts one syntactic
piece of `Q_h`:

| Stage | Module | What it discovers |
|---|---|---|
| Initialization | `initialization.py` | schema, cardinalities |
| DB restore | `db_restorer.py` | full-D state |
| Correlated sampling (Cs2) | `cs2.py` | seed satisfying tuples |
| View minimization | `view_minimizer.py` | one-row instance `D¹` |
| From / Join | `from_clause.py`, `equi_join.py` | FROM-clause tables and equi-join graph |
| **Filter** | `filter.py` | WHERE-clause constants |
| AOA | `aoa.py` | inequality predicates |
| Projection | `projection.py` | SELECT columns |
| Aggregation, GROUP BY, HAVING | `aggregation.py` | aggregates |
| ORDER BY | `orderby_clause.py` | ordering |
| LIMIT | `limit.py` | row limits |

Pipeline orchestration lives in `ExtractionPipeLine.py`. A
`DisjunctionPipeLine` falsify-and-rerun loop layers over the whole
pipeline to handle cross-predicate `OR` clauses by repeatedly invalidating
the discovered predicates and finding new ones (Sumang's negation
approach).

### 1.2 The within-attribute OR-of-intervals problem

When the hidden predicate on an attribute `A` is a *single* contiguous
range (e.g. `A BETWEEN 5 AND 15`), Filter's binary search converges
correctly: it bisects outward from a satisfying seed value, narrowing on
the left and right edges by repeated Pop probes on `D¹`.

When the hidden predicate is a *union* of disjoint intervals on the same
attribute (e.g. `A ∈ [10,20] ∪ [30,40]`), the existing binary search has
two failure modes:

**Failure mode 1 — Over-approximation.** From a seed inside one disjunct
(say `A = 15`), bisection's midpoints land *inside the other interval*
(e.g. midpoint `35` is also Pop-true), so the search converges on a single
envelope `[~10, ~40]` that silently merges the gap `(20, 30)`.

**Failure mode 2 — Silent drop.** When the hidden predicate spans both
domain extremes (e.g. `A < 5 OR A > 20`), both `Pop(A = INT_MIN)` and
`Pop(A = INT_MAX)` are true. The `handle_point_filter` routine's
condition cascade has no branch for that case and emits no predicate at
all on `A` — the WHERE clause loses the constraint entirely.

Both modes corrupt extraction. v1 of the gap-aware feature addressed them
behind a `gap_aware = yes` flag with a data-witness sampling heuristic;
this report describes v2, which replaces that heuristic with a principled
`Re − Rh` witness oracle.

---

## 2. Background and Building Blocks

The v2 design reuses existing Xpose machinery rather than introducing new
DB infrastructure. The relevant building blocks:

### 2.1 The Comparator (`EXCEPT ALL` diff)

`Comparator.match(Q_h, Q_E)` (in `pipeline/abstract/Comparator.py`) takes
two SQL queries and returns whether their result sets agree. Internally:

1. Creates a view `r_e` from `Q_E`.
2. Creates a table `r_h LIKE r_e` (so `r_h` inherits `r_e`'s schema).
3. Runs `Q_h`, inserts the result rows into `r_h`.
4. Runs `(r_e EXCEPT ALL r_h)` and `(r_h EXCEPT ALL r_e)` and counts rows.
5. Returns `True` if both counts are zero.

Note the schema invariant: because `r_h LIKE r_e`, the two tables must
have identical columns for the diff to be meaningful — a constraint that
becomes important in §4.3 below.

### 2.2 Minimizer ctid bisection

`Minimizer.reduce_Database_Instance` (in `core/abstract/MinimizerBase.py`,
specialized in `core/nep.py` for NEP and in `core/view_minimizer.py` for
view minimization) bisects a working table by `ctid` ranges. At each step
it asks a `check_result_for_half` predicate whether a particular half
contains the row(s) it is looking for, then keeps that half and recurses.
The result is a single-row table whose row is the *witness* the
minimizer was searching for.

The bisection skeleton is generic; what makes a minimizer specific to
NEP, ViewMinimizer, etc. is the `check_result_for_half` predicate it
overrides.

### 2.3 NEP feature

NEP (Not-Equal Predicate) discovers `A <> v` predicates that the main
Filter stage misses because `<>` is not extracted by Filter's binary
search. Its flow:

1. Run the extracted query `Q_E` and `Q_h`; if they agree (comparator
   returns true), NEP exits — no `<>` predicates exist.
2. Otherwise, restore full D, then `ctid`-bisect to a single row that
   causes the diff.
3. On that witness row, for each attribute, mutate to a different value
   and re-run `Q_h`. If `Q_h` becomes non-empty after the mutation, the
   original value was being negated by a `<>` predicate.

For v2, the key reusable idea from NEP is steps 1–2: comparator-driven
diff plus `ctid` bisection to locate a single base-table witness row.

### 2.4 DisjunctionPipeLine and Sumang's negation

Cross-attribute disjunction — e.g. `(A = 1) OR (B = 2)` — is handled by
the DisjunctionPipeLine's falsify-and-rerun loop, gated by the `or = yes`
flag. After each iteration of the main pipeline yields a discovered
predicate, the loop negates it and re-runs the minimizer, forcing a new
`D¹` that satisfies `Q_h` via a different disjunct. The loop terminates
when negation produces no new `D¹`.

This mechanism is orthogonal to v2: v2 handles *within-attribute* gaps,
DisjunctionPipeLine handles *cross-attribute* disjuncts. Both can be
enabled simultaneously (`gap_aware = yes` and `or = yes`) without
interaction. v2 does **not** require `or = yes`.

---

## 3. Design

### 3.1 The Re−Rh witness oracle

The original handoff document proposed the following oracle:

> Let `Re` = the set of rows accepted by the extracted query in its
> current state. Let `Rh` = the set of rows accepted by the hidden
> query. Any row in `Re − Rh` is a *gap witness*: extracted-current
> accepts it but hidden does not. The witness's value of `A` lies in a
> gap of `A`'s hidden predicate, and that gap can be located by ordinary
> bisection from the witness.

If a gap row exists in the database, the oracle is sound. The question
is how to implement `Re − Rh` such that:

(a) The diff is well-defined (matching schemas).
(b) We can recover the witness's `A`-value when `A` is not in `Q_h`'s
    projection.
(c) The diff sees a sufficient base-row population to make `Re` and `Rh`
    meaningful (i.e. not just `D¹`, which is a single row).

### 3.2 Why projection-based diff doesn't generalize

A natural first attempt is the "project both to `A` and set-diff" form
that was discussed during design:

```
Re_A := SELECT DISTINCT A FROM user_schema.T WHERE <discovered_intervals>
Rh_A := SELECT DISTINCT A FROM (Q_h)   -- requires A in Q_h's SELECT
gap_values := Re_A EXCEPT ALL Rh_A
```

This works whenever `A` appears in `Q_h`'s projection. In TPC-H and most
real workloads, however, the filter attributes are *not* in `Q_h`'s
SELECT — queries typically project descriptive columns (`c_name`,
`n_name`) and filter on key/numeric columns (`c_acctbal`, `n_nationkey`).
For such cases this approach degenerates.

### 3.3 NEP-style row-level witness via ctid bisection

The NEP feature, by contrast, locates its witness as a *base-table row*
rather than a result-set row. After the comparator confirms a diff
exists, the minimizer's `ctid` bisection narrows the working table down
to a single row that, when present, still causes the diff. The row sits
in the base table and **all its attributes are readable directly**.

Adapting this to gap detection:

```
Re := SELECT <Q_h's projection> FROM <FROM-clause> WHERE A ∈ <intervals>
Comparator returns the diff. If non-empty, ctid-bisect <T> until size 1.
Read T.A from that single row → witness's A-value.
```

The witness's `A`-value seeds outward bisection (existing
`_binsearch_first_sat` / `_last_sat` helpers in Filter) to locate the
gap's left and right edges. The discovered gap is excised from the
current interval and the loop iterates: with the refined intervals in
hand, recompute `Re − Rh` to check for further gaps inside the discovered
intervals.

### 3.4 The Pop pre-check

A subtle complication: the comparator diff identifies a row `r` such that
`r ∈ Re ∧ r ∉ Rh`. The row's gap may be in `A` — but it may also be in
*some other attribute* of `r`. When Filter loops over all attributes of
the table and runs v2 on each in turn, the witness for an attribute
whose hidden predicate is trivially `TRUE` (no constraint) is a *false*
witness: the diff exists because of constraints on other columns, not
because the candidate attribute is in any gap.

To guard against this, v2 inserts a Pop pre-check between locating the
witness and bisecting its gap edges: mutate `D¹` so its row has
`A = witness_val`, run `Q_h`, observe Pop. If Pop is true,
`A = witness_val` is acceptable to `Q_h`, hence not in a gap of `A` —
abort the split for this attribute. This rejection step was empirically
necessary for the canonical regression test: without it, v2 emitted a
spurious gap on `n_regionkey` for the
`n_nationkey < 5 OR n_nationkey > 20` query.

### 3.5 The schema-matching requirement and Q_h projection capture

Comparator creates `r_h LIKE r_e`, which means `r_h` inherits whatever
schema `r_e` has. If `Q_E` is naïvely written as `SELECT * FROM ...`,
`r_e` ends up with all of the base table's columns. Inserting `Q_h`'s
result rows (which have only `Q_h`'s projection) leaves the other
columns NULL, and `r_e EXCEPT ALL r_h` matches no rows at all — the diff
is trivially the entirety of `Re`.

The fix: capture `Q_h`'s projection column names once at v2 setup (by
running `Q_h` and reading the result header) and use those column names
in `Q_E`'s SELECT clause. This requires `Q_h`'s projection to contain
bare column names; expressions, aggregates, and `*` cannot be safely
spliced into `Q_E` without parsing `Q_h`, so v2 bails out of those cases
and the v1 fallbacks handle them.

---

## 4. Algorithm

### Algorithm 1 — Top-level dispatch (`_refine_with_gap_search`)

```
INPUT:  table T, attribute A, datatype dt,
        current envelope [lo, hi], hidden query Q_h,
        running filter_predicates list F

OUTPUT: list of intervals [(lb_1, ub_1), ..., (lb_k, ub_k)]
        — k > 1 means a disjunction was discovered

1: if gap_aware flag is off: return [(lo, hi)]
2: (delta, cutoff) ← datatype constants

3: ── Primary path: v2 NEP-style witness loop ──
4: nep ← REFINE-BY-NEP-WITNESS(T, A, dt, lo, hi, Q_h, F)
5: if nep ≠ NULL and len(nep) > 1: return nep

6: ── Fallback 1: v1 data-witness sampling ──
7: sampled ← REFINE-BY-DATA-WITNESS(T, A, dt, lo, hi, Q_h)
8: if sampled ≠ NULL and len(sampled) > 1: return sampled

9: ── Fallback 2: v1 speculative midpoint bisection ──
10: rec ← FIND-GAPS-RECURSIVE(T, A, dt, lo, hi, Q_h)
11: return COALESCE(rec)  if non-empty else [(lo, hi)]
```

### Algorithm 2 — v2 NEP-style witness loop (`_refine_by_nep_witness`)

```
INPUT:  T, A, dt, lo, hi, Q_h, F
OUTPUT: list of intervals, or NULL if v2 cannot run

1: if connection harness or app oracle unavailable: return NULL
2: finder ← GapWitnessFinder(T, A, dt, FROM_clause_tables)
3: if not finder.setup(Q_h): return NULL    // see Alg 3
4: try:
5:    intervals ← [(lo, hi)]
6:    for iter ← 1 .. MAX_ITERS do
7:       v ← finder.find_witness_value(intervals)        // Alg 4
8:       if v = NULL: break                              // no more gaps
9:       new_intervals ← SPLIT-INTERVAL-ON-WITNESS(intervals, T, A, dt, v, Q_h)
10:      if new_intervals = NULL or new_intervals = intervals: break
11:      intervals ← new_intervals
12:   return COALESCE(intervals)
13: finally:
14:    finder.teardown()                                 // restore D¹
```

### Algorithm 3 — GapWitnessFinder lifecycle

```
setup(Q_h):
  1: for each table t ∈ from_clause_tables:
  2:    rename working_schema.t  →  working_schema.t_unmasque_GapWitness_bkp
  3:    create working_schema.t LIKE user_schema.t
  4:    INSERT INTO working_schema.t SELECT * FROM user_schema.t
  5: run Q_h, capture result header as qh_cols
  6: if any c in qh_cols contains parens / spaces / commas / '*':
       roll back swaps, return False         // cannot splice into Q_E
  7: return True

teardown:
  1: drop r_e view, r_h table if present
  2: for each swapped t:
  3:    drop working_schema.t           // (corrupted by Pop mutations)
  4:    rename working_schema.t_unmasque_GapWitness_bkp  →  working_schema.t
```

### Algorithm 4 — `find_witness_value`

```
INPUT:  current intervals
OUTPUT: a value v of A such that some row in D has A = v and is rejected
        by Q_h, or NULL if no such row exists

1: Q_E ← "SELECT <qh_cols> FROM <FROM-clause> WHERE A IN <intervals>"
2: if NOT diff-nonempty(Q_E, Q_h): return NULL

3: ── ctid-bisect working_schema.T down to a witness row ──
4: while row_count(working_schema.T) > 1:
5:    (mid_1, mid_2) ← median ctid pair of working_schema.T
6:    if half-has-witness(start..mid_1, Q_E):
7:        restrict working_schema.T to that half
8:    else if half-has-witness(mid_2..end, Q_E):
9:        restrict working_schema.T to that half
10:   else: break
11: ctid_w ← any ctid in current working_schema.T
12: return T.A at ctid_w


half-has-witness(start_c, end_c, Q_E):
  1: temp-rename working_schema.T aside
  2: create working_schema.T = slice of aside table on ctid in [start_c, end_c]
  3: result ← diff-nonempty(Q_E, Q_h)
  4: restore: drop sliced T, rename aside table back to T
  5: return result


diff-nonempty(Q_E, Q_h):
  1: create view  r_e  AS  Q_E
  2: create table r_h  LIKE r_e
  3: insert Q_h's rows into r_h
  4: return ((r_e EXCEPT ALL r_h) has > 0 rows)
```

### Algorithm 5 — `SPLIT-INTERVAL-ON-WITNESS` (with Pop pre-check)

```
INPUT:  intervals, T, A, dt, witness_val v, Q_h, cutoff
OUTPUT: refined intervals, or NULL if no real gap at v

1: locate the interval (lb, ub) containing v
2: if no such interval: return NULL

3: ── Pop pre-check: is this a real gap on attribute A? ──
4: mutate D¹.A := v, run Q_h, capture Pop result, revert D¹.A
5: if Pop = true: return NULL              // false witness — gap is in another attr

6: gap_left  ← largest u ∈ [lb, v] with Pop(D¹.A := u) = true  (binsearch_last_sat)
7: gap_right ← smallest u ∈ [v, ub] with Pop(D¹.A := u) = true (binsearch_first_sat)
8: if gap_left ≥ gap_right (no room): return NULL

9: replace (lb, ub) in intervals with (lb, gap_left) and (gap_right, ub)
10: drop any zero-width or inverted sub-intervals
11: return updated intervals
```

---

## 5. Architecture and Flow Diagrams

### 5.1 Filter resolution chain

```
                +----------------------------------+
                |  Filter.handle_point_filter      |
                |  / handle_precision_filter       |
                |                                  |
                |  binary search yields envelope   |
                |  [lo, hi] for attribute A on T   |
                +-----------------+----------------+
                                  |
                                  v
                +-----------------+----------------+
                |   _refine_with_gap_search        |
                |     (only if gap_aware = yes)    |
                +-----------------+----------------+
                                  |
        +-------------------------+--------------------------+
        |                         |                          |
        v                         v                          v
 +--------------+         +----------------+        +-----------------+
 | v2: NEP-     |  miss   | v1: data-      | miss   | v1: midpoint    |
 | style        | ------> | witness        | -----> | bisection       |
 | witness loop |         | sampling       |        | _find_gaps_     |
 | (Alg 2)      |         | (Alg fallback) |        | recursive       |
 +-------+------+         +--------+-------+        +--------+--------+
         | hit                     | hit                     | hit
         v                         v                         v
        intervals (k>1)           intervals (k>1)           intervals (k>1)
                                  |
                                  v
                +-----------------+----------------+
                |  _emit_range_intervals           |
                |                                  |
                |  1 interval  → filter_predicates |
                |  k intervals → envelope tuple    |
                |                + disjunctive_    |
                |                  ranges side ch. |
                +-----------------+----------------+
                                  |
                                  v
                         AOA, Projection, ...
                         (envelope tuple is               render path
                         consumed; render path             consults the
                         consults side channel             side channel
                         for OR-of-BETWEEN)
```

### 5.2 v2 witness loop (single attribute)

```
                       +---------------------------+
                       |  REFINE_BY_NEP_WITNESS    |
                       |  attr A, envelope [lo,hi] |
                       +-------------+-------------+
                                     |
                                     v
                       +-------------+-------------+
                       |  GapWitnessFinder.setup   |
                       |   - swap T → full D       |
                       |   - capture qh_cols       |
                       +-------------+-------------+
                                     |
                  intervals = [(lo, hi)]
                                     |
                                     v
                    +----------------+----------------+
        +---------> |  find_witness_value(intervals)  |
        |           |                                 |
        |           |  Q_E = SELECT qh_cols FROM <FROM>
        |           |        WHERE A IN intervals     |
        |           +----------------+----------------+
        |                            |
        |                            v
        |              diff = (Q_E EXCEPT ALL Q_h) ?
        |                            |
        |             +--------------+-------------+
        |             |                            |
        |        non-empty                       empty
        |             |                            |
        |             v                            v
        |     ctid-bisect T to                 break loop
        |     single witness row
        |     v ← T.A at that ctid
        |             |
        |             v
        |     Pop(D¹.A := v) ?
        |             |
        |        +----+----+
        |        |         |
        |       true     false
        |        |         |
        |        v         v
        |    false       gap_left ← binsearch_last_sat(lb, v)
        |    witness     gap_right ← binsearch_first_sat(v, ub)
        |    break       intervals ← intervals with (lb,ub)
        |    loop          replaced by (lb,gap_left), (gap_right,ub)
        |                            |
        +----------------------------+
                                     |
                                     v
                       +-------------+-------------+
                       |  GapWitnessFinder.teardown|
                       |   - drop r_e, r_h         |
                       |   - drop swapped T's,     |
                       |     rename backups back   |
                       +-------------+-------------+
                                     |
                                     v
                              return intervals
```

### 5.3 Database state lifecycle

```
                   working_schema state         user_schema state
                   ─────────────────────        ─────────────────
  Filter start:    D¹ (1 row per table)         full D (untouched)

  v2 setup:        for each from-clause tab t:
                     rename t → t_GapWitness_bkp
                     create t LIKE us.t, copy rows
                   ── t is now full D ──        full D (untouched)

  v2 witness       (bisection slices t in       full D (untouched)
  search:          place; Pop pre-check
                   mutates t.A uniformly
                   then reverts to D¹.A's val)

  v2 teardown:     for each swapped t:
                     drop t (degraded)
                     rename t_GapWitness_bkp → t
                   ── t is D¹ again ──          full D (untouched)

  Filter cont'd:   D¹ (1 row per table)         full D (untouched)
                   — invariant restored —
```

### 5.4 Comparator's role inside diff-nonempty

```
       +-------------+         +------------------+
       |    Q_E      |         |       Q_h        |
       | (we built)  |         |  (user-supplied) |
       +------+------+         +---------+--------+
              |                          |
              | CREATE VIEW              | run app.doJob(Q_h)
              v                          | -> result rows
        +-----+----+                     |
        |  r_e     |  schema decided     v
        |  (view)  |  by Q_E           +-+--------+
        +-----+----+                   |  header  |  +  rows
              |                        +----------+
              | CREATE TABLE r_h LIKE r_e
              v
        +-----+----+   INSERT INTO r_h <header>
        |  r_h     |<-------------------- VALUES <rows>
        |  (table) |
        +-----+----+
              |
              v
        SELECT count(*) FROM (r_e EXCEPT ALL r_h)
              |
              v
          > 0  → witness exists in current slice
          = 0  → r_e ⊆ r_h, no gap rows here
```

---

## 6. Implementation

### 6.1 Files changed

| File | Status | Role |
|---|---|---|
| `mysite/unmasque/src/core/gap_witness.py` | new | `GapWitnessFinder` class — full-D swap, Q_h projection capture, comparator diff, ctid-bisection, witness-attr read, teardown. ~330 lines. |
| `mysite/unmasque/src/core/filter.py` | modified | Added `_refine_by_nep_witness` (driver) and `_split_interval_on_witness` (Pop pre-check, outward bisection, interval splitting). Modified `_refine_with_gap_search` to dispatch v2 first, then v1 sampling, then v1 bisection. Threaded `filter_predicates` through three call sites. |
| `mysite/config.ini` | modified | Replaced `web_lineitem` reference in `[table_sizes]` with `lineitem` so the local TPC-H sanitizer runs. |
| `mysite/pkfkrelations.csv` | modified | Same `web_lineitem → lineitem` substitution; `.bak` of original retained. |
| `CLAUDE.md` | modified | Updated the within-attribute OR section with the three-tier resolution chain. |
| `GAP_AWARE_HANDOFF.md` | modified | Added v2 design and verification sections. |

### 6.2 Integration points

v2 is invoked from three sites inside `filter.py`, corresponding to the
three cases where the binary search produces an envelope rather than a
tight bound:

| Site | Branch | When |
|---|---|---|
| `handle_point_filter`, `min_present and max_present` block | "both extremes satisfy" — silent-drop case | hidden predicate spans both domain extremes (Failure mode 2) |
| `handle_point_filter`, `not min_present and not max_present` block | "regular range" case | binary search converges to an envelope (Failure mode 1) |
| `handle_precision_filter`, `not min_present and not max_present` block | same for numeric/float | as above for non-integer types |

All three sites pass the running `filter_attribs` list to
`_refine_with_gap_search` so that downstream changes can incorporate
earlier-discovered predicates into `Q_E`'s WHERE if needed. (v2 today
does not splice them in — the cross-attribute correlation is already
expressed implicitly via `Q_h`'s WHERE in the diff — but the plumbing is
in place.)

### 6.3 DB state contract

v2's correctness depends on a strict swap-in / swap-out discipline:

- **Entry invariant:** working_schema.t holds the `D¹` representation
  (1 row) for every `t ∈ from_clause_tables`.
- **During v2:** working_schema.t holds full D (cloned from user_schema),
  possibly with one column uniformized by transient Pop probes during
  outward bisection.
- **Exit invariant:** working_schema.t holds `D¹` again. `user_schema.t`
  is never written to during v2.

The contract is enforced by `setup()` (which renames `D¹` aside before
cloning full D) and `teardown()` (which drops the degraded working table
and renames the `D¹` backup back). `teardown()` is called in a `finally`
block in `_refine_by_nep_witness` so even on an exception the `D¹` state
is restored.

### 6.4 Fall-through semantics

If any of the following holds, v2 bails (returns NULL) and Filter falls
through to v1:

- `connectionHelper.config.schema` or `user_schema` is missing (offline
  unit tests with stubbed harnesses).
- `Q_h`'s projection contains an expression (parentheses, `*`, comma in a
  column name, embedded whitespace).
- A swap or `CREATE TABLE LIKE` fails (e.g. the working table is a view
  rather than a table, leftover from a prior pipeline stage).
- The comparator returns an unexpected error.

v1's data-witness sampling has its own bail-out (no rows fetched from
user_schema), in which case the chain falls through to the speculative
midpoint bisection.

---

## 7. Verification

### 7.1 Test environment

- Postgres on `localhost:5432`, database `tpch`, full TPC-H scale-1 data.
- Python venv at `mysite/.venv` (Python 3.12).
- `pkfkrelations.csv` adjusted to reference the local `lineitem` table
  (the canonical file referenced `web_lineitem`, which does not exist in
  the local install).

### 7.2 Regression suite (end-to-end)

All four cases below were run on the v2 implementation:

| # | Hidden query | `gap_aware` | Extracted query | Expected | Status |
|---|---|---|---|---|---|
| 1 | `SELECT n_name FROM nation WHERE n_nationkey < 5 OR n_nationkey > 20;` | `yes` | `Select n_name From nation Where (nation.n_nationkey ≤ 4 OR nation.n_nationkey ≥ 21);` | recover the disjunction | ✓ |
| 2 | same as 1 | `no` | `Select n_name From nation;` (no WHERE) | preserve v1's silent-drop behavior when gap-aware is off | ✓ |
| 3 | `SELECT n_name FROM nation WHERE n_nationkey BETWEEN 5 AND 15;` | `yes` | `Select n_name From nation Where nation.n_nationkey between 5 and 15;` | tight range, no spurious disjunction | ✓ |
| 4 | `SELECT n_name FROM nation WHERE n_nationkey > 10;` | `yes` | `Select n_name From nation Where nation.n_nationkey ≥ 11;` | one-sided predicate unchanged | ✓ |

Case 1 is the canonical "Failure mode 2" (silent drop) recovery. The
output `n_nationkey ≤ 4 OR n_nationkey ≥ 21` is semantically equivalent
to the user's original `n_nationkey < 5 OR n_nationkey > 20`.

The v1 sampling path was independently exercised via the offline
algorithmic stub harness (no live DB) using four predicate shapes —
DQ3-shape, two-disjoint, no-gap, near-edges — all of which continue to
produce the expected intervals (v2 bails on the stub, v1 fallbacks
produce the result). This confirms that v2 fails gracefully when its
preconditions aren't met.

### 7.3 Trace evidence

The end-to-end trace for case 1 shows v2 being invoked twice — once per
column candidate — with the following outcomes:

```
[v2] tab=nation attr=n_nationkey  envelope=[-2^31, 2^31-1]  →  [(-2^31, 4), (21, 2^31-1)]
[v2] tab=nation attr=n_regionkey  envelope=[-2^31, 2^31-1]  →  [(-2^31, 2^31-1)]
```

The Pop pre-check (Alg 5 line 4) is the load-bearing safety on the
second line: ctid bisection on `nation` for `n_regionkey`'s envelope
*does* return a witness row, but `Pop(D¹.n_regionkey := witness_val)` is
true (because `Q_h` does not constrain `n_regionkey`), so the split is
correctly aborted and no spurious disjunction is emitted on
`n_regionkey`.

The downstream AOA stage compresses the trivial outer bounds, yielding
the final `(n_nationkey ≤ 4 OR n_nationkey ≥ 21)` rendered WHERE clause.

---

## 8. Known Limitations

### 8.1 Q_h projection must be bare columns

v2 captures `Q_h`'s projection by running `Q_h` once and reading the
result-header columns, then splicing those identifiers into `Q_E`'s
SELECT. This works for SPJ queries, but fails for `Q_h` queries that
project aggregates, expressions, or `*` — the splice would not type-check
or wouldn't carry the same semantics. v2 bails out in such cases and v1
takes over. A v3 could address this by either:

(a) Parsing `Q_h` to extract the FROM-clause and rewriting it to project
    base columns instead (was rejected during design — `Q_h` is treated
    as opaque).
(b) Materializing `Q_h`'s result and joining back to base rows via a key
    if `Q_h` happens to project a primary key.

### 8.2 Witness-in-D assumption (A5-W)

v2 requires a *real* row in the database whose attribute value lies in
the gap. If `D` happens to lack such a row, the `Re − Rh` diff is empty
and v2 reports "no gap" even when one exists. This is the same
assumption v1's sampling relied on; v1's midpoint-bisection fallback is
the only mechanism in the chain that does not require a witness in `D`.
For TPC-H workloads with full-domain data this is rarely an issue.

### 8.3 Pop pre-check destructiveness

During the outward bisection step (Alg 5 lines 6–7), each Pop probe
UPDATEs all rows of working_schema.t to set the attribute uniformly. The
column ends up uniform after the probes complete. Subsequent v2
iterations on the *same* attribute are unaffected (`Q_h`'s WHERE
references *this* attribute, which is exactly what's being mutated). But
if a future `Q_h` query had cross-attribute correlation — e.g. `WHERE
n_regionkey ≥ 2 AND n_nationkey < 5` — running v2 on `n_nationkey` first
would leave `n_regionkey` partially mutated for the subsequent
`n_regionkey` pass. v2 currently restores `D¹` at attribute-iteration
boundaries (via `teardown` in `finally`), so the working schema is
always re-cloned from `user_schema` between attributes. The destructive
mutations are confined to within one attribute's processing.

### 8.4 String IN-list disjunctions are out of scope

v2 only handles numeric/date attributes (it follows the binary-search
machinery, which doesn't apply to strings). String IN-list disjunctions
(e.g. `p_brand IN ('Brand#52', 'Brand#12')`) continue to flow through
`handle_string_filter` and DisjunctionPipeLine.

### 8.5 Trajectory-converges-to-single-disjunct case

The original handoff filed this as a known v1 limitation: if Filter's
binary search converges entirely inside one disjunct (skipping the gap
in its mid-trajectory), the envelope captures only that disjunct, and
v2's diff inside that envelope finds no further gap. The user's analysis
during v2 design (2026-05-25) confirmed this is handled by Sumang's
negation in DisjunctionPipeLine (turn `or = yes` on alongside
`gap_aware = yes`); v2 was scoped not to duplicate that mechanism.

---

## 9. Future Work

The following items remain on the v3 backlog, in rough priority order:

1. **Handle string IN-list disjunctions** by directly extending Sumang's
   negation (per user direction): after Filter finds an `equal`/`LIKE`
   predicate on a string attribute, negate it and re-minimize. Successive
   discoveries become the IN-list.

2. **Multi-table v2 hardening for aggregate Q_h.** Today the projection
   capture step aborts on aggregates. A solution that does not require
   parsing `Q_h` is the open challenge — possibly by wrapping `Q_h` as a
   subquery and joining back to base rows when a primary key is in the
   projection.

3. **Remove `handle_filter_for_subrange` dead code** (filter.py:110-143)
   per the CLAUDE.md cleanup note.

4. **Tighten the from-clause table list passed to GapWitnessFinder.**
   Today v2 swaps *every* table in `core_relations`, which is sound but
   may be wasteful when the hidden query touches only a subset. An
   analysis of the discovered join graph could narrow the swap set.

5. **Expand verification.** Beyond the four-case regression suite, run
   v2 against DQ1–DQ9 in `test/disjunction/test_queries.sql` (some of
   which exercise within-attribute disjunction with multiple disjuncts)
   and document any new behaviors.

---

## 10. Conclusion

v2 implements the `Re − Rh` witness oracle the original gap-aware
extraction handoff identified as the principled solution to within-
attribute disjunction discovery. By reusing the NEP feature's
comparator-diff and ctid-bisection machinery, it operates at the
base-row level rather than at `Q_h`'s result-set level, sidestepping the
limitation that the filter attribute may not appear in `Q_h`'s
projection. A Pop pre-check guards against false witnesses whose actual
gap belongs to a different attribute. v1's data-witness sampling and
speculative midpoint bisection are retained as graceful fallbacks for
the cases v2 does not cover (aggregate `Q_h`, missing harness, witness
not in D).

The implementation is integrated as the primary path inside
`_refine_with_gap_search`, with no changes to Filter's external contract
or to downstream pipeline stages. All four pre-existing TPC-H regression
cases continue to pass, including silent-drop preservation when
`gap_aware = no` and tight-range preservation when there is no actual
disjunction to recover.

---

## Appendix A — Reproducible verification

From `mysite/`, with the venv set up and Postgres reachable:

```bash
# End-to-end regression on case 1 (n_nationkey disjunction with gap_aware=yes)
../.venv/bin/python <<'EOF'
import sys; sys.path.insert(0, '.')
from unmasque.src.util.ConnectionFactory import ConnectionHelperFactory
from unmasque.src.core.factory.PipeLineFactory import PipeLineFactory
from unmasque.src.pipeline.abstract.TpchSanitizer import TpchSanitizer

san = ConnectionHelperFactory().createConnectionHelper(); san.connectUsingParams()
TpchSanitizer(san).sanitize(); san.closeConnection()

conn = ConnectionHelperFactory().createConnectionHelper()
conn.config.detect_gap_aware = True
conn.connectUsingParams()
Q = "SELECT n_name FROM nation WHERE n_nationkey < 5 OR n_nationkey > 20;"
f = PipeLineFactory(); t = f.init_job(conn, Q); f.doJob(Q, t)
print(f.result)
conn.closeConnection()
EOF
```

Expected output:

```
 Select n_name
 From nation
 Where (nation.n_nationkey <= 4 OR nation.n_nationkey >= 21);
```

For the offline (no-DB) algorithmic regression on v1 fallbacks, see the
`Fake`-class harness in the original `GAP_AWARE_HANDOFF.md`.

---

## Appendix B — Glossary

| Term | Meaning |
|---|---|
| `Q_h` | the hidden query Xpose is trying to extract — text known to the framework but treated as opaque for extraction purposes |
| `Q_E` | the currently extracted query — what Xpose has reconstructed so far |
| `Re`, `Rh` | result rows of `Q_E` and `Q_h` respectively |
| `D` | the original database; multiple tables, full row populations |
| `D¹` | the minimized one-row-per-table instance produced by `view_minimizer.py` |
| Pop oracle | a Boolean predicate that returns `True` iff `Q_h` returns a non-empty (and not all-NULL) result on the current database state |
| ctid | Postgres physical row identifier `(page, row)`, used by the Minimizer for bisection |
| NEP | Not-Equal Predicate — Xpose's existing feature for extracting `A <> v` constants |
| Sumang's negation | falsify-and-rerun loop in `DisjunctionPipeLine` for cross-attribute OR |
| envelope | the single contiguous range `[lo, hi]` Filter's binary search converges on for an attribute |
| disjunctive_ranges | side-channel dict in Filter that records `(tab, attr) → list of sub-intervals` for OR-of-BETWEEN render |
| witness | a base-table row whose existence in `Re − Rh` reveals a gap |
| false witness | a witness row whose actual gap is in some attribute *other than* the one currently being refined; rejected by the Pop pre-check |

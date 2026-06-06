# XPOSE_NEW — Coverage Expansion: Working Report (thesis source)

> **What this file is.** A running, append-only accumulator. Every work-item we implement (or
> seriously attempt) gets written up here in *thesis-ready* form: the construct, a concrete
> before/after example, the black-box approach, the implementation, the proof/verification we ran,
> and the honest limits/findings. When all feasible items are done, the final thesis is assembled
> from these sections — so write each entry as if it were a thesis subsection.
>
> **Companion docs:** the task tracker is [checklist.md](checklist.md) (status, theory, file
> anchors for every item). This report is the *evidence + narrative* layer.
>
> **Per-item template** (copy for each new item):
> ```
> ## WI-NN — <construct>            [status]
> ### Problem (the "before")
> ### Approach (black-box theory)
> ### Implementation
> ### Proof / verification
> ### Findings & limits
> ### Thesis takeaway
> ```

---

## §0. Framework model (shared thesis preamble)

XPOSE reconstructs a hidden SQL query `Qh` that it can only **run** against a database — it never
reads the query text. Everything is recovered by **mutating the database, executing `Qh`, and
observing the change**. Two observation channels:

- **`Pop` oracle** — "is the result non-empty *and* null-free?" (one bit), defined in
  [nullfree_executable.py](mysite/unmasque/src/core/abstract/nullfree_executable.py).
- **Result-shape** — row counts and the read-back values of surviving rows.

Extraction is a staged pipeline (FROM → mutation/filter/AOA/equi-join → disjunction → projection →
group-by → aggregate → order-by → limit → NEP), orchestrated in
[ExtractionPipeLine.py](mysite/unmasque/src/pipeline/ExtractionPipeLine.py). The final SQL is
assembled by [`QueryStringGenerator`](mysite/unmasque/src/util/QueryStringGenerator.py) (QSG),
whose [`assembleQuery`](mysite/unmasque/src/util/QueryStringGenerator.py#L79-L88) emits exactly six
clauses: `Select / From / Where / Group By / Order By / Limit`.

Two structural tensions recur throughout this report and bound what is observable:

1. **Single-witness minimization.** The View Minimizer collapses the database to one surviving row
   `D¹`. Multiplicity phenomena (DISTINCT, UNION-dedup, COUNT, cross-product scaling, self-join
   degree) are invisible on one row and must be probed on a controlled multi-row instance.
2. **Null-free oracle.** `Pop` rejects any row with a null in a projected column, which actively
   fights constructs whose semantics depend on nulls (`IS NULL`, `COALESCE`, `NULLS FIRST/LAST`,
   and — as it turns out below — `COUNT(col)` vs `COUNT(*)`).

Test environment for all proofs below: local PostgreSQL, full TPC-H (`public` schema: 1.5M orders,
6M lineitem); the framework mutates working copies in the `unmasque` schema, leaving `public`
intact.

---

## WI-01 — `COUNT(col)` rendering            ✅ DONE (2026-06-01)

### Problem (the "before")

The select-clause renderer collapsed a **column** `COUNT` to a bare, invalid token. The relevant
constants are `COUNT = 'Count'` and `COUNT_STAR = 'Count(*)'`
([constants.py:29-30](mysite/unmasque/src/util/constants.py#L29-L30)). The render gate was a
*substring* test:

```python
# QueryStringGenerator.__generate_select_clause (before)
if COUNT in self._workingCopy.global_aggregated_attributes[i][1]:   # 'Count' in label
    elt = self._workingCopy.global_aggregated_attributes[i][1]        # -> bare label
else:
    elt = label + '(' + elt + ')'                                     # wrap column
```

Because `'Count'` is a substring of `'Count(*)'`, **both** the star and column cases took the
short-circuit. For a column-COUNT whose stored label is the bare `'Count'`, the column argument was
dropped, emitting e.g. `Count as supplier_cnt` — not valid SQL.

*Concrete before:* a stored aggregate `('o_orderkey', 'Count')` with projection name `order_count`
rendered as the bare `Count as order_count` instead of `Count(o_orderkey) as order_count`.

### Approach (black-box theory)

This is a pure **emission** defect — detection is untouched, so no new mutation experiment is
needed. The correct discriminator is identity, not substring: only the literal `COUNT_STAR`
(`'Count(*)'`, which aggregation stores as `('', COUNT_STAR)` for an empty projected attribute,
[aggregation.py:243](mysite/unmasque/src/core/aggregation.py#L243)) is already valid SQL and should
be emitted verbatim; every other aggregate label — including the column-COUNT label `'Count'` —
must wrap its column.

### Implementation

Import `COUNT_STAR` and gate the short-circuit on equality
([QueryStringGenerator.py:611](mysite/unmasque/src/util/QueryStringGenerator.py#L611)):

```diff
-from ..util.constants import COUNT, SUM, max_str_len, AVG, MIN, MAX, ORPHAN_COLUMN
+from ..util.constants import COUNT, COUNT_STAR, SUM, max_str_len, AVG, MIN, MAX, ORPHAN_COLUMN
@@
-                if COUNT in self._workingCopy.global_aggregated_attributes[i][1]:
+                if self._workingCopy.global_aggregated_attributes[i][1] == COUNT_STAR:
                     elt = self._workingCopy.global_aggregated_attributes[i][1]
                 else:
                     elt = self._workingCopy.global_aggregated_attributes[i][1] + '(' + elt + ')'
```

Added regression test [CountRenderTest.py](mysite/unmasque/test/CountRenderTest.py) (4 cases).

### Proof / verification

**(a) Unit test on the real `__generate_select_clause` (no DB mutation).** A populated
`QueryDetails` modelling `SELECT o_orderpriority, count(o_orderkey) AS order_count, COUNT(*) AS
total, sum(o_totalprice) AS rev`:

```
SELECT o_orderpriority, Count(o_orderkey) as order_count, Count(*) as total, Sum(o_totalprice) as rev
PASS column-COUNT wraps its column + keeps alias
PASS COUNT(*) preserved
PASS non-COUNT aggregate unchanged
PASS no bare invalid 'Count' token anywhere
```
`python -m unittest mysite.unmasque.test.CountRenderTest` → **Ran 4 tests … OK**.

**(b) End-to-end on live TPC-H** (`select o_orderpriority, count(o_orderkey) as order_count from
orders group by o_orderpriority`, optional detectors off):

```
Q_E:  Select o_orderpriority, Count(*) as order_count
      From orders Group By o_orderpriority Order By o_orderpriority desc;
pipeline.correct (Qh == Q_E): True
```
`public.orders` row count after the run: **1,500,000** (unchanged — originals safe).

### Findings & limits

**The fix is currently *latent* — and this is the most important result of WI-01.** The end-to-end
run reconstructed `count(o_orderkey)` as `Count(*)`, *not* `Count(o_orderkey)`. The pipeline
collapses column-COUNT to `COUNT(*)` before rendering, so the `(col,'Count')` label this fix
targets is never produced today. Mechanism:

1. The **projection** stage discovers each output column's source by mutating base-column *values*
   and seeing which output values change. A COUNT depends on row *presence*, not value — mutating a
   value never moves a COUNT — so projection finds **no dependency** and leaves the count column's
   projected attribute empty (`''`).
2. [aggregation.py:241-243](mysite/unmasque/src/core/aggregation.py#L241-L243) then overrides any
   empty-projected slot to `COUNT_STAR`.

Hence `count(col) → count(*)`. Two consequences worth stating precisely:

- **For a NOT-NULL column this is correct**, because `count(col) ≡ count(*)` — the framework's
  answer is result-equivalent (`pipeline.correct = True` confirms `Qh ≡ Q_E`).
- The distinction is **only observable, and only meaningful, on a nullable column**, where
  `count(col)` skips nulls. Detecting it requires a *null-injection* probe (insert a NULL into the
  counted column on a multi-row instance; `count(col)` drops while `count(*)` does not) — this is
  the null-free-oracle tension from §0, and it is the detection-side work tracked under WI-06.

So WI-01 ships as a **correct, no-regression prerequisite**: it makes the renderer ready, so that
once the nullable-column detection produces `(col,'Count')`, the output is right. (The original
audit framed WI-01 as a standalone live-output fix; it had missed the line-243 override. Corrected.)

### Thesis takeaway

A render bug can be real yet *masked* by an upstream stage that, for sound semantic reasons,
collapses the construct first. Distinguishing "the renderer is wrong" from "the construct is
genuinely unobservable here" required an end-to-end probe, not just static inspection — and the
answer (`count(col) ≡ count(*)` unless the column is nullable) is itself a clean illustration of how
the null-free `Pop` oracle defines the boundary of what is observable.

---

## WI-02 — LIMIT detection via exponential + binary search            ✅ DONE (2026-06-01)

### Problem (the "before")

LIMIT is recovered by *adding rows* to the database and watching where the result count stops
growing — a LIMIT L clips the answer at L rows no matter how many more match. The original stage
([limit.py](mysite/unmasque/src/core/limit.py)) did this with a **single fixed-size insert**: it
seeded `no_rows = config.limit_limit` (default **1000**,
[configParser.py:25](mysite/unmasque/src/util/configParser.py#L25)), built `no_rows` matching rows
per core relation, inserted them all, ran `Qh` once, and read the clipped cardinality through

```python
fresh = len(result) - rmin_card + 2          # rmin_card = D¹ projection-start result length
limit = fresh - 1                            # excludes the header row
```

Two costs followed from the fixed batch:

1. **A hard ceiling.** A LIMIT at or above `no_rows` made `fresh` exceed the budget, hit the
   `else` branch, and was **silently dropped to `None`** — `LIMIT ≥ 1000` was simply lost.
2. **Constant O(no_rows) work, irrespective of L.** Even a `LIMIT 10` query inserted the full
   1000 rows into every relation. Raising the ceiling to catch larger limits therefore taxed
   *every* extraction equally, so the ceiling could not be raised cheaply.

*Concrete before:* `… Order By o_orderkey Limit 1500` under the default config → the stage inserts
1000 rows, sees the result still growing at the budget edge, and emits **no LIMIT clause** (`Q_E`
is `Qh` minus its LIMIT — a strict under-approximation).

### Approach (black-box theory)

Same observable, smarter search. Define a black-box probe `card(m)` = "reset every relation to D¹,
insert exactly `m` matching rows into each, run `Qh`, return the normalized cardinality
`len(result) − rmin_card + 2`." Because each inserted row contributes exactly one more candidate
output row,

```
card(m) = min(m, L) + c          (c = a fixed baseline: header + D¹ + any union/OJ offset)
```

— strictly increasing while `m < L`, then **flat at `L + c`** once the LIMIT clips. So:

- **Exponential phase.** Probe `m = 1, 2, 4, 8, …`. The first time doubling the rows does *not*
  raise `card`, the result has plateaued: the LIMIT is now clipping. This brackets `L` between the
  last still-growing batch and the first plateaued batch in `O(log L)` probes.
- **Binary phase.** Binary-search that bracket for the smallest insert count that already reaches
  the plateau value — another `O(log L)` probes — confirming the plateau is stable (not data
  exhaustion).

The reported limit is **`plateau − 1`**, i.e. the existing, validated `fresh − 1` formula applied
at saturation. This is deliberate and important: the plateau *value* equals `L + 1` and is
independent of the baseline constant `c`, whereas the *boundary insert count* is `L − 1` (the
inserted row that duplicates the D¹ witness shifts it by one — see the proof below). Reading the
limit off the boundary would be off-by-one; reading it off the plateau value is exact.

Because the probe inserts only ~`2L` rows before terminating (not the whole budget), the detection
ceiling can now be raised arbitrarily without penalising small-LIMIT queries — a query with
`LIMIT 10` under a 100 000 ceiling still finishes in ~9 probes inserting ≤16 rows. That is the
substantive lift: not a higher hard-coded constant, but a search whose cost scales with the *answer*
rather than with the *budget*.

### Implementation

[limit.py](mysite/unmasque/src/core/limit.py): `doLimitExtractJob` now delegates to two new helpers
and the old per-relation linear loop is gone.

- `__probe_limit_card(query, m, …)` — one probe. Calls `do_init()` first (the only reset that
  actually *removes* the previous probe's inserts: it recreates each relation from the pristine
  original and re-lays D¹; `restore_d_min_from_dict` alone would leave the inserted rows), clears
  `joined_attrib_valDict` so join keys realign for the new `m`, then reuses the **existing**
  `__determine_k_insert_rows` + `insert_attrib_vals_into_table` + `app.doJob` primitives, and
  returns `len(result) − rmin_card + 2` (the unchanged normalization).
- `__search_limit(probe, bounded)` — the exponential + binary search; returns `plateau − 1`, or
  `None` when no plateau is observable within budget. Factored to take the probe as a callable so
  the search logic is unit-testable against a synthetic oracle.

Two regimes share the code via the `bounded` flag:

- **No group-by (common):** each inserted row is a fresh output row, so the insert budget is
  `2 × no_rows` (one doubling past the ceiling lets a LIMIT as large as `no_rows` be confirmed by a
  second equal probe).
- **Group-bounded:** the number of distinct output rows is capped by the number of group-key
  combinations (`len(grouping_attribute_values) == total_combinations`), so the budget equals
  `no_rows`; if the search reaches that edge still clipped (fewer rows out than groups in), the
  limit is read single-shot — preserving the old grouped behaviour.

Emission is **unchanged**: `QueryDetails.limit_op = str(limit) if limit is not None else ''`
([QSG:254-255](mysite/unmasque/src/util/QueryStringGenerator.py#L254-L255)).

### Proof / verification

**(a) Algorithm — `LimitSearchTest` (9 cases) on the real `__search_limit`, synthetic oracle.**
A faithful oracle `card(m) = min(m, L) + baseline` injected as the probe callable:

```
test_small_limit                       L=10,   no_rows=1000      -> 10
test_limit_at_legacy_cap               L=1000, no_rows=1000      -> 1000   (old code dropped this)
test_limit_well_past_legacy_cap_5000   L=5000, no_rows=5000      -> 5000   (< 40 probes, ≤ 2·no_rows rows)
test_no_limit_returns_none             no LIMIT                   -> None
test_limit_beyond_budget_returns_none  L=9000, no_rows=1000      -> None   (beyond budget, honest)
test_group_bounded_edge_single_shot    L=950,  no_rows=1000, B    -> 950
test_group_bounded_no_limit_is_none    L≥#groups, B               -> None
test_tiny_limit_below_floor_is_none    L=2                        -> None   (floor)
test_probe_count_is_logarithmic        L∈{16,64,500,3000,7000}    -> exact, probe count = O(log L)
```
`python -m unittest mysite.unmasque.test.LimitSearchTest` → **Ran 9 tests … OK**.

**(b) Integration — full pipeline on live TPC-H** (`Select o_orderkey, o_totalprice From orders
Where o_totalprice <= 60000 Order By o_orderkey Limit <L>`, optional detectors off, each run in its
own process). The stage's own DEBUG trace shows the search exactly as theorised:

```
LIMIT 1500  (budget 2·2000):
  probe    1 -> card 3      probe    2 -> 4      probe   4 -> 6     ...   probe 1024 -> 1026
  probe 2048 -> 1501  (clipped)        probe 4000 -> 1501  (plateau confirmed)
  binary search -> clipping boundary at 1499 inserted rows ->  Finalized Limit 1500
  Q_E: … Order By o_orderkey asc Limit 1500;        pipeline.correct (Qh == Q_E): True

LIMIT 10:
  probe 1 -> 3   2 -> 4   4 -> 6   8 -> 10   16 -> 11 (clipped)   32 -> 11 (plateau)
  binary search -> clipping boundary at 9 inserted rows  ->  Finalized Limit 10
  Q_E: … Order By o_orderkey asc Limit 10;          pipeline.correct (Qh == Q_E): True
```

Note the data confirming the off-by-one analysis: `card(m) = m + 2`, the plateau sits at `L + 1`
(`1501`, `11`), and the boundary insert count is `L − 1` (`1499`, `9`) — so the limit is correctly
taken from `plateau − 1`, **not** from the boundary. `public.orders` was **1,500,000 rows before
and after** both runs (working copies live in the `unmasque` schema; originals untouched).

### Findings & limits

- **The ceiling is now cheap, not gone.** At a given `no_rows`, the largest detectable LIMIT is
  still ~`no_rows` (the no-group case needs budget `2·no_rows ≥ 2L`). The win is twofold: (i) at the
  old default the boundary case `L = no_rows` is now *recovered* rather than dropped, and (ii)
  inserts scale with `~2L`, not with the budget, so `[options] limit` can be set as high as needed
  (e.g. ≥ 5000 for the headline case) without slowing down the overwhelming majority of small-LIMIT
  queries. This is the honest statement of "lifting the cap."
- **`L ≤ 2` is unobservable** — the clipped result is indistinguishable from the degenerate D¹
  baseline (`fresh < 4`). Preserved as the stage's long-standing floor.
- **Grouped `L ≥ #group-combinations` is unobservable** — we cannot synthesize more distinct group
  rows than combinations exist in the controlled instance, so the plateau and "no limit" coincide.
  Defaults to `None`, as before.
- **Plateau soundness depends on fresh distinct inserts.** Because the row generator supplies
  unlimited distinct matching values, a plateau can only be caused by a LIMIT, not by running out of
  rows — except in the grouped case above, where the budget is deliberately bounded and the
  single-shot edge read handles it.

### Where it lives

- Detection: [limit.py](mysite/unmasque/src/core/limit.py) — `__probe_limit_card`,
  `__search_limit`; `doLimitExtractJob` rewritten. Reuses `do_init`,
  `__determine_k_insert_rows`, `insert_attrib_vals_into_table`, `app.doJob`.
- Emission: unchanged ([QSG:254-255](mysite/unmasque/src/util/QueryStringGenerator.py#L254-L255)).
- Budget knob: `config.limit_limit` (`[options] limit`,
  [configParser.py:25](mysite/unmasque/src/util/configParser.py#L25)) — now safe to raise.
- Test: [LimitSearchTest.py](mysite/unmasque/test/LimitSearchTest.py) (permanent, 9 cases).

### Thesis takeaway

The same one-bit-richer observable (result *count*, not just non-emptiness) that the stage always
used becomes far more powerful under the right search. Replacing a fixed linear probe with an
exponential-then-binary search turns an O(N) scan with a hard wall into an `O(log L)` search whose
cost tracks the *answer*, letting the detectable range be widened essentially for free. The subtle
correctness point — reading the limit from the *plateau value* (`L + 1`, constant-independent)
rather than the *boundary count* (`L − 1`, shifted by the witness-duplicate baseline) — is a clean
example of preferring the observable that is invariant to the framework's own normalization.

---

## S2 — Controlled duplicate-row probe primitive (shared enabler)            ✅ DONE (2026-06-02)

### Problem (the "before")

A whole class of SQL constructs are *multiplicity* phenomena: the literal constant `1` vs
`COUNT()==1`, `COUNT(DISTINCT col)`, `SELECT DISTINCT`, `UNION` vs `UNION ALL`, `HAVING`. None of them
is observable on the single witness row `D¹` that the View Minimizer leaves behind (§0, tension 1) — on
one row a count is 1, a duplicate is collapsed, a dedup is invisible. To see them a detector must build
a *controlled* multi-row instance: add a known number of duplicate tuples, run `Qh`, read the
result-shape change, then undo the duplication so `D` is left exactly as it was.

The three operations that experiment needs — duplicate a row by ctid, delete a row by ctid, count a
query's rows — already existed in the codebase, but **copy-pasted and privately owned** by the first two
stages that happened to need them:

- `CardinalityProbe._insert_duplicate` (`INSERT … SELECT * … RETURNING ctid::text`) and
  `CardinalityProbe._delete_rows_at_ctids` (per-ctid `DELETE`), in
  [cardinality_probe.py](mysite/unmasque/src/core/cardinality_probe.py);
- `MultiplicityProbe._count_rows` (run `Qh`, strip the header row), in
  [multiplicity_probe.py](mysite/unmasque/src/core/multiplicity_probe.py).

Any new multiplicity detector (WI-05/06/14/22/23) would either re-implement these — and risk diverging
on the SQL or the header/None handling — or reach across into a sibling stage's private methods.

### Approach (black-box theory)

Factor the three operations into one reusable helper and add nothing new to the *oracle*. The key
observation that makes this safe and commit-free: the stage's `app` (the executable that runs `Qh`)
shares the stage's `connectionHelper` — one psycopg2 connection, therefore one transaction
([AppExtractorBase.py](mysite/unmasque/src/core/abstract/AppExtractorBase.py):13 creates `app` from the
same `connectionHelper`). So an **uncommitted** `INSERT` issued through `connectionHelper` is already
visible to the very next `app.doJob`, and a matching `DELETE` reverts it within the same transaction.
The duplicate→count→revert round-trip therefore needs no `COMMIT`/`ROLLBACK` and leaves the table — down
to its ctid set — unchanged.

### Implementation

New class [`RowProbe`](mysite/unmasque/src/core/row_probe.py)`(connectionHelper, app, logger)`:

- `duplicate_rows(fqn, ctids=None)` → `INSERT INTO fqn SELECT * FROM fqn [WHERE ctid IN (…)] RETURNING
  ctid::text`. With `ctids=None` it duplicates every row (the CardinalityProbe self-join case); with an
  explicit list it duplicates exactly those rows (targeted multiplicity probes). The `SELECT` is
  snapshotted before the `INSERT`, so duplicating from the same table never feeds on its own output.
  Returns the new rows' ctids.
- `delete_rows(fqn, ctids)` → one `DELETE … WHERE ctid = '…'` per ctid (reverts a duplicate).
- `count_rows(query)` → run `query` via `app`, return the data-row count with the column-name header
  stripped (`max(0, n-1)`); `-1` if the query could not be executed (verbatim port of
  `MultiplicityProbe._count_rows`).
- `list_ctids(fqn)` → the current ctids (for targeting a row and for asserting set-restoration).

The two owners now delegate: `CardinalityProbe._insert_duplicate` → `duplicate_rows`,
`_delete_rows_at_ctids` → `delete_rows`; `MultiplicityProbe._count_rows` → `count_rows`. (CardinalityProbe
`_count_qh` keeps its own `app.done` guard, untouched, to preserve the verified self-join detection
exactly.)

### Proof / verification

[RowProbeTest.py](mysite/unmasque/test/RowProbeTest.py):

- **10 unit cases** (fake connectionHelper/app, deterministic) lock the SQL the primitive builds —
  duplicate-all (`INSERT … SELECT * FROM t RETURNING ctid::text`), duplicate-by-ctid (`… WHERE ctid IN
  ('(0,1)', '(0,2)') …`), one `DELETE` per ctid, ctid read-back — and the header-stripping count
  (list→`n-1`, header-only→0, `None`→0, exception→`-1`).
- **1 live-DB integration case** — the S2 acceptance criterion. On a throwaway `public` table seeded with
  3 rows: `before = {3 ctids}`; `count = 3`; duplicate one targeted ctid → `count = 4`, ctid set grows by
  one; delete the new ctid → **`after == before`** and `count = 3`. The ctid set is restored, i.e. `D` is
  byte-for-byte unchanged.

`python -m unittest mysite.unmasque.test.RowProbeTest` → **OK** (11 tests).

### Findings & limits

- The commit-free round-trip is sound **only because `app` and the mutating stage share one connection**.
  A caller that ran `Qh` on a *separate* connection would not see the uncommitted dup; `RowProbe` is
  written for the in-stage case, which is every current consumer.
- `count_rows` faithfully reproduces the original quirk that an `app.doJob` *error string* (returned, not
  raised, on failure) is measured by length rather than recognised as an error; a consumer that must tell
  an error apart from a degenerate result should additionally check `app.done` (WI-05's probe does).

### Thesis takeaway

The minimization that makes most of the pipeline tractable (collapse `D` to one witness row) is exactly
what blinds it to multiplicity. S2 is the small, sound primitive that buys the multiplicity channel back:
add a controlled, reversible perturbation to the row *count* and read the shape change. Consolidating it
once — rather than per-stage — is what lets the multiplicity items (WI-05 here, then COUNT DISTINCT,
UNION-dedup, DISTINCT, HAVING) share a single audited experiment instead of four subtly different copies.

---

## WI-05 — `const-1` vs `COUNT()==1` disambiguation            ✅ DONE (2026-06-02)

### Problem (the "before")

`SELECT 1, …` and `SELECT COUNT(*), …` both contribute a projected column the projection stage cannot
explain by value: a literal `1` has no source column, and a COUNT depends on row *presence*, not value,
so mutating any base value never moves it. Both therefore arrive at the GroupBy stage with an empty
projected attribute (`''`). The stage splits them with a **value heuristic**
([groupby_clause.py:74-80](mysite/unmasque/src/core/groupby_clause.py#L74)): across the probe instances
it synthesises, if every group it observed showed the string `'1'`, the column is recorded as the literal
constant 1 (`CONST_1_THERE`); if any group ever showed a value `≠ '1'`, it is a COUNT (`COUNT_THERE`,
left empty so [aggregation.py:241-243](mysite/unmasque/src/core/aggregation.py#L241) renders `COUNT(*)`).

This is **unsound**: a genuine `COUNT(*)`/`COUNT(col)` whose value is 1 in every probed group is, by
value alone, indistinguishable from a literal 1 — and would be mis-emitted as the constant `1`. It
happens to be *sound in practice today* only because of an unrelated implementation detail: the probe
fills the grouping column with the repeated delta `[0, 1, 1]`
([groupby_clause.py:39](mysite/unmasque/src/core/groupby_clause.py#L39)), so two synthesised rows always
collide into one ≥2-row group, which forces a real COUNT to reveal a `2` in at least one probe. The
classifier should not depend on a witness-synthesis side effect it has no contract with. (This mirrors
WI-01's "latent" finding: the construct is currently masked, and the fix removes the fragility rather
than a live mis-extraction.)

### Approach (black-box theory)

Decide the column by *directly probing multiplicity* instead of inferring it from whatever the grouping
probes happened to produce. The discriminator is exact:

> A literal `1` is invariant to row multiplicity. `COUNT(*)` (and `COUNT(col)` on a non-null column)
> increases by at least one when a contributing tuple is added to a group.

So, using S2: reset to the single-witness instance `D¹` (which yields exactly **one group**, hence one
result row), read the candidate column's value, **duplicate one contributing witness row by ctid**, re-run
`Qh`, and read it again. Value grew → genuine COUNT; value unchanged → real literal 1; then revert the
duplicate. Probing on `D¹` is what makes "target the duplicate at exactly the measured group" automatic:
with a single group the duplicated tuple can only land in it, so the signal — read as the `max` over the
lone result row — is undiluted (no risk of a multi-group `max` hiding the increment in a non-maximal
group).

### Implementation

[groupby_clause.py](mysite/unmasque/src/core/groupby_clause.py): the final const-1 application loop now
routes every `CONST_1_THERE` candidate through `_confirm_const1_columns(query, const1_cols)`:

```python
const1_cols = [i for i in range(len(check_array)) if check_array[i] == CONST_1_THERE]
for i in self._confirm_const1_columns(query, const1_cols):
    self.projected_attribs[i] = CONST_1_VALUE     # confirmed literal 1
# columns dropped by the probe stay '' -> Aggregation emits COUNT(*)
```

`_confirm_const1_columns` `do_init()`s to `D¹`, reads `base_val` per candidate via the static helper
`_max_int_in_col` (largest integer in that result column over the rows), duplicates one ctid of
`core_relations[0]` through [`RowProbe`](mysite/unmasque/src/core/row_probe.py) (S2), re-runs `Qh`, and
reverts in a `finally`. A candidate whose value rose is logged and dropped (→ COUNT); the rest are
returned as genuine literal-1 columns. It runs **only** when a candidate exists, and degrades to the old
verdict (treat as const-1) on any unreadable reading — so it can never turn a true literal 1 into a COUNT
(its value cannot grow). No QSG change; no feature flag (the change has no false-positive direction).

### Proof / verification

**(a) Unit — [GroupByConst1Test.py](mysite/unmasque/test/GroupByConst1Test.py), 11 cases on the real
methods**, driven by a synthetic oracle that models the exact case the old heuristic gets wrong (a COUNT
reading 1 on the witness instance, rising by one per duplicated contributing row):

```
COUNT stuck at 1            -> reclassified to COUNT (dropped); dup issued AND reverted (D unchanged)
literal 1                  -> kept as const-1; still reverted
mixed [COUNT==1, literal 1] -> only the literal kept
no candidates              -> short-circuits, no probe at all
unreadable witness result  -> defaults to const-1 (no false positive)
targeted duplicate         -> uses list_ctids()[:1] of the first relation
_max_int_in_col            -> max over groups / non-int -> None / out-of-range -> None / strips space
```
`python -m unittest mysite.unmasque.test.GroupByConst1Test` → **OK** (11 tests).

**(b) End-to-end on live TPC-H** (optional detectors off, each run in its own process):

```
Qh : select o_orderpriority, 1 from orders group by o_orderpriority
Q_E: Select o_orderpriority, 1 From orders Group By o_orderpriority Order By o_orderpriority desc;
     correct (Qh == Q_E): True

Qh : select o_orderpriority, count(*) as order_count from orders group by o_orderpriority   (control)
Q_E: Select o_orderpriority, Count(*) as order_count From orders Group By o_orderpriority
     Order By o_orderpriority desc;
     correct (Qh == Q_E): True
```

The stage's DEBUG trace shows the probe firing only for the literal-1 query, exactly as designed —
`Group_By … INSERT INTO unmasque.orders SELECT * FROM unmasque.orders WHERE ctid IN ('(0,10)') RETURNING
ctid::text;` then `DELETE FROM unmasque.orders WHERE ctid = '(0,11)';` (the S2 dup + revert), and **no**
"reclassified" line (the literal 1 was correctly confirmed). The COUNT(*) control logs **no** Group_By
duplicate at all: its count varies across groups, so the natural `COUNT_THERE` path already classifies
it and no candidate reaches the probe. `public.orders` was **1,500,000 rows before and after** both runs.

### Findings & limits

- **Latent, like WI-01.** Under today's `[0,1,1]` witness synthesis a real COUNT always reveals a `2`, so
  no live query is currently mis-extracted. WI-05's value is that it makes the decision *sound* — robust
  to the synthesis delta — and supplies the multiplicity-probe pattern that WI-06 (`COUNT(DISTINCT)`)
  builds on directly.
- **One-sided by construction.** The probe can only *demote* a const-1 candidate to COUNT; it can never
  promote a true literal 1 (whose value is multiplicity-invariant). So enabling it carries no risk of
  corrupting a genuine constant — which is why it needs no feature-flag gate.
- **`COUNT(col)` vs `COUNT(*)` is still out of scope here** — that distinction is only observable on a
  *nullable* counted column via null-injection, tracked under WI-06; on a non-null column the two are
  result-equivalent (the WI-01 finding).

### Where it lives

- Detection: [groupby_clause.py](mysite/unmasque/src/core/groupby_clause.py) —
  `_confirm_const1_columns`, `_max_int_in_col`; built on [`RowProbe`](mysite/unmasque/src/core/row_probe.py) (S2).
- Emission: unchanged — a confirmed const-1 renders as the bare `1`
  ([QSG:608-634](mysite/unmasque/src/util/QueryStringGenerator.py#L608)); a demoted candidate renders as
  `Count(*)` via [aggregation.py:241-243](mysite/unmasque/src/core/aggregation.py#L241).
- Tests: [GroupByConst1Test.py](mysite/unmasque/test/GroupByConst1Test.py) (11 cases) and the S2
  primitive's [RowProbeTest.py](mysite/unmasque/test/RowProbeTest.py).

### Thesis takeaway

Two constructs that are indistinguishable on the *value* channel (`1` and a COUNT that reads 1) separate
cleanly on the *multiplicity* channel: perturb the row count of one group and watch whether the column
moves. The earlier heuristic got the right answer for the wrong reason — it leaned on an incidental
property of the witness synthesiser rather than on the semantics of COUNT. Replacing it with a direct,
reversible duplicate-row experiment is a small but representative instance of the report's recurring
move: when the single-witness value channel is ambiguous, recover the missing bit from a controlled
change in cardinality.

---

## WI-06 — `COUNT(DISTINCT col)` + companion `COUNT(col)` vs `COUNT(*)`            ✅ DONE (2026-06-02)

### Problem (the "before")

A COUNT is the one aggregate the pipeline cannot localise by value. The projection stage discovers a
column's source by mutating base *values* and watching which output values move; a COUNT depends on row
*presence*, not value, so mutating any value never moves it. Projection therefore finds no dependency and
leaves a count column's projected attribute empty (`''`), and the aggregation stage's final pass blanket-
labels every empty-projected slot `('', COUNT_STAR)`
([aggregation.py:241-243](mysite/unmasque/src/core/aggregation.py#L241)). The stage even says so in a
comment: *"AsSUMing NO DISTINCT IN AGGREGATION"* ([aggregation.py:133](mysite/unmasque/src/core/aggregation.py#L133)).

The consequence is that `COUNT(*)`, `COUNT(col)`, and `COUNT(DISTINCT col)` were **all** reconstructed as
`Count(*)`. For `COUNT(*)` that is right; for `COUNT(col)` on a non-null column it is result-equivalent
(`count(col) ≡ count(*)`); but for `COUNT(DISTINCT col)` it is simply **wrong** — a query that asks "how
many *distinct* customers per status" was answered with "how many *rows* per status."

*Concrete before:* `SELECT o_orderstatus, COUNT(DISTINCT o_custkey) FROM orders GROUP BY o_orderstatus`
→ `Q_E = … Count(*) … GROUP BY o_orderstatus` — a different query (over-counts whenever a customer has
more than one order in a status group).

### Approach (black-box theory)

`COUNT(DISTINCT col)` is a *multiplicity* phenomenon: it is invisible on the single witness row `D¹` (where
every count is 1) and must be probed on a controlled multi-row instance (§0, tension 1). Nothing here reads
the query text — every verdict comes from inserting a crafted row, running `Qh`, and reading the count off
the result. The refinement, per result index already labelled `COUNT_STAR`, works on `D¹` (one group, count
= 1) in three steps.

**Step 1 — distinctness.** Exact-duplicate one contributing witness row (enabler S2). The behaviour splits
cleanly:

| crafted change to the witness's group | `COUNT(*)` | `COUNT(col)` | `COUNT(DISTINCT col)` |
|---|---|---|---|
| **exact duplicate** of the witness row | +1 | +1 | **+0** (value repeated) |

So the count *rises* for a non-distinct count and *stays* for a distinct count. The duplicate is a copy of a
surviving row, so it always survives `Qh` — this signal is unconditionally reliable.

**Step 2a — if distinct, identify the column.** For each candidate column, insert a witness-copy whose only
change is a **fresh distinct** value in that column (everything else, including the group key, held at the
witness value):

| crafted change | `COUNT(DISTINCT X)` when col = X | `COUNT(DISTINCT X)` when col ≠ X |
|---|---|---|
| fresh distinct value in `col` | **+1** (new distinct value of X) | +0 (X held at its witness value) |

Exactly one column — the counted one — lifts the count by one; every other column is held constant for X, so
the distinct set does not grow. There is no false positive: changing a non-counted column cannot enlarge X's
distinct set.

**Step 2b — if non-distinct, the `COUNT(col)`-vs-`COUNT(*)` companion** (this is what makes WI-01's render
fix *observable*). Inject a NULL into a candidate column on a witness-copy row:

| crafted change | `COUNT(*)` | `COUNT(col)` when col is the arg |
|---|---|---|
| NULL in `col` (other projected cols non-null) | +1 (row counted) | **+0** (NULL skipped) |

`COUNT(*)` counts the row regardless; `COUNT(col)` skips its NULL. A **survival guard** — a non-null fresh
value in the *same* column must lift the count — rules out the only false positive (the crafted row was
dropped by an unforeseen constraint, which would also leave the count flat). Filtered columns are skipped
because a filter would reject the NULL and drop the row spuriously. Because the null-free `Pop` oracle
operates on the *result* row `(group-key, count)` — which is never null — the injected base-table NULL is
consumed by the aggregate and the result row survives; this is the rare construct where the null-free
oracle does *not* fight us.

Candidate columns exclude GROUP BY keys (perturbing one moves the row to a different group) and equi-join
keys (perturbing one breaks the join and silently drops the crafted row → a false negative).

### Implementation

A new pass [`Aggregation._refine_counts`](mysite/unmasque/src/core/aggregation.py), gated by the
`count_distinct` feature flag (default OFF), runs right after the line-243 blanket `COUNT_STAR` label:

- `_count_arg_candidates` — base columns minus GROUP BY keys and equi-join keys.
- `_distinctness_probe` — S2 exact-duplicate of `core_relations[0]`'s witness via
  [`RowProbe`](mysite/unmasque/src/core/row_probe.py); reverts in a `finally`.
- `_identify_distinct_column` / `_identify_nonnull_count_column` — the per-column step-2 searches.
- `_probe_count_with_col(query, ri, tab, col, mode)` — `do_init()` to `D¹`, craft a witness-copy row
  (read via `get_dmin_val`) with `col` set to a fresh value (`get_different_s_val`, respecting filters) or
  `None` (→ SQL NULL), `insert_attrib_vals_into_table`, run `Qh`, return the count.
- module-level `_max_int_in_result_col` reads the count off the (single-group) result.

A confirmed distinct count is stored as `(col, COUNT_DISTINCT)`; a confirmed nullable column-count as
`(col, COUNT)`. The aggregate tuple **shape is unchanged** — still `(attrib, op)`, only new `op` values —
so no predicate-consumer audit is needed beyond the op itself. New sentinel `COUNT_DISTINCT = 'Count(distinct)'`
([constants.py](mysite/unmasque/src/util/constants.py)) plus the `count_distinct` flag wired through
[config.ini](mysite/config.ini) and [configParser.py](mysite/unmasque/src/util/configParser.py).

Emission ([QueryStringGenerator.py:608-640](mysite/unmasque/src/util/QueryStringGenerator.py#L608)):
`COUNT_DISTINCT` added to `AGGREGATES`; a dedicated render branch emits `Count(distinct <col>)` (pulling
the column from the aggregate tuple, since the projected attribute is empty); and the generic column-COUNT
branch now falls back to the aggregate tuple's column when the projected attribute is empty — so the
companion `(col, COUNT)` renders `Count(o_orderkey)` rather than the invalid `Count()`. The order-by stage's
`COUNT in op` substring tests treat the `'Count'`-containing sentinel as a count (correct); its only other
toucher, `check_order_by_on_count`, is dead code (zero callers).

### Proof / verification

**(a) Unit — [CountDistinctAggTest.py](mysite/unmasque/test/CountDistinctAggTest.py), 17 cases on the REAL
methods** (bound to a duck-typed `self`; a synthetic oracle computes the count from an in-memory row set, so
the classification logic is isolated from DB plumbing). The oracle models COUNT(*)/COUNT(col)/COUNT(DISTINCT
col) and a "drop column" whose mutation fails `Qh` (to exercise the survival guard):

```
COUNT(DISTINCT col)                -> (col, COUNT_DISTINCT), right column even with a decoy neighbour
COUNT(*)                           -> stays ('', COUNT_STAR)
COUNT(col) on a nullable column    -> (col, COUNT)
DISTINCT over a join key           -> unidentified -> stays COUNT(*)
DISTINCT over an '='-filtered col  -> unidentified -> stays COUNT(*)
companion survival guard           -> dropped-row column NOT mistaken for COUNT(col)
no count columns                   -> no probe, no change
candidates exclude group/join keys ; distinctness probe reverts ; QSG renders Count(distinct col)
```
`python -m unittest mysite.unmasque.test.CountDistinctAggTest` → **Ran 17 tests … OK**. Per-module
regressions (`CountRenderTest`, `RowProbeTest`, `GroupByConst1Test`) pass in isolation.

**(b) End-to-end on live TPC-H** (`count_distinct=yes`, other detectors off, each run in its own process):

```
Qh : select o_orderstatus, count(distinct o_custkey) from orders group by o_orderstatus
Q_E: Select o_orderstatus, Count(distinct o_custkey) as count From orders
     Group By o_orderstatus Order By o_orderstatus asc;            correct (Qh == Q_E): True

Qh : select o_orderstatus, count(o_orderkey) as cnt  from orders group by o_orderstatus   (companion)
Q_E: Select o_orderstatus, Count(o_orderkey) as cnt  From orders
     Group By o_orderstatus Order By o_orderstatus desc;           correct (Qh == Q_E): True

Qh : select o_orderstatus, count(*) as cnt           from orders group by o_orderstatus   (control)
Q_E: Select o_orderstatus, Count(*) as cnt           From orders
     Group By o_orderstatus Order By o_orderstatus desc;           correct (Qh == Q_E): True
```

The companion run is the WI-01 payoff: the pipeline emits `Count(o_orderkey)` instead of collapsing to
`Count(*)` — the column-COUNT render fix is finally *exercised by the live flow*. The control confirms no
false positive. The Aggregation DEBUG trace shows the mechanism exactly as theorised — the S2 distinctness
dup + revert:

```
Aggregation: INSERT INTO unmasque.orders SELECT * FROM unmasque.orders WHERE ctid IN ('(0,10)') RETURNING ctid::text;
Aggregation: DELETE FROM unmasque.orders WHERE ctid = '(0,11)';
```

then the per-column null-inject sweep (one crafted witness-copy with each candidate column = NULL in turn:
`(None, 110063, 'O', …)`, `(6000000, None, 'O', …)`, …), 8 candidates (o_orderstatus excluded as the group
key). `public.orders` was **1,500,000 rows before and after** all three runs (working copies live in the
`unmasque` schema; originals untouched).

### Findings & limits

- **The companion is *latent* on TPC-H, like WI-01/WI-05.** `COUNT(DISTINCT col)` over a unique key and
  `COUNT(col)` over a non-null column are *result-equivalent* to the simpler form, so on a schema whose
  columns are non-null/unique (most of TPC-H) the companion improves *faithfulness* (it emits the actual
  column) but not *result-correctness*. Its value is recovering the construct the user actually wrote, and
  being sound when nulls do exist. The headline `COUNT(DISTINCT col)` over a *non-unique* column (e.g.
  `o_custkey` within an order-status group) is a genuine before/after correctness fix.
- **The counted column must be perturbable.** A column that is an equi-join key, the group key, or
  single-valued under an `=` filter cannot be identified — the stage leaves `COUNT(*)` and logs a note
  rather than guess. (Distinct-over-a-join-key is the most notable gap; perturbing the key breaks the join.)
- **First cut is a single base column**, not an expression (`COUNT(DISTINCT a+b)`), and the distinctness
  probe duplicates `core_relations[0]`; multi-table counts work when the perturbed column lives in the
  table whose witness is duplicated, but are not yet exhaustively verified — single-table is the verified
  scope.
- **One-sidedness keeps it safe.** Step 1 is unconditionally reliable (the exact duplicate always survives).
  Step 2 can only *promote* `COUNT(*)` to a more specific form when a column genuinely produces the right
  signal; a wrong column produces the wrong count and is rejected, and the companion's survival guard blocks
  the dropped-row false positive. When in doubt it falls back to the pre-WI-06 `COUNT(*)`. The stage is
  nonetheless flag-gated (default OFF) per the project rule for probe-costing, false-positive-capable
  detection.

### Where it lives

- Detection: [aggregation.py](mysite/unmasque/src/core/aggregation.py) — `_refine_counts` and helpers;
  built on [`RowProbe`](mysite/unmasque/src/core/row_probe.py) (S2).
- Emission: [QueryStringGenerator.py:608-640](mysite/unmasque/src/util/QueryStringGenerator.py#L608) —
  `Count(distinct col)` branch + column-COUNT fallback to the aggregate tuple; `COUNT_DISTINCT` in `AGGREGATES`.
- Flag: `count_distinct` ([config.ini](mysite/config.ini) `[feature]`,
  [configParser.py](mysite/unmasque/src/util/configParser.py)), default OFF; read via
  `connectionHelper.config.detect_count_distinct`.
- Test: [CountDistinctAggTest.py](mysite/unmasque/test/CountDistinctAggTest.py) (17 cases).

### Thesis takeaway

Three constructs that are indistinguishable on the value channel — and which the pipeline therefore collapsed
into one — separate cleanly on the *multiplicity* channel with two orthogonal perturbations: a duplicate
(does the count track repetition? → DISTINCT or not) and a fresh-value / NULL injection (which column, and
does it skip NULLs?). It is a direct continuation of the report's recurring move (cf. WI-05): when the
single-witness value channel is ambiguous, recover the missing bit from a controlled change in cardinality.
It also closes the loop on WI-01: the column-COUNT renderer that shipped "latent" in WI-01 is finally driven
by a real detection signal, demonstrating that the render fix and the detection fix were two halves of one
construct.

---

## WI-03 — Robust outer-join candidate equivalence (EXCEPT ALL bag diff)            ✅ DONE (2026-06-02)

### Problem (the "before")

The outer-join stage reconstructs the join *type* of each edge (INNER / LEFT / RIGHT / FULL) by
generating a set of candidate queries `Q_E` and keeping only those that stay **result-equivalent to
`Qh` under a battery of mutations**. For every join edge it breaks the join key on `D¹` (sets it to a
non-matching value) and for every ON-predicate column it injects a NULL; after each mutation it asks
*"do `Qh` and the candidate still produce the same result?"* — an outer join exposes NULL-extended
rows that an inner join (or the wrong outer variant) does not, so the surviving candidate reveals the
true edge type. The equivalence test lived in
[`__are_the_results_same`](mysite/unmasque/src/core/outer_join.py#L253):

```python
# OuterJoin.__are_the_results_same (before)
res_HQ = self.app.doJob(query)          # run Qh on the mutated D1
res_poss_q = self.app.doJob(poss_q)     # run candidate Q_E on the mutated D1
if len(res_HQ) != len(res_poss_q):
    same = False
else:
    data_HQ, data_poss_q = res_HQ[1:], res_poss_q[1:]
    for var in range(len(data_HQ)):          # maybe use the available result comparator techniques
        if not (data_HQ[var] == data_poss_q[var]):
            same = False
return same
```

This is an **ordered, positional** row-by-row equality after a length check. SQL results are
*bags* (multisets), so the comparison is fragile in exactly the regime this stage operates in:

- **Row reordering.** Two queries that produce the same bag in a different order compare *unequal*
  (`data_HQ[0] != data_poss_q[0]`). The candidate carries the same `ORDER BY` as `Qh`, but ORDER BY
  with ties does not impose a total order, and the moment an outer variant adds/removes a NULL-extended
  row the tie structure shifts.
- **Duplicates.** With repeated rows, position `i` in one result need not be the "same" tuple as
  position `i` in the other even when the multisets are identical.
- **NULLs.** Outer joins are *defined* by their NULL-extended rows; the Python path stringifies them and
  compares tuples positionally, which has no principled NULL-equality semantics.

A mis-judged equivalence here is not cosmetic: a false *accept* emits the wrong join type into `Q_E`,
and a false *reject* drops the only correct candidate. The code's own comment ("maybe use the available
result comparator techniques") flagged the gap.

### Approach (black-box theory)

The pipeline already owns a proven, order- and duplicate-correct result-equivalence primitive — the
`r_e`/`r_h` diff used by [Comparator](mysite/unmasque/src/pipeline/abstract/Comparator.py): two results
are **bag-equal iff `(A EXCEPT ALL B)` and `(B EXCEPT ALL A)` are both empty**
([`run_diff_queries`](mysite/unmasque/src/pipeline/abstract/Comparator.py#L77) +
[`is_match`](mysite/unmasque/src/pipeline/abstract/Comparator.py#L71)). `EXCEPT ALL` is multiset
difference with *not-distinct* NULL semantics (two NULLs are equal), so it is exactly the oracle this
test wants. The soundness statement:

> Let `A`, `B` be the result bags of `Qh` and `Q_E` on the mutated `D¹`. `|A EXCEPT ALL B| = Σ_r max(0,
> A[r] − B[r])`. If `A = B` both diffs are 0. If `A` has a surplus of any row, the forward diff is
> > 0; if `B` has a surplus, the reverse diff is > 0. Hence **both diffs = 0 ⟺ A = B as bags** — order-
> and duplicate-insensitive, NULL-correct, and implying row-count equality without a separate check.

The one wrinkle is *where* to run it. The full
[`Comparator.match`](mysite/unmasque/src/pipeline/abstract/Comparator.py#L97) first **restores the
tables to `user_schema`** (`doActualJob` → `restore_table_and_confirm`), which would erase the very
mutation the test depends on. So we do not call `Comparator`; we reuse its *primitive* in-stage,
computing the diff inline against the current mutated working `D¹` exactly as
[`GapWitnessFinder._diff_nonempty`](mysite/unmasque/src/core/gap_witness.py#L228) already does:

```sql
select count(*) from ( (<left_query>) EXCEPT ALL (<right_query>) ) as T;
```

run through `self.app.doJob`, which prepends `set search_path='<working_schema>'`
([executable.py:49](mysite/unmasque/src/core/executables/executable.py#L49)) so both operands resolve
against the mutated copy. On `D¹` the result sets are tiny (the witness row ± a mutation), so the two
extra count queries per candidate are negligible — and the candidate set is one per BFS root.

**Failure direction.** The dangerous error is a *false accept* (emitting an outer-join variant that is
not actually equivalent). So when the diff cannot be evaluated — a malformed candidate, a column/type
mismatch between `Qh` and `Q_E`, any SQL error — the test **fails closed**: it returns "not same" and
the candidate is rejected, never silently accepted. (The old code would simply crash on such a query.)

### Implementation

[outer_join.py:253](mysite/unmasque/src/core/outer_join.py#L253) — `__are_the_results_same` rewritten,
plus one helper `__bag_diff_count`:

```python
def __are_the_results_same(self, poss_q, query, same):
    if not same:
        return False                          # caller threads `same` across edges; once False, stays False
    fwd = self.__bag_diff_count(query, poss_q) # |Qh EXCEPT ALL Q_E|
    rev = self.__bag_diff_count(poss_q, query) # |Q_E EXCEPT ALL Qh|
    if fwd is None or rev is None:
        return False                          # could not certify -> reject (sound direction)
    return fwd == 0 and rev == 0

def __bag_diff_count(self, left_q, right_q):
    left  = (left_q  or "").rstrip().rstrip(';').strip()
    right = (right_q or "").rstrip().rstrip(';').strip()
    if not left or not right:
        return None
    diff_sql = f"select count(*) from (({left}) except all ({right})) as T;"
    try:
        res = self.app.doJob(diff_sql)
    except Exception as e:
        self.logger.debug(f"EXCEPT ALL diff failed: {e}")
        return None
    if not res or len(res) < 2 or not res[1]:
        return None
    try:
        return int(str(res[1][0]).strip())
    except (ValueError, TypeError):
        return None
```

Each operand is parenthesised so it keeps its own `ORDER BY`/`LIMIT` (a bare trailing `ORDER BY` would
otherwise bind to the whole set operation); trailing semicolons are stripped so the composition is a
single legal statement. The `if not same` short-circuit preserves the caller's accumulation semantics
(`__remove_semantically_nonEq_queries` does `same = self.__are_the_results_same(poss_q, query, same)`
in a loop, and the mutation/revert around each call is unchanged) while saving two DB round-trips once a
candidate has already been separated. No predicate shape changed, so no consumer audit is needed; no QSG
change; no new flag (the whole stage is gated by the existing `outer_join` flag).

### Proof / verification

**(a) Unit — [OuterJoinResultSameTest.py](mysite/unmasque/test/OuterJoinResultSameTest.py), 17 cases on
the REAL methods.** The methods are bound to a duck-typed `self` with a fake `app` that *faithfully
implements EXCEPT ALL* over synthetic row bags (it reads the operand order off the diff SQL and returns
`Σ_r max(0, left[r] − right[r])`), so the test feeds the exact row sets the old positional check
mishandles:

```
same rows, different order                 -> SAME   (old positional: WRONG "not same")
same multiset w/ duplicates, reordered     -> SAME
NULL-extended rows, reordered              -> SAME   (EXCEPT ALL treats NULL = NULL)
different duplicate counts                  -> NOT SAME
disjoint rows / subset                      -> NOT SAME
empty vs empty / empty vs non-empty         -> SAME / NOT SAME
already-not-same                            -> short-circuits, ZERO db calls
equal bags                                  -> exactly two diff queries (both directions)
diff raises (sql error)                     -> NOT SAME (fail closed, though bags are equal)
__bag_diff_count: surplus count / ';' strip / parenthesised operands / ''→None / degenerate→None
```
`.venv/bin/python -m unittest mysite.unmasque.test.OuterJoinResultSameTest` → **Ran 17 tests … OK**.

**(b) End-to-end on live TPC-H** (`detect_oj` ON, other detectors off, each run in its own process). The
modified method is on the critical path — `__remove_semantically_nonEq_queries` calls it for every edge
and every ON-predicate to pick the surviving candidate:

```
Qh : select n_name, r_comment FROM nation FULL OUTER JOIN region on n_regionkey = r_regionkey and r_name = 'AFRICA';
Q_E: Select n_name, r_comment From nation FULL OUTER JOIN region
     ON nation.n_regionkey = region.r_regionkey and region.r_name = 'AFRICA';      correct (Qh == Q_E): True

Qh : Select ps_suppkey, p_name, p_type from part RIGHT outer join partsupp
     on p_partkey=ps_partkey and p_size > 4 and ps_availqty > 3350 Order By ps_suppkey Limit 10;   (OQ6)
Q_E: Select ps_suppkey, p_name, p_type From part RIGHT OUTER JOIN partsupp
     ON part.p_partkey = partsupp.ps_partkey and partsupp.ps_availqty >= 3351 and part.p_size >= 5
     Order By ps_suppkey asc Limit 10;                                            correct (Qh == Q_E): True  (139s)
```

Both recovered the exact outer-join type and ON/WHERE split, confirmed by the pipeline's own bag-equality
check (`NEP PipeLine: Extracted Query is Correct.`). `public.*` was unchanged across both runs
(`orders` = 1,500,000; `part` = 200,000; `partsupp` = 800,000).

### Findings & limits

- **Latent on the verified workloads, like WI-01/05/06.** On the single-witness `D¹`, both `Qh` and the
  candidate carry the same deterministic `ORDER BY` over a handful of rows, so the *old* positional check
  would also have returned the right verdict here — there is no live before/after mis-extraction to show.
  The win is **robustness**, not a corrected output: the bag check is immune to row reordering, ORDER-BY
  ties, and duplicate rows, and handles NULL-extended rows with native `NULL = NULL` semantics rather than
  positional string-tuple comparison. This is an equivalence *oracle* tightened from positional to bag.
- **It unblocks WI-11.** Wiring outer joins ON by default ([ExtractionPipeLine.py:233](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L233))
  needs a *trustworthy* equivalence check to decide INNER-vs-OUTER without spurious accepts/rejects; the
  positional check was the stated blocker. WI-03 is the prerequisite, not a standalone feature.
- **One-sided safety.** The only behaviour change versus the old code is on the *failure* path: a candidate
  whose diff cannot be evaluated is now rejected (fail closed) instead of crashing the stage — the sound
  direction for an emit-or-not decision.
- **`LIMIT` + tie nondeterminism is inherent, not introduced.** If `Qh` and a candidate break `ORDER BY`
  ties differently *under a LIMIT*, their clipped bags can legitimately differ — but that reflects a real
  difference in the queries, and on `D¹` (few rows, no clipping) it does not arise. The old positional
  check was strictly *more* exposed to this.
- **Side-finding (out of scope, logged for WI-11).** With `gap_aware=yes`, the gap-witness Filter stage's
  [`_build_qe`](mysite/unmasque/src/core/gap_witness.py#L203) comma-joins **all** FROM tables, so a numeric
  filter on a multi-table outer join builds an unbounded cartesian comparison query
  (`part × partsupp ≈ 1.6 × 10¹¹` rows) that never returns; worse, a `kill -9`'d client leaves the
  server-side backend running and holding locks on the working schema, blocking the next run's
  `DROP SCHEMA`. This is a pre-existing gap-aware/multi-table interaction, unrelated to the outer-join
  equivalence fix; OQ6 was verified with `gap_aware` off. Worth a dedicated item when WI-11 lands.

### Where it lives

- Detection/decision: [outer_join.py:253](mysite/unmasque/src/core/outer_join.py#L253) —
  `__are_the_results_same`, `__bag_diff_count`. Reuses the EXCEPT ALL bag-diff semantics of
  [`Comparator.run_diff_queries`/`is_match`](mysite/unmasque/src/pipeline/abstract/Comparator.py#L63) and
  the in-stage inline-diff pattern of
  [`GapWitnessFinder._diff_nonempty`](mysite/unmasque/src/core/gap_witness.py#L228).
- Emission: unchanged.
- Flag: none new — gated by the existing `outer_join` ([config.ini](mysite/config.ini) `[feature]`) /
  `connectionHelper.config.detect_oj`.
- Test: [OuterJoinResultSameTest.py](mysite/unmasque/test/OuterJoinResultSameTest.py) (17 cases).

### Thesis takeaway

Not every contribution is a new construct; some are tightening the *oracles* the framework already trusts.
The outer-join stage's verdict is only as sound as its equivalence test, and a positional row-by-row
comparison silently assumes a total order and no duplicates — assumptions that outer joins (NULL-extension)
and ORDER-BY ties routinely break. Replacing it with the project's own proven `EXCEPT ALL` bag-diff —
run *in place* against the mutated `D¹` rather than via the table-restoring `Comparator.match` — makes the
test order-, duplicate-, and NULL-correct, and converts a crash-on-bad-input path into a sound fail-closed
rejection. The result is latent on today's clean TPC-H workloads (the recurring honesty of this report:
the bug is masked until the data exercises it), but it is the trustworthy comparison oracle that the
default-on outer-join work (WI-11) is gated on.

---

## WI-11 — Wire outer joins ON by default (route to the JOIN...ON renderer)            ✅ DONE (2026-06-02)

### Problem (the "before")

The output assembler renders the FROM clause in one of two mutually exclusive shapes. The default
[`formulate_query_string`](mysite/unmasque/src/util/QueryStringGenerator.py#L432) emits a **comma-FROM**
(`", ".join(core_relations)`) and pushes every join condition into WHERE; the alternative
[`generate_from_on_clause`](mysite/unmasque/src/util/QueryStringGenerator.py#L847) emits an explicit
`T1 <join> T2 ON …`, choosing the join word from
[`join_map`](mysite/unmasque/src/util/QueryStringGenerator.py#L131):

```
('l','l') -> INNER JOIN          ('l','h') -> RIGHT OUTER JOIN
('h','l') -> LEFT OUTER JOIN     ('h','h') -> FULL OUTER JOIN
```

A comma-FROM + WHERE-equality is **semantically an INNER join** — it keeps only matched rows. So for
any hidden LEFT/RIGHT/FULL OUTER JOIN, the comma-FROM form silently drops the NULL-extended (dangling)
rows that define an outer join, and `Qh ≢ Q_E` whenever a dangling row reaches the output.

The machinery to do better already existed: a nullability probe
([`__create_importance_dict`](mysite/unmasque/src/core/outer_join.py#L120)) that, by breaking each join
key on `D¹` and reading whether a table's projected attribute survives as a non-null value, marks each
side of an edge `h` (preserved/outer) or `l` (inner) — and the `generate_from_on_clause` renderer that
turns those markers into the right join word. But all of it lived inside `OuterJoinPipeLine`, which
[`PipeLineFactory`](mysite/unmasque/src/core/factory/PipeLineFactory.py#L66) only instantiates when the
`outer_join` feature flag is set — and that flag was **off by default**. With the shipped config the
factory chose the plain `ExtractionPipeLine`, the outer-join stage never ran, and outer joins were
emitted as comma-FROM.

*Concrete before* (default config): `select n_name, r_comment FROM nation FULL OUTER JOIN region ON
n_regionkey = r_regionkey and r_name = 'AFRICA'` → `Q_E = Select n_name, r_comment From nation, region
Where nation.n_regionkey = region.r_regionkey and region.r_name = 'AFRICA'` — an INNER join that loses
every nation with no AFRICA-region match and the regions with no nation. Wrong.

This is the **emission/routing half** of a construct whose detection was already present — the same
shape as WI-01 (a render path that was correct but never exercised until the detection signal reached
it). Here the "signal" is the importance-dict markers, and the missing half is *routing the FROM by
them, by default*.

### Approach (black-box theory)

Nothing here reads `Qh`'s text. The marker is produced by a mutation experiment (break the join key on
`D¹`, run `Qh`, observe which side's projected column survives non-null); WI-11 is purely about turning
those markers into the FROM shape and enabling that path by default. Three pieces:

**1. A principled, marker-based routing decision.** Per BFS edge-sequence, route to JOIN...ON iff *some*
edge carries a non-symmetric marker (`(l,h)`/`(h,l)`/`(h,h)`); an all-`(l,l)` sequence is a pure inner
join and keeps the comma-FROM baseline (result-equivalent, structurally simpler, byte-identical output).
Before WI-11 this decision was made by **string-matching the rendered SQL** — `if q_candidate.count(
'OUTER')` — the exact WI-01 anti-pattern (a substring scan of the output standing in for a semantic
test). The new predicate
[`_seq_routes_to_join_on`](mysite/unmasque/src/core/outer_join.py#L358) reads the
`importance_dict` markers directly. The two are equivalent on well-formed input (`join_map` maps every
non-`(l,l)` pair to a string containing `OUTER`, and `(l,l)` to `INNER JOIN`), but the marker test is
robust to renderer wording, immune to an incidental `OUTER` substring, and — the point — *explicit and
unit-testable* as a routing decision rather than a side effect of rendering.

**2. A guard that the two FROM emitters never both fire.** The comma-FROM baseline is rendered once at
[`doExtractJob`](mysite/unmasque/src/core/outer_join.py#L32) into `self.Q_E`. For an all-inner sequence
the new loop simply `continue`s — it never touches the query generator — so that baseline stands as the
only FROM. For an outer sequence it calls
[`clear_from_where_ops`](mysite/unmasque/src/util/QueryStringGenerator.py#L871) (wiping `from_op` to
`''`) *before* `generate_from_on_clause` builds the JOIN...ON, so the candidate's FROM is purely the
JOIN form. The comma-list and the JOIN clause are therefore mutually exclusive per query, by
construction.

**3. The equivalence oracle that makes it safe.** The dangerous error direction is a **false outer**:
emitting LEFT/RIGHT/FULL where `Qh` is INNER changes results whenever dangling rows exist. Two
independent guards block it. The marker probe only marks a side `h` on a genuine non-null dangling
survivor; and every candidate is then re-checked by
[`__remove_semantically_nonEq_queries`](mysite/unmasque/src/core/outer_join.py#L223) under the
join-break / NULL-inject mutations using the **WI-03 EXCEPT-ALL bag oracle**
([`__are_the_results_same`](mysite/unmasque/src/core/outer_join.py#L253)), which **fails closed** on any
diff it cannot certify. If no candidate survives, the stage returns the comma-FROM baseline. So routing
can only *upgrade* to an outer join when both the marker probe and the bag oracle agree — which is
exactly why WI-03 was the stated prerequisite: it is the trustworthy INNER-vs-OUTER decision this
default-on path leans on.

**Enabling it by default.** The whole behaviour keys off one flag: `outer_join` selects
`OuterJoinPipeLine` in the factory, gates the stage via `OuterJoin.enabled`, and adaptively selects the
NULL-tolerant executable (`NullFreeExecutable` only when `Qh`'s own result actually contains nulls —
[ExecutableFactory](mysite/unmasque/src/core/factory/ExecutableFactory.py#L21)) and the NEP `re_E`
materialization path. Flipping `outer_join = yes` (and the `configParser` fallback default) turns all of
this on coherently. Non-outer queries degrade exactly to today's behaviour (a clean query has no result
nulls → same executable; an all-inner join → comma-FROM), at the cost of the extra nullability-probe
round-trips — the documented price of turning the stage on.

### Implementation

- **Routing** ([outer_join.py](mysite/unmasque/src/core/outer_join.py)):
  [`__formulateQueries`](mysite/unmasque/src/core/outer_join.py#L319) now skips a sequence unless
  `_seq_routes_to_join_on(seq)` is true, and appends the JOIN...ON candidate directly instead of
  filtering on `q_candidate.count('OUTER')`. New predicate
  [`_seq_routes_to_join_on`](mysite/unmasque/src/core/outer_join.py#L358) decides on the markers.
- **Hardening** ([`__determine_join_edge_type`](mysite/unmasque/src/core/outer_join.py#L402)): now
  defaults the marker pair to `('l','l')` (the sound INNER → comma-FROM direction) when an edge is absent
  from `importance_dict`, instead of leaving `imp_t1`/`imp_t2` unbound. The old `else` branch fell
  through to a `self.logger.debug(imp_t1, imp_t2)` on unassigned locals — a latent `UnboundLocalError`,
  harmless while the stage was opt-in but a hard crash now that it is the default path.
- **Default-on** ([config.ini](mysite/config.ini) `[feature] outer_join = yes`;
  [configParser.py](mysite/unmasque/src/util/configParser.py) `self.detect_oj = True`).
- **No QSG change**, no new flag, no predicate-shape change.

### Proof / verification

**(a) Unit — [OuterJoinRouteTest.py](mysite/unmasque/test/OuterJoinRouteTest.py), 12 cases on the REAL
methods** (no DB). The routing predicate is bound to a duck-typed `self` with a synthetic
`importance_dict`; the guard is exercised on a real `QueryStringGenerator` instantiated via `__new__`
(bypassing the DB-touching `__init__`), so the property descriptors and `join_map` are the production
ones:

```
('l','l')                       -> comma-FROM            (l,h)/(h,l)/(h,h)        -> JOIN...ON
mixed inner+outer multi-edge    -> JOIN...ON             all-inner multi-edge     -> comma-FROM
reversed-edge lookup            -> marker still resolved (verdict preserved)
missing edge                    -> defaults to INNER, no crash (hardening)
one-missing + one-outer         -> JOIN...ON
clear_from_where_ops            -> from_op and where_op both emptied
JOIN...ON route                 -> from_op has 'LEFT OUTER JOIN' + 'ON …'; the comma list 'nation, region'
                                   is GONE  (exactly one emitter: JOIN replaced comma, not appended)
('l','l') render                -> ' INNER JOIN ' and NO 'OUTER' (why the marker, not the string, decides)
```
`.venv/bin/python -m unittest mysite.unmasque.test.OuterJoinRouteTest` → **Ran 12 tests … OK**. The
WI-03 regression [OuterJoinResultSameTest.py](mysite/unmasque/test/OuterJoinResultSameTest.py) (17
cases) stays green.

**(b) End-to-end on live TPC-H, under the DEFAULT config.** The harness deliberately does **not** set
`detect_oj` — it reads from `config.ini` (now `yes`) — so the run proves the flag is on by default; it
disables the other optional detectors and `gap_aware` (orthogonal; see the limit below). Each run is its
own process:

```
config detect_oj (from config.ini, NOT overridden): True
pipeline class selected by factory: OuterJoinPipeLine

Qh : select n_name, r_comment FROM nation FULL OUTER JOIN region on n_regionkey = r_regionkey and r_name = 'AFRICA';
Q_E: Select n_name, r_comment From nation FULL OUTER JOIN region
     ON nation.n_regionkey = region.r_regionkey and region.r_name = 'AFRICA';      correct: True  (173s)

Qh : select n_name, r_name from nation, region where n_regionkey = r_regionkey;     (INNER control)
Q_E: Select n_name, r_name From nation, region Where nation.n_regionkey = region.r_regionkey;   correct: True  (110s)

Qh : select n_name, c_comment from nation RIGHT OUTER JOIN customer on c_nationkey = n_nationkey and c_acctbal < 1000;
Q_E: Select n_name, c_comment From customer LEFT OUTER JOIN nation
     ON customer.c_nationkey = nation.n_nationkey and customer.c_acctbal <= 999.99;  correct: True  (112s)
```

Three things to note. (1) The **FULL OUTER** is recovered exactly, ON/WHERE split included. (2) The
**INNER control** stays comma-FROM under the now-default-on stage — the routing guard kept the baseline,
demonstrating *no regression* on the common case (the stage runs, finds all-`(l,l)`, and keeps
comma-FROM). (3) The **RIGHT OUTER** is recovered in its **commuted equivalent** `customer LEFT OUTER
JOIN nation` (A RIGHT JOIN B ≡ B LEFT JOIN A — the two BFS roots produce both orderings and the WI-03
bag oracle accepts the equivalent one); the DEBUG trace shows the oracle's
`select count(*) from ((Qh) except all (Q_E)) as T;` probes filtering the candidate set down to the two
equivalent survivors before `sem_eq_queries[0]` is taken. The numeric filter `c_acctbal < 1000` lands in
the ON clause as `<= 999.99` (the filter stage's 0.01-precision boundary, result-equivalent — a
WI-15-class cosmetic, not an error). `public.*` was unchanged across all three runs (`orders`
1,500,000; `customer` 150,000; `nation` 25; `region` 5), and no orphaned backends remained.

### Findings & limits

- **A real before/after, not latent.** Unlike WI-01/03/05/06 (latent on clean TPC-H), this is an
  observable correctness change *by default*: before WI-11 the default pipeline emitted comma-FROM (=
  INNER) for every outer join; after, the same default config recovers the outer join. The win is gated
  on data that produces dangling rows — but the *emission* is unconditionally corrected.
- **Cost: extra probes for every query.** Turning the stage on by default means the nullability probe
  (break each join key, run `Qh`) now runs on all multi-table queries, and a FULL OUTER probes both
  sides. The stage bails early (returns the comma-FROM baseline) for single-table queries, queries with
  no join graph, and — see next — queries where a relation projects nothing.
- **Every relation must project a column** ([`__create_table_attrib_dict`](mysite/unmasque/src/core/outer_join.py#L179)):
  a relation with no projected attribute makes the importance probe unobservable (there is no value to
  read NULL-vs-non-null on), so the stage falls back to comma-FROM. Outer joins whose optional side is
  projection-free (anti-join / existence shapes) are therefore still missed — that is exactly WI-12.
- **Dangling rows masked by aggregation/LIMIT.** If a GROUP BY/aggregate or a LIMIT removes the
  NULL-extended row from the *result shape*, the marker probe sees no dangling survivor and marks the
  edge inner (a false **inner** — the safe direction, degrading to comma-FROM, result-equivalent only
  when no dangling row would have reached the output).
- **`gap_aware` × multi-table interaction (now shipped-default-relevant).** With `gap_aware = yes` (the
  config default) the gap-witness Filter stage
  ([`_build_qe`](mysite/unmasque/src/core/gap_witness.py#L203)) comma-joins all FROM tables, so a numeric
  filter on a multi-table outer join builds an unbounded cartesian comparison query that never returns.
  The e2e here disabled `gap_aware` (orthogonal to outer joins). With both `outer_join = yes` and
  `gap_aware = yes` now the shipped default, this is the highest-priority follow-up — scope the gap
  witness's `Re` to the attribute's own table rather than the full FROM cross-product. Logged as a
  dedicated future item; not fixed here (out of scope, and it does not affect the outer-join routing
  itself).

### Where it lives

- Routing/decision: [outer_join.py](mysite/unmasque/src/core/outer_join.py) —
  `__formulateQueries`, `_seq_routes_to_join_on`, hardened `__determine_join_edge_type`. Leans on the
  WI-03 EXCEPT-ALL oracle (`__are_the_results_same` / `__bag_diff_count`) and the
  [`generate_from_on_clause`](mysite/unmasque/src/util/QueryStringGenerator.py#L847) /
  [`join_map`](mysite/unmasque/src/util/QueryStringGenerator.py#L131) renderer.
- Default-on: [config.ini](mysite/config.ini) `[feature] outer_join = yes`;
  [configParser.py](mysite/unmasque/src/util/configParser.py) `self.detect_oj = True`.
- Emission: unchanged.
- Test: [OuterJoinRouteTest.py](mysite/unmasque/test/OuterJoinRouteTest.py) (12 cases).

### Thesis takeaway

A construct can be fully *detectable* and fully *renderable* and still never appear in the output,
because the path that connects detection to emission is gated off or decided by a fragile proxy. WI-11
is the routing half: it replaces a substring scan of the rendered SQL (`count('OUTER')`) with a
principled decision on the semantic markers the probe already produced, guarantees the comma-FROM and
JOIN...ON emitters are mutually exclusive, and turns the stage on by default — which is only safe because
WI-03 first made the INNER-vs-OUTER equivalence oracle trustworthy. It is the clearest illustration in
this report of the project's two-layer "Supported" rule: detection (the markers), emission (the
renderer), *and* the routing that must connect them before a construct truly counts.

---

## WI-14 — UNION vs UNION ALL (set-operator dedup discrimination)            ✅ DONE (2026-06-02)

### Problem (the "before")

A hidden `Qh` of the form `b1 UNION[ ALL] b2 …` is reconstructed by
[`UnionPipeLine`](mysite/unmasque/src/pipeline/UnionPipeLine.py): the `Union` stage partitions the
relations into per-branch FROM sets, each branch is extracted independently (with the other branches'
relations nullified), and the branch SQL strings are stitched together. The stitch token was
**hardcoded** — [`__post_process`](mysite/unmasque/src/pipeline/UnionPipeLine.py#L63) joined every branch
with `"\n UNION ALL "`. There was no bag-vs-set probe.

The set/bag distinction is a **multiplicity phenomenon** (like WI-05/WI-06): it is whether duplicate
output rows are collapsed. On the single-witness instance `D¹` the View Minimizer leaves behind, each
branch produces one row, so there is nothing to dedup and the bit is invisible — exactly the
minimization tension flagged in the checklist ground rules.

What made this subtle is what the *rest of the pipeline already does* with a `UNION`. Running the e2e
revealed that for a set-`UNION` query the group-by stage **spuriously infers a per-branch `GROUP BY` on
all projected columns** — the union's dedup reads to the group-by probe as grouping — and the branch is
then emitted as `SELECT … GROUP BY <all cols>`. Combined with the hardcoded `UNION ALL`, the recovered
query for a `UNION` was:

```
(distinct b1)  UNION ALL  (distinct b2)        =  DISTINCT(b1) ⊎ DISTINCT(b2)      (bag union of the two distinct sets)
```

whereas the true `Qh = b1 UNION b2 = DISTINCT(b1 ⊎ b2)`. These differ **exactly by cross-branch
duplicates**. So the old emission was result-correct only when the branches' outputs are
cross-branch-disjoint, and **silently wrong** the moment two branches share an output row (e.g.
`select n_regionkey from nation union select r_regionkey from region` is 5 rows, but
`DISTINCT(nation) ⊎ DISTINCT(region)` is 10).

*Concrete before* (`union = yes`, the UNION query below): `Q_E = (… GROUP BY …) UNION ALL (… GROUP BY …)`
— a bag union that double-counts any row both branches produce.

### Approach (black-box theory)

Nothing reads `Qh`'s text. The bit is recovered by a controlled-multiplicity experiment on `D¹`, reusing
enabler **S2** ([`RowProbe`](mysite/unmasque/src/core/row_probe.py)). The `UnionPipeLine` loop already
isolates one branch at a time (every other branch's relations are nullified), which is exactly the state
the probe needs: with only branch *i* populated, running `Qh` returns *only branch i's contribution under
the hidden set operator*. For that branch:

1. Reset branch *i* to `D¹` (one contributing witness row). Count `Qh`'s null-free output rows → `c0`.
2. Duplicate **one contributing witness row** of branch *i*'s first relation (S2). On a branch with no
   genuine aggregate this is guaranteed to add an identical row to the branch's *pre-dedup* output (one
   extra feeding tuple → one extra projected row; a join just multiplies it, which still grows the bag).
3. Re-run `Qh` → `c1`; revert the duplicate.

The signal:

```
c1 > c0   ->  the duplicate survived   ->  UNION ALL  (bag)
c1 == c0  ->  the duplicate collapsed  ->  UNION      (set)
otherwise ->  undecided  ->  keep the safe UNION ALL default
```

**Why growth is unconditionally sound.** An *exact-duplicate* projected row can never *grow* a
set-deduped result — under `UNION` it collapses into the row it copies. So `c1 > c0` is proof of bag
semantics with no possible false positive, on *any* branch.

**Why the no-growth signal is trustworthy here — and the key insight.** The probe runs **`Qh` itself**,
not our extracted branch. So it is *immune to our own reconstruction's artifacts* — in particular the
spurious per-branch `GROUP BY` the group-by stage inferred for the `UNION` case. `Qh`'s branch is the
plain projection; the dedup the probe observes is the global operator's. The one thing that genuinely
confounds the no-growth signal is a **real per-branch aggregate** (`sum`/`count`/…), which absorbs the
duplicate into a group's *value* regardless of the operator and would masquerade as `UNION`. So the gate
skips a branch only when *our extraction found a genuine aggregate*; a bare `GROUP BY` with no aggregate
is **not** skipped (skipping it would make `UNION` undetectable, since every branch of a `UNION` query
carries that artifact).

**Why no GROUP-BY stripping is needed.** When the probe says `UNION`, the fix simply flips the join token.
The redundant per-branch `GROUP BY` is harmless under set semantics:
`(distinct b1) UNION (distinct b2) = DISTINCT(DISTINCT(b1) ⊎ DISTINCT(b2)) = DISTINCT(b1 ⊎ b2) = Qh`.

**Safe default.** When no branch yields a decisive signal, the token stays `UNION ALL` — the historical
behaviour and the honest choice when `D` exposes no duplicate. The only deviation from the default is a
*positively observed* dedup, so a failed/empty probe can never regress a real `UNION ALL`.

### Implementation

- New probe [`SetOpProbe`](mysite/unmasque/src/core/set_op_probe.py) — a small
  `GenerationPipeLineBase` subclass so it inherits the real `do_init()` (D¹ reset), the singleton `Qh`
  executable `app`, and `get_fully_qualified_table_name`. `probe_branch(query)` runs the
  count → duplicate → recount → revert experiment and returns `'UNION ALL'` / `'UNION'` / `None`. The
  duplicate/revert go through `RowProbe` (S2, no new oracle).
- [`UnionPipeLine`](mysite/unmasque/src/pipeline/UnionPipeLine.py): the branch loop calls
  `__probe_set_op` while the branch is still isolated (before reverting the nullifications), gated by
  `__branch_is_probeable` (no genuine aggregate). Verdicts are combined by the pure static
  `_resolve_set_op` (default `UNION ALL`; `UNION` only when dedup was observed and no branch grew;
  conflicting verdicts → safe `UNION ALL`). `__post_process` now takes the resolved token, and its
  single-branch unwrap guard was changed from a fragile `"UNION ALL" not in u_Q` substring test to
  `len(u_eq) <= 1` — the substring test would have wrongly unwrapped a multi-branch *bare-`UNION`* query.
- **No QSG change** — set-op wrapping lives entirely in `UnionPipeLine`. No predicate-shape change. Gated
  by the existing `union` flag (the whole pipeline only runs under it).

### Proof / verification

**(a) Unit — [SetOpDedupTest.py](mysite/unmasque/test/SetOpDedupTest.py), 23 cases on the REAL methods**
(no DB). `SetOpProbe.probe_branch` is driven by a fake `app` that scripts `Qh`'s row count before/after
the duplicate and a fake `RowProbe` that records the duplicate/revert (constructed via `__new__` to
bypass the DB-touching `__init__`):

```
1 -> 2  => UNION ALL (+ duplicate reverted, targeted single ctid)     1 -> 1  => UNION
1 -> 4  => UNION ALL (self-join multiplicative growth)                empty witness / exec-fail / dup-fail => undecided
post-dup exec failure => undecided BUT duplicate still reverted        no core relations => undecided
_resolve_set_op: [] / [ALL] -> UNION ALL ;  [UNION] / [UNION,UNION] -> UNION ;  [ALL,UNION] -> UNION ALL (conflict)
__branch_is_probeable: plain -> yes ; GROUP BY no-agg -> yes ; sum/Count(*) -> no
__post_process: 2 branches -> '(b1)\n <TOKEN> (b2);' ; single branch -> unwrapped ; pipeline-error -> None
```
`.venv/bin/python -m unittest mysite.unmasque.test.SetOpDedupTest` → **Ran 23 tests … OK**.

**(b) End-to-end on live TPC-H** (`union = yes`, `outer_join = yes`, `gap_aware` off; each run its own
process). The *same two outer-join branches*, run once as `UNION ALL` and once as `UNION`:

```
Qh : … nation FULL OUTER JOIN region ON … and r_name='AFRICA'  UNION ALL  … nation RIGHT OUTER JOIN customer ON … and c_acctbal<1000;
Q_E: (Select n_name, r_comment From nation FULL OUTER JOIN region ON …)
     UNION ALL                                                                                  correct: True  (218s)
     (Select n_name, c_comment as r_comment From customer FULL OUTER JOIN nation ON … <= 999.99);

Qh : … (same branches)                                          UNION      … (same branches);
Q_E: (Select n_name, c_comment as r_comment From customer FULL OUTER JOIN nation ON … Group By c_comment, n_name Order By …)
     UNION                                                                                      correct: True  (146s)
     (Select n_name, r_comment From nation FULL OUTER JOIN region ON … Group By n_name, r_comment Order By …);
```

The token **flips with the operator** on identical branches — `UNION ALL` → emit `UNION ALL`, `UNION` →
emit bare `UNION` — both `correct=True`. The DEBUG trace shows the mechanism directly:

```
UNION ALL run:  SetOp_Probe: duplicate survived  (1 -> 2) => UNION ALL   [branch nation,region]
                SetOp_Probe: duplicate survived  (1 -> 2) => UNION ALL   [branch customer,nation]   -> resolved UNION ALL
UNION run:      SetOp_Probe: duplicate collapsed (1 -> 1) => UNION       [branch customer,nation]
                SetOp_Probe: duplicate collapsed (1 -> 1) => UNION       [branch nation,region]      -> resolved UNION
```

Both branches agree per query; the witness duplicate survives under bag semantics and collapses under
set semantics. `public.*` was intact across both runs (`orders` 1,500,000; `customer` 150,000; `nation`
25; `region` 5); 0 orphaned backends afterward.

### Findings & limits

- **A real emission correction (latent on this particular `D`).** WI-14 unconditionally corrects the
  *operator* the framework emits for a `UNION` query (before: always `UNION ALL`; after: bare `UNION`
  when dedup is observed). On the verified `D` the two branches happen to be cross-branch-disjoint
  (AFRICA's region comment never equals a customer comment), so the *result* is the same either way and
  `correct=True` holds for both tokens — the same "latent until the data exercises it" pattern as
  WI-01/05/06. The result-difference surfaces on cross-branch-overlapping data, where the old
  `DISTINCT(b1) ⊎ DISTINCT(b2)` double-counts shared rows and only bare `UNION` matches `Qh`.
- **The clean cross-branch-overlap demo — now unblocked (see Addendum).** The sharpest before/after —
  `select n_regionkey from nation union[ all] select r_regionkey from region` (30 rows vs 5) — was
  originally blocked by an orthogonal bug in the union FROM-clause detection (a status string `set()`
  char-split into junk relations `{' ','K','O'}` corrupted the partition). That bug is now **fixed**
  ([`_as_relation_list`](mysite/unmasque/src/core/union_from_clause.py#L62)); both queries extract, and
  this `D` turns out to be a **genuine cross-branch-overlap** case where the WI-14 token flip is *not
  latent* — it flips correctness (5 vs 10 rows). Full before/after in the Addendum below.
- **Blind spot: a genuine per-branch `SELECT DISTINCT` under `UNION ALL`.** Such a branch dedups its own
  output, so the witness duplicate collapses (no growth) even though the operator is bag — a false
  `UNION`. This is rare and squarely in unimplemented WI-21 (DISTINCT) territory: the framework does not
  detect `SELECT DISTINCT` at all, and represents it as the same `GROUP BY all-cols` artifact a `UNION`
  produces, so the two are observationally identical by row-count alone.
- **No cross-branch / within-branch duplicate in `D` ⇒ indistinguishable.** If the data exposes no
  duplicate for a branch, `UNION ≡ UNION ALL` for that `D`; the probe finds no signal and defaults to
  `UNION ALL` (honest observability limit, logged — cf. WI-02's grouped case).
- **Mixed operators (`A UNION B UNION ALL C`) are unrepresentable** (a single global token) — conflicting
  per-branch verdicts fall back to the safe `UNION ALL` default with a logged warning.

### Where it lives

- Probe: [set_op_probe.py](mysite/unmasque/src/core/set_op_probe.py) (`SetOpProbe.probe_branch`,
  `_count_qh`), built on enabler S2 [`RowProbe`](mysite/unmasque/src/core/row_probe.py).
- Decision + emission: [UnionPipeLine.py](mysite/unmasque/src/pipeline/UnionPipeLine.py) —
  `__probe_set_op`, `__branch_is_probeable`, `_resolve_set_op`, token-parameterized `__post_process`.
- Emission (QSG): unchanged. Gated by the existing `union` flag.
- Test: [SetOpDedupTest.py](mysite/unmasque/test/SetOpDedupTest.py) (23 cases).

### Thesis takeaway

WI-14 is a third *multiplicity-channel* recovery (after WI-05's const-1-vs-COUNT and WI-06's
COUNT/COUNT-DISTINCT): a bit that is invisible on the minimized single-row witness is recovered by adding
a controlled duplicate and reading whether it survives. Its distinctive lesson is **the probe must watch
`Qh`, not our own reconstruction.** The pipeline's group-by stage had already *masked* the set/bag
distinction by folding a `UNION`'s dedup into a spurious per-branch `GROUP BY` — so introspecting the
extracted branch would have concluded "this is grouped, skip," and the construct would stay undetected.
Because the probe instead duplicates a row and observes the *hidden query's* response directly, it sees
the true operator through the artifact, and flipping the single join token then yields a faithful — and,
on overlapping data, newly *correct* — `UNION`.

### Addendum (2026-06-02): union FROM-clause junk-relation fix — the overlap demo, unblocked

The original WI-14 e2e was *latent* (both branches cross-branch-disjoint, so `UNION`/`UNION ALL`
produced the same result and `correct=True` held either way). The sharp non-latent demo —
`n_regionkey from nation union[ all] r_regionkey from region` — was blocked by an **orthogonal,
pre-existing** bug in the union FROM-clause detection. This addendum records that bug, its fix, and the
resulting non-latent before/after.

**The bug.** For a bare single-table single-column UNION branch the two arms share **no** common
relation, so the common-table pass
([`UnionFromClause.get_comTabs`](mysite/unmasque/src/core/union_from_clause.py#L83) →
`FromClause.doJob(QH, TYPE_RENAME)` → [`get_core_relations_by_void`](mysite/unmasque/src/core/from_clause.py#L46))
finds zero core relations and `FromClause.doActualJob` raises `UnmasqueError(ERROR_006)`.
[`Base.doJob`](mysite/unmasque/src/core/abstract/ExtractorBase.py#L36) swallows that and returns the
status string `OK` (`"OK "`, with a trailing space). The old code stored the string verbatim as
`self.comtabs`, and two consumers char-split it:
- `UnionFromClause.doActualJob`: `set(self.get_comTabs(...))` → `set("OK ")` → `{'O','K',' '}`;
- [`algorithm1.algo`](mysite/unmasque/src/core/algorithm1.py#L26): `for ct in comtabs` iterates the
  *characters* and `cc.add(ct)` injects `'O'`, `'K'`, `' '` into **every** branch partition.

The corrupted partition then made `_after_from_clause_extract` error on relations `'O'/'K'/' '`
(another ERROR_006), aborting the whole extraction. (Confirmed in a scratch run: the old behaviour
partitions `nation ⊎ region` into `[[' ','K','O','nation'], [' ','K','O','region']]`.)

**The fix** (1 helper, 2 call-sites — [union_from_clause.py:62](mysite/unmasque/src/core/union_from_clause.py#L62)).
A static `_as_relation_list(result)` collapses any non-`list` `doJob` result (the `"OK "` status string,
`False` on setup failure, or an exception string) to `[]`, applied in both `get_comTabs` and
`get_fromTabs`. **An empty common-table set is the *expected, correct* outcome for disjoint single-table
branches — not an error.** Normalising at the source fixes all three consumers at once
(`set([])`=∅; `for ct in []` iterates nothing; `s_set.difference([])`=`s_set` in `nullify_except`).
Black-box discipline preserved: still pure nullify-and-observe, no query-text parsing.

**Before → after (genuine correctness flip, not latent).** The nation/region regionkeys *fully overlap*
(`n_regionkey ∈ {0..4}` over 25 nations, `r_regionkey ∈ {0..4}` over 5 regions), so the operator
genuinely changes the result:

```
Qh  : select n_regionkey from nation union all select r_regionkey from region     (true bag = 30 rows)
  before fix : extraction ABORTS — ERROR_006 on junk relations 'O','K',' '
  after fix  : Q_E = (Select n_regionkey From nation)
                     UNION ALL
                     (Select r_regionkey as n_regionkey From region);
               correct = True   (30 rows)                                          [144.6s]

Qh  : select n_regionkey from nation union select r_regionkey from region          (true set = 5 rows)
  before fix : extraction ABORTS (same junk-relation corruption)
  after fix  : Q_E = (Select r_regionkey as n_regionkey From region  Group By r_regionkey Order By n_regionkey asc)
                     UNION
                     (Select n_regionkey From nation  Group By n_regionkey Order By n_regionkey asc);
               correct = True   (5 rows)                                           [195.5s]
```

This is exactly where WI-14's token flip stops being latent. Even with the FROM-clause bug hypothetically
absent, the **pre-WI-14** emission for the `UNION` query would have been
`DISTINCT(nation) UNION ALL DISTINCT(region)` (the spurious per-branch `GROUP BY` joined by the old
hardcoded `UNION ALL`) = `{0..4} ⊎ {0..4}` = **10 rows ≠ 5** — it double-counts every shared regionkey.
WI-14 flips the token to bare `UNION` → `DISTINCT(nation ⊎ region)` = **5 rows = Qh**. Measured directly:

```
UNION ALL (bag)                               : 30 rows
UNION (set)                                   :  5 rows
DISTINCT(nation) UNION ALL DISTINCT(region)   : 10 rows   <- the pre-WI-14 (wrong) emission for the UNION query
```

Both branches' DEBUG traces show the SetOpProbe witness duplicate **surviving 1→2** under `UNION ALL`
and **collapsing 1→1** under `UNION`. `public.*` intact across both runs (`orders` 1,500,000; `nation`
25; `region` 5; `customer` 150,000); 0 orphaned backends.

**Verification.** [`UnionFromClauseTest.py`](mysite/unmasque/test/UnionFromClauseTest.py) — 10 cases on
the REAL `get_comTabs`/`get_fromTabs`/`doActualJob`/`algorithm1.algo` path (DB-touching `__init__`
bypassed via `__new__`, `FromClause` replaced by a scriptable fake): a `"OK "`/`False`/exception-string
result collapses to `[]` (no char-split); a genuine list passes through; `doActualJob` yields a junk-free
partition; and the real `algorithm1.algo` partitions `nation ⊎ region` into
`{{nation}, {region}}` with no junk relation in any branch. Proven non-vacuous by re-running the same
test with the helper stubbed to the identity (old behaviour) and confirming the `{'O','K',' '}` junk
reappears in every partition. `.venv/bin/python -m unittest mysite.unmasque.test.UnionFromClauseTest`
→ **Ran 10 tests … OK**; `SetOpDedupTest` (23) still green.

**Takeaway.** A degenerate-case status string (`"OK "`) flowing into a `set()`/`for` over what the caller
assumed was a relation collection is a classic char-split footgun. The robust fix is to normalise at the
boundary where the type ambiguity is introduced (the `doJob` wrapper can return a list, a bool, or a
string), and to recognise that "no common relations across union arms" is a *legitimate empty result*,
not a failure. The payoff is twofold: single-table UNION branches extract at all, and WI-14 gets its
non-latent correctness demo.

---

## WI-36 — Uncorrelated EXISTS / NOT EXISTS gate            ✅ DONE (2026-06-03)

### Problem (the "before")

An *uncorrelated* `EXISTS` gate in the WHERE clause references a relation that contributes **no
projected output column and no join edge** to the outer query — it only gates the whole result on
its (filtered) non-emptiness:

```sql
SELECT n_name FROM nation WHERE EXISTS (SELECT 1 FROM region WHERE r_regionkey > 2)
```

The FROM-clause stage classifies a relation as *core* by removing it and checking whether `Qh`
goes empty / errors ([`from_clause.py`](mysite/unmasque/src/core/from_clause.py)). For an `EXISTS`
gate, removing `region` makes the subquery unsatisfiable — `EXISTS` false → 0 rows (or, under the
default error-forwarding app type, a "relation does not exist" error) — so `region` is **correctly
swept into `core_relations`**. But `core_relations` then flows verbatim into the mutation pipeline
and finally into `q_generator.from_clause`, where
[`QueryStringGenerator`](mysite/unmasque/src/util/QueryStringGenerator.py) comma-joins every core
relation into the FROM list. The gate is therefore emitted as an unconstrained extra FROM table —
a **wrong cross join**.

*Concrete before:* the query above reconstructed as `Select n_name From nation, region Where
region.r_regionkey >= 3` — a cross product (25 × |{regionkeys ≥ 3}| = 50 rows) instead of the
gated 25.

### Approach (black-box theory)

Nothing here reads the query text. The gate is identified among the core relations by four
mutation-grounded conditions; a core relation `T` is an uncorrelated `EXISTS` gate **iff all** hold:

1. **Load-bearing** — emptying/removing `T` makes `Qh` empty. Already true by construction: that is
   exactly why `from_clause` kept `T` as core.
2. **Non-projecting** — no projected output column is attributed to `T`. Read from
   `Projection.dependencies` (the per-relation `(tab, attr)` attribution; `projected_attribs`
   itself stores only the column name). A gate projects nothing.
3. **Non-joining** — no equi-join edge (`aoa.algebraic_eq_predicates`) and no AOA/theta edge
   (`aoa.aoa_predicates` / `aoa.aoa_less_thans`) touches `T`. A gate joins nothing.
4. **NON-SCALING** — the decisive discriminator vs a **CROSS JOIN** (WI-20), which *also* empties
   the result when emptied and *also* projects/joins nothing. On the single-witness instance `D¹`,
   duplicate one contributing row of `T` (shared enabler S2,
   [`RowProbe`](mysite/unmasque/src/core/row_probe.py)) and recount `Qh`:

   | mutation | cross / inner join | `EXISTS` gate |
   |---|---|---|
   | duplicate one `T` row | `\|Qh\|` **grows** (~×(rows added)) | `\|Qh\|` **unchanged** (0/1 switch, already open) |

   A cross join pairs every outer row with the new inner row, so the result scales; an `EXISTS`
   gate was already satisfied and stays satisfied, so the row count is invariant. **Unchanged ⇒
   gate; grows ⇒ cross/inner join.**

The false-positive direction (emitting `EXISTS` for something that is really a cross join or an
inner join → wrong result) is closed by requiring *all four*: (2)+(3) rule out a projected/joined
table, and (4) rules out the cross join. Any inconclusive verdict — `Qh` unreadable, empty witness,
duplicate failed — or any exception **fails closed**: `T` stays in `core_relations` (the status-quo
comma-FROM). The gate's inner predicate is recovered for free: the filter stage already mutates
`T`'s columns and finds the boundary at which `Pop` flips (pushing every `T` row out of the
subquery predicate → gate false → empty), so the filter tuples on `T`'s columns **are** the
subquery's WHERE — WI-36 simply scopes them into the subquery instead of the outer WHERE.

The risk flagged in planning — that the View Minimizer might not retain a `T` row satisfying the
inner predicate — does not arise: `Qh` is non-empty on `D¹` by construction of a successful
minimization, and a non-empty `EXISTS`-gated result *requires* a satisfying `T` row, so `D¹`'s
witness for `T` is always a satisfying row.

### Implementation

**Detection.** [`ExistsGateProbe`](mysite/unmasque/src/core/exists_gate_probe.py) — a
`GenerationPipeLineBase` subclass modelled on WI-14's `SetOpProbe`, owning condition (4):
`set_data_schema()` + `do_init()` reset to `D¹` (so a prior stage's leftover inserts, e.g. the
Limit probe's, don't perturb the count), count `Qh`, duplicate one `T` row via `RowProbe`, recount,
revert in a `finally`. `c1 == c0` → gate (`True`); `c1 > c0` → join (`False`); else undecided
(`None`). Conditions (1)–(3) and the reclassification loop live in
[`ExtractionPipeLine`](mysite/unmasque/src/pipeline/ExtractionPipeLine.py)
(`_reclassify_exists_gates` → `__reclassify_exists_gates_impl`, `_gate_projected_tables`,
`_gate_joined_tables`, `_strip_gate_relations`), run right after the Limit stage. On a gate verdict
`T` is removed from `core_relations`, `instances`, and `alias_to_table` so it never reaches FROM.
The whole pass is wrapped to fail closed.

**Emission.** New `exists_gates` field on the query generator (`QueryDetails.exists_gates` +
property, plumbed exactly like `disjunctive_ranges`). `__generate_where_clause` appends a
`<kind> (SELECT 1 FROM T [WHERE <inner preds>])` conjunct (`__generate_exists_gate_clauses` /
`__collect_gate_inner_predicates`, reusing the existing `formulate_predicate_from_filter` for the
inner WHERE), and `__generate_arithmetic_pure_conjunctions` **skips any predicate whose table is a
gate** — those tuples belong inside the subquery, and `T` is no longer in FROM, so an outer
`T.col ...` reference would be an invalid missing-FROM-entry. No new top-level QSG slot is needed:
an `EXISTS` gate is a WHERE conjunct.

**Flag.** `exists` ([config.ini](mysite/config.ini) `[feature]`, default **OFF**; `DETECT_EXISTS`
in [constants.py](mysite/unmasque/src/util/constants.py); `detect_exists` in
[configParser.py](mysite/unmasque/src/util/configParser.py)) — off by default because it costs
extra probes and carries a correlated-subquery false-positive risk.

### Proof / verification

**(a) Unit — [`ExistsGateTest.py`](mysite/unmasque/test/ExistsGateTest.py), 26 cases on the REAL
methods** (fake `app` scripting `|Qh|`, fake `RowProbe`, `unittest.mock` for the injected probe;
DB-touching `__init__` bypassed via `__new__`):

```
ExistsGateProbe.is_nonscaling_gate : 1->1 gate; 1->2 / 1->4 join; empty-witness / baseline-fail /
                                     dup-fail / post-dup-fail / not-core -> undecided; dup reverted
_gate_projected_tables / _gate_joined_tables : dependencies + aggregated cols; equi + AOA edges;
                                     constant AOA nodes skipped; real-PGAOcontext regression guard
_reclassify_exists_gates : gate stripped; scaling/inconclusive kept; flag-off / single-relation
                                     no-op; joined relation excluded before probing; never strips last
QSG __generate_where_clause : EXISTS / NOT EXISTS rendered; gate predicate moved into subquery and
                                     removed from outer WHERE; no-inner-pred; no-gate unchanged
```
`python -m unittest mysite.unmasque.test.ExistsGateTest` → **Ran 26 tests … OK**; the full
regression sweep (CountRender, CountDistinctAgg, SetOpDedup, OuterJoinRoute/ResultSame,
GroupByConst1, RowProbe, LimitSearch) stays green.

**(b) End-to-end on live TPC-H** (`exists=yes`, `detect_oj=off` so the plain `ExtractionPipeLine`
runs, `gap_aware=off`, each run its own process). All three **`correct=True`** (bag-equality over
full `D`):

```
EXISTS (the deliverable):
  Qh : select n_name from nation where exists (select 1 from region where r_regionkey > 2)
  Q_E: Select n_name From nation Where EXISTS (SELECT 1 FROM region WHERE region.r_regionkey >= 3);
  trace: ExistsGateProbe: region duplicate left |Qh| unchanged (1 -> 1) => EXISTS gate

CROSS-JOIN control (must NOT be EXISTS):
  Qh : select n_name from nation, region
  Q_E: Select n_name From nation, region;          (comma-FROM kept)
  trace: ExistsGateProbe: region duplicate scaled |Qh| (1 -> 2) => cross/inner join

NOT EXISTS:
  Qh : select n_name from nation where not exists (select 1 from region where r_regionkey > 9)
  Q_E: Select n_name From nation Where EXISTS (SELECT 1 FROM region WHERE region.r_regionkey <= 9);
```

The control is the proof of condition (4): the **same** `region` relation — non-projecting and
non-joining in both queries — is classified a gate (`1→1`) in the first and a cross join (`1→2`) in
the second. The `CardinalityProbe` independently logged `region B_dup=1 ratio=1.00` (non-scaling)
in the gate runs, corroborating the signal. `public.*` was intact (orders 1,500,000; lineitem
6,001,215) before and after all three; 0 orphaned backends.

### Findings & limits

**NOT EXISTS is reconstructed result-correct, not invisible — and this corrects the plan.** The
checklist predicted NOT EXISTS would be invisible to `from_clause` ("emptying `T` changes nothing").
That assumed the *void* method, but the default app type is `SQL_ERR_FWD`, so `from_clause` uses the
**error** method: it renames the relation away and a referenced subquery relation then errors
(`relation "region" does not exist`) ⇒ core — for **both** EXISTS and NOT EXISTS. So the NOT EXISTS
gate relation is **kept core**. The filter then recovers the **complement** of the inner predicate
(the witness must *fail* the inner predicate for the gate to be open: `> 9` ⇒ recovered `<= 9`), and
WI-36 emits a positive `EXISTS(¬P)`. This is **bag-equivalent on the extraction database for every
extractable NOT EXISTS gate**: an uncorrelated `NOT EXISTS(P)` yields a non-empty outer result only
when *no* `T` row satisfies `P` (∀¬P), under which `EXISTS(¬P)` is likewise true (T non-empty). So
WI-36 produces a **result-correct** query but does **not recover the polarity** — `EXISTS` vs
`NOT EXISTS` is unobservable on the single witness `D¹` (the witness satisfies the recovered
predicate either way), and the emitted `EXISTS(¬P)` would diverge from `NOT EXISTS(P)` on a database
where `T`'s rows *straddle* `P`. A clean polarity probe is specified for future work: INSERT a `T`
row that *violates* the recovered predicate `Q` (satisfies `¬Q`); under `EXISTS(Q)` the witness keeps
the gate open (`|Qh|` unchanged), under `NOT EXISTS(¬Q)` the inserted row closes it (`|Qh|` → 0);
then emit `NOT EXISTS(¬Q)`.

Other honest limits:

- **Correlated EXISTS is Infeasible.** On `D¹` the correlation column holds a fixed witness value,
  so the filter records it as a constant `=` predicate, making a correlated gate indistinguishable
  from an uncorrelated one. The default-OFF flag mitigates the resulting false-positive risk.
- **Semi-join to a UNIQUE key that every outer row matches** is result-equivalent to an inner join
  on many `D` — but such a gate carries a join edge, so it **fails condition (3)** and is kept as a
  join (no spurious `EXISTS`). The win is scoped to gates that are *not* join-equivalent on the test
  `D`.
- **A blanket global aggregate** (COUNT/SUM with no GROUP BY) collapses `|Qh|` to one row, defeating
  the row-count signal of condition (4) — but such queries typically also defeat `from_clause`'s
  emptiness classifier (a `COUNT(*)` always returns a row), so they rarely reach this stage.
- **A latent dev bug worth recording:** `_gate_projected_tables` first read `PGAOcontext.aggregate`,
  which is a *write-only* property whose getter `raise NotImplementedError` — and `getattr(..., None)`
  does **not** suppress `NotImplementedError` (only `AttributeError`). It propagated and aborted the
  whole extraction (empty `Q_E`) even though detection is otherwise fail-closed. Fixed to read the
  concrete `aggregated_attributes`, with a real-`PGAOcontext` regression test. Lesson: `getattr` with
  a default is not a safe probe against a property whose getter can raise.
- **Pipeline composition:** verified under the plain `ExtractionPipeLine`. Composing with the
  `OuterJoinPipeLine` post-processor (which independently rebuilds FROM from `genPipelineCtx`, still
  holding the gate) is future integration — there is no shipped-default conflict because `exists` is
  OFF by default.

### Where it lives

- Detection: [`exists_gate_probe.py`](mysite/unmasque/src/core/exists_gate_probe.py) (condition 4);
  [`ExtractionPipeLine`](mysite/unmasque/src/pipeline/ExtractionPipeLine.py) `_reclassify_exists_gates`
  & helpers (conditions 1–3 + reclassification + gate stripping); built on
  [`RowProbe`](mysite/unmasque/src/core/row_probe.py) (S2).
- Emission: [`QueryStringGenerator`](mysite/unmasque/src/util/QueryStringGenerator.py) — `exists_gates`
  field/property, `__generate_exists_gate_clauses`, `__collect_gate_inner_predicates`, the gate-table
  skip in `__generate_arithmetic_pure_conjunctions`.
- Flag: `exists` ([config.ini](mysite/config.ini), [constants.py](mysite/unmasque/src/util/constants.py),
  [configParser.py](mysite/unmasque/src/util/configParser.py)), default OFF.
- Test: [`ExistsGateTest.py`](mysite/unmasque/test/ExistsGateTest.py) (26 cases).

### Thesis takeaway

An `EXISTS` gate and a cross join are *identical* on every channel the pipeline normally uses — both
are load-bearing, both can project and join nothing, both empty the result when emptied — and they
separate cleanly only on the **multiplicity** channel: duplicate one contributing row and ask whether
the result *scales*. It is the same recurring move as WI-05/06/14 (when the single-witness value
channel is ambiguous, recover the missing bit from a controlled change in cardinality), here applied
to a *structural* construct rather than an aggregate. The NOT EXISTS result is the report's sharpest
illustration of the gap between *result-correctness* (bag-equality on the extraction `D`, which WI-36
achieves for NOT EXISTS via the complement) and *faithful construct recovery* (the `EXISTS` vs
`NOT EXISTS` polarity, which is genuinely unobservable on a single witness row): the framework can be
provably right about the answer while structurally wrong about the question.

---

_Append the next item below this line._

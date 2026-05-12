# Detecting Multi-Instance (Self-Join) Tables — Algorithm 1

> Status: implemented and opt-in (`[feature] multi_instance`); see `mysite/unmasque/src/core/`
> (`multiplicity / alias_aware_minimizer / cross_alias_predicate / per_alias_filter /
> per_alias_pinned_filter / alias_aware_assembler.py`), wired into
> `mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py` +
> `mysite/unmasque/src/pipeline/ExtractionPipeLine.py`.  Pure-logic unit tests in
> `mysite/unmasque/test/{Multiplicity,AliasAwareMinimizer,CrossAliasPredicate,PerAliasFilter,
> PerAliasPinnedFilter,AliasAwareAssembler}Test.py`; end-to-end integration tests in
> `mysite/unmasque/test/MultiInstancePipelineTest.py` (validated against TPC-H SF=0.1 — see
> `docs/multi_instance_HANDOFF.md` for what works, the bugs that run surfaced, and what's left).
> Audience: anyone working on the UNMASQUE / XPOSE extraction pipeline.

---

## 1. The problem in one example

Hidden query:

```sql
SELECT  ps1.ps_partkey
FROM    partsupp ps1, partsupp ps2
WHERE   ps1.ps_partkey = ps2.ps_partkey
  AND   ps1.ps_supplycost <= ps2.ps_supplycost
```

The From-Clause extractor (Extraction-by-Error / Extraction-by-Voiding) tells us only that
`partsupp` is *touched* by the query. It cannot tell us that `partsupp` appears **twice**
under two aliases. Worse, the SIGMOD'21 minimization theorem ("there is always a `D_min`
with a single row per table") and Alaap's Lemmas 4.1–4.4 silently assume a one-to-one
alias→row binding, which is false the moment a self-join has a non-trivial cross-alias
predicate. Today Xpose punts the whole issue to the LLM layer (XFE guideline **G6**:
"if `Q_S` has a multi-instance table, keep all the table instances").

`MultiplicityDetect` (Algorithm 1) recovers `mult(T)` — the alias count of every physical
relation — using only black-box probes, so the rest of the pipeline can become alias-aware.

UNMASQUE is strictly black-box: it may only *run* the hidden query against a database and
look at the result. So the detector works by perturbing the database and watching the
result, never by reading SQL.

---

## 2. The idea: cardinality-scaling fingerprint

Run after view minimization, when each core table holds a tiny witness set on which `Q_H`
is FIT. For one table `T` at a time (everything else stays at `D_min`):

1. take a snapshot of `T`'s current content into a temp table;
2. for `n = 2, 3, …, kmax+2`, replace `T` by `n` copies of that snapshot and record
   `f(n) = |Q_H|` (bag semantics — Postgres `SELECT` without `DISTINCT` keeps duplicates);
3. `f` is a polynomial in `n` whose **degree equals the number of unpinned aliases of `T`**.
   Equi-joins between two aliases on the duplicated key keep *every* combination
   (`f(n) = c·n^k`); a non-strict cross-alias inequality keeps it polynomial of the same
   degree (`f(n) ~ c·C(n,k)`). Either way the degree is read off the **leading non-zero
   finite difference** of `f(1), f(2), …` — robust to the unknown leading coefficient and
   to the `n^k` vs `C(n,k)` shape.

Why the degree is exactly the alias count: this is the black-box shadow of the provenance
polynomial (Green–Karvounarakis–Tannen, PODS'07). Each output tuple is annotated with a
sum of products; every product has one factor per alias of `T`, so the monomial degree in
`T`'s annotations is `mult(T)` (minus pinned aliases).

Everything happens inside a transaction that is rolled back, so the database is left exactly
as it was found even if a probe throws.

Inflation copies the **whole** current table content `n` times (not one row), so a witness
set that already contains the rows that distinguish the aliases (a strictly increasing
`x_a < x_b < x_c` for a 3-way chain, say) keeps the cross-alias predicates satisfiable
across copies and the count still scales as `n^k`. This is why running after (even a
collapsed) minimization works: a FIT D_min necessarily holds such a witness set.

### Fallback — FreshTupleProbe

When `GROUP BY` / `DISTINCT` / `LIMIT` flattens or caps `f(n)`, insert **one fresh row** of
`T` with sentinel attribute values and count, over all output rows, the maximum number of
output cells holding a sentinel; divide by `|attrs(T)|`. Each alias the fresh row binds to
contributes its own slice of columns to that output row, so the maximum is `mult(T)`.

---

## 2b. Algorithm 2 — alias-aware k-coloured halving (`src/core/alias_aware_minimizer.py`)

Once `mult` is known, the *brute-force* one-row-per-table minimizer is unsafe for any
`R` with `mult(R) = k ≥ 2`: it can settle on a single row that is a *fixpoint* of the
self-join (one row satisfying `t1.x = t2.x`), which hides the second alias from every
downstream extractor. Algorithm 2 builds an **alias-aware D_min**: a set of `k` rows of `R`
on which `Q_H` is still FIT and that keeps the `k` aliases distinguishable.

For one multi-instance relation `R` at a time (everything else stays at its legacy single-row
D_min — the right semantics for minimizing `R` in isolation):

1. **Pool**: start from `R`'s legacy single-row D_min (guaranteed FIT) **plus** a bounded
   sample of `R`'s original content — candidate rows for the remaining aliases. Grow the
   sample (×4) until `Q_H` is FIT and there are at least `k` candidates.
2. **Binary halving** of the candidate pool, only ever keeping a half that (a) keeps `Q_H`
   FIT and (b) has `≥ k` rows — the multiplicity lower bound is never crossed (Lemmas C.1/C.2).
3. **Colour-partitioned cleanup**: split what remains into `k` contiguous "colours" and
   greedily drop rows colour by colour, never letting the total fall below `k` — so the
   result is one representative row per colour, not `k` copies of one row (Lemma C.3 /
   Corollary C.4).

All of this runs inside a transaction that is **rolled back**, so the *live* single-row
D_min that the (not-yet-alias-aware) downstream extractors consume is untouched. The
alias-aware D_min is published separately, on `self.alias_aware_min_instance_dict`, for
Algorithms 3 & 4 to consume once they exist; until then this stage is purely additive.

`AliasAwareMinimizer.doJob(query)` populates:

| field | meaning |
|---|---|
| `alias_aware_min_instance_dict` | `{table → [header, row, …]}`, ideally `mult(table)` data rows |
| `expanded` | tables for which a genuine multi-row witness set was found |
| `fallback` | tables for which we fell back to `k` copies of the legacy single row |

The core routine `kcolour_halve(content, k, fit_fn)` is factored out (independent of the DB)
and unit-tested.

---

## 2c. Algorithm 3 — cross-alias predicate extraction (`src/core/cross_alias_predicate.py`)

With `mult(R) = k` and the alias-aware D_min in hand, this stage recovers the predicates
that relate the `k` aliases of `R` to one another — invisible to the legacy pipeline, which
has no notion of aliases:

* **Intra-alias self-equi-joins `t.c = t.c'`** (a within-row column equality on one alias).
  Detected on a *single-row* copy of the witness: if it has `R.c = R.c'`, changing `R.c`
  alone makes `Q_H` UNFIT, and also setting `R.c' := R.c` makes it FIT again ⇒ `t.c = t.c'`.

* **Same-column inter-alias predicates `t_p.c REL t_q.c`** for `REL ∈ {=, <, >}` (or "no
  relation"). We give column `c` `k` distinct, ascending **discriminator windows** across
  the `k` rows of the alias-aware D_min (where the data range allows it without breaking
  `Q_H`), run `Q_H` once, and look at the output slots that expose `c`. In every output row
  the slot for `t_p.c` carries some window value and `t_q.c` carries some window value; if
  `t_p.c` is always in a strictly lower window than `t_q.c`, the query has `t_p.c < t_q.c`;
  always the same window ⇒ `t_p.c = t_q.c`; mixed ⇒ the aliases are free w.r.t. `c`. This is
  the black-box reading of which alias-to-row binding combinations the hidden query keeps
  (the provenance combinations of report §D) — no per-predicate mutation needed.

Columns whose `k` values cannot be made distinct without breaking `Q_H` are reported in
`coupled_columns` — they may carry a same-column cross-alias equi-join, an equi-join to
another (single-row) table, or a tight constant filter.

v1 deliberately leaves for future work: cross-*column* cross-alias predicates
(`t_p.a < t_q.b`, `a ≠ b`), and cross-alias predicates on columns `R` does not project
(those need the s-value-bound-floating machinery Xpose already has for single-table
algebraic predicates, lifted to alias pairs).

It also **alias-lifts the projection extractor** along the way: when it discriminates `R`
and runs `Q_H`, each output column whose value is constant across all rows and uniquely
equals one discriminator window value is attributed to `(R, alias_index, source_col)` — that
tells the assembler which `R_a<j>` a projected column really belongs to. (Output columns
whose value *varies* are alias-symmetric — no inter-alias predicate pins them — and are left
unattributed; `R_a1` is then as good as any.)

`CrossAliasPredicate.doJob(query)` populates:

| field | meaning |
|---|---|
| `cross_alias_predicates` | `{table → [pred, …]}`, each `{'kind':'intra_eq','cols':(c,c')}` or `{'kind':'inter','col':c,'op':'<','slots':(sp,sq)}` |
| `coupled_columns` | `{table → [cols]}` columns that could not be discriminated |
| `output_attribution` | `{output_col_index → (table, alias_index, source_col)}` — the projection alias-lift |
| `notes` | per-table remarks on what was / wasn't analysable |

Pure helpers `spread_values`, `_window_index`, `infer_inter_alias_predicates`,
`attribute_output_columns` are factored out and unit-tested.

---

## 2d. Algorithm 4 — per-alias filter extraction (`src/core/per_alias_filter.py`)

Each alias of a multi-instance table can carry its *own* filter on a column —
`… WHERE t1.x ≤ 10 AND t2.x ≤ 20`. The legacy filter extractor only ever recovers
the *tightest* bound (`≤ 10`), walking one row. This stage recovers the whole set of
per-alias bounds by a **cardinality-step search**.

Let `A` be the legacy D_min row of `T` (Q_H is FIT on `{A}`, so `A.c` is inside every
per-alias interval). Probe with `T` set to two rows — `A`, and `B` = `A` with `c := V`:

```
f(V) = |Q_H| on T = {A, B(V)}   =  C · ∏_{a=1..k} (1 + [V ∈ [l_a, u_a]])
```

— each alias independently binds `A` (always allowed) or `B` (iff `V` is inside that
alias's interval). `f` is a step function whose break points, as `V` moves away from
`A.c`, are exactly the per-alias bounds (and the size of each step — a ×2 per alias —
says how many aliases share it). We push `V` out to the column type's domain limit to
see whether a bound exists on that side, then bisect to locate it (`find_step_breakpoints`).

The *size* of each break-point's cardinality jump (a ×2 per alias that shares that bound) is
read off too, so the per-alias bound is reported as a **multiset** (one entry per alias with a
finite bound), not just the distinct values — that distinguishes "both aliases ≤ 10" (`[10,10]`)
from "only `t1` ≤ 10" (`[10]`), which matters for the assembler.

`PerAliasFilter.doJob(query)` populates:

| field | meaning |
|---|---|
| `per_alias_filters` | `{table → {col → {'lower':[…], 'upper':[…], 'lower_multiset':[…], 'upper_multiset':[…], 'tightest':(l,u), 'loosest':(l,u)}}}` |
| `notes` | per-table remarks (uniform / suppressed / coupled-columns-skipped / …) |

Columns flagged by Algorithm 3 as cross-alias-coupled are skipped (the aliases aren't
independent there). v1 leaves for future work: **per-alias HAVING** (`HAVING l ≤ AGGR(t_i.x)
≤ u` — composes with Xpose/Alaap's aggregate-predicate "diagram" machinery, not this probe);
GROUP BY / DISTINCT queries that flatten `|Q_H|` (the step then lives in an aggregate value);
text columns; bounds further than the type's practical domain limit from the witness value.
The pure helpers `find_step_breakpoints`, `_midpoint`, `_adjacent`, `_add` are unit-tested.

---

## 2e. Alias-aware query assembler (`src/core/alias_aware_assembler.py`)

The legacy pipeline always returns a *single-instance* query. This stage stitches the
Algorithm-1–4 artifacts onto the legacy extracted query string to produce a **candidate
multi-instance query** (`self.alias_aware_query`), published *alongside* the legacy result:

* the `FROM` clause becomes `R AS R_a1, …, R AS R_ak` for every `R` with `mult(R) = k ≥ 2`;
* the `SELECT` list is rebound using the **projection alias-attribution** (Algorithm 3's
  `output_attribution`): a plain projected column `c` that the discriminator run pinned to
  alias `j` becomes `R_a<j>.c` (so `SELECT t1.x, t2.x` is reconstructed faithfully, not
  collapsed to `SELECT t1.x, t1.x`), and a single-column aggregate `f(c)` (`f ≠ COUNT`)
  attributed the same way becomes `f(R_a<j>.c)`. `GROUP BY` / `ORDER BY` references inherit
  that alias when the column is projected from exactly one alias, else fall back to `R_a1`;
* everything else (the legacy `WHERE` filters / joins, `COUNT`/composite/unattributed `SELECT`
  items, `AS`-names left alone, string literals untouched) is rebound to the primary alias `R_a1`;
* appended to `WHERE`: intra-alias self-equi-joins `R_ai.c = R_ai.c'` (every alias);
  same-column inter-alias predicates `R_ap.c REL R_aq.c` (slots topo-ordered into aliases);
  "coupled" columns chained `R_a1.c = R_a2.c = … = R_ak.c`; the per-alias filter bound
  *multiset* (tightest → `R_a1`, already in the legacy `WHERE`; the rest in order → `R_a2 …`;
  aliases beyond the multiset length stay unbounded — so "only `t1` ≤ 10" no longer leaks
  `R_a2.x ≤ 10`, and "both ≤ 10" no longer drops `R_a2.x ≤ 10`).

Then it does a small **verifier-guided search**: it builds a handful of candidate *variants*
(the discriminator-probe-attributed one — §2f below; the plain default; the bound-tail-reversed
one), runs each *and* `Q_H` against the original database, compares their result multisets, and
keeps the first variant that reproduces `Q_H` (setting `verified = True`). If none verifies it
keeps the probe/default variant with `verified = False` (the candidate is still emitted as a
best guess); `verified = None` means it couldn't be checked (no comparable result / too large /
error). This is what the report's §F per-`(alias, attribute)` probing ultimately enables —
"verify the candidate against the DB instead of just emitting it".

Even unverified, the candidate is observationally *exact* whenever the aliases are symmetric on
each column (relabeling `t1 ↔ t2` is a no-op then) — which covers the common
2-way-self-join-with-equi-join-plus-inequality patterns (TPC-H Q2/Q17 rewrites). It is
best-effort otherwise (e.g. an inter-alias ordering predicate *plus* differing per-alias bounds
where the probe didn't run / `k ≥ 3`; a join through a non-alias-coupled FK → only `R_a1`
joins) — every such assumption is in `notes` / `self.info['ALIAS_AWARE_QUERY']`, and `verified`
flags the result either way. Runs in `ExtractionPipeLine.extract` after the legacy `eq` is
built; purely additive (a failure leaves only the legacy query). The pure helpers
`_split_clauses`, `_qualify_text`, `_qualify_select`, `_rewrite_select`, `_qualify_clause`,
`_topo_order_slots`, `_result_multiset`, `_strip_col_atoms`, `build_alias_aware_query` are
unit-tested.

---

## 2f. Per-`(alias, attribute)` discriminator probe (`src/core/per_alias_pinned_filter.py`, report §F)

Algorithm 4 recovers the *set* of per-alias filter bounds on a column but not which alias owns
which — because, with the aliases free w.r.t. that column, the assignment is observationally
irrelevant. When an **inter-alias chain on some column `d`** pins the aliases (`t_{a1}.d <
t_{a2}.d < …`), the assignment *is* identifiable: discriminate `d` (k distinct ascending values
across the alias-aware D_min's rows), so the chain forces alias `a_i` onto the i-th-smallest-`d`
row; then, for every other column `c`, recover `a_i`'s bound on `c` **directly** by varying just
that one pinned row's `c` and binary-searching the FIT/UNFIT boundary (`recover_bound_via_fit_probe`
— a `find_step_breakpoints` on the 0/1 FIT signal): every other alias keeps binding its own row,
whose `c` is inside its interval (`Q_H` is FIT on the D_min), so `Q_H` stays FIT iff the varied
value is in `a_i`'s interval, and the boundary *is* `a_i`'s bound. This works for any `k ≥ 2`
(the original v1 used a single confirming probe and only handled `k = 2`; the direct binary
search is both simpler and complete). When no full chain pins all `k` aliases, attribution is
left to the verifier-guided search above.

Output `pinned_filters[tab] = {alias_index → {col → {'lower':l,'upper':u}}}` (1-based alias
index, in the chain's ascending order; only aliases with a finite bound on `col` appear). The
assembler uses it as the first candidate variant: it strips the rebound legacy filter atoms on
`col` and emits the probed bounds with the correct alias identity (so the legacy `R_a1.col ≤
v_tight` — too tight if the probe says `a1` owns the loose bound — is replaced). Runs in a
rolled-back transaction, purely additive, behind `[feature] multi_instance`. Pure helper
`recover_bound_via_fit_probe` is unit-tested (`test/PerAliasPinnedFilterTest.py`). **Not yet
done:** attributing the *legacy join* edge `R.fk = S.x` to a specific alias of `R` when `fk` is
not alias-coupled (when it is, the assembler's coupled-column chain `R_a1.fk = R_a2.fk = …`
already carries the join to every alias — exact there); needs the legacy join graph to be
alias-aware (§6).

---

## 2g. Minimizer restructure (`MinimizerBase.py`, `view_minimizer.py`, `_mutation_pipeline`)

The legacy view minimizer used to **abort the whole pipeline** on a self-join that needs more
than one distinct row per table (`… WHERE t1.x < t2.x`): the base-executable halving took a
half *without re-checking FIT*, broke the table, and the post-minimization sanity check failed.
`check_sanity_when_base_exe` now mirrors the null-free version — it takes the upper half only if
that half keeps `Q_H` FIT, and signals "stop" (`None, None`) when *neither* half does — and
`do_intraPage_copyBased_binary_halving` handles that by restoring the table and stopping. This
is safe for `mult = 1` (the witness row is always in exactly one half, so the "neither" branch
never fires).

On top of that, when `[feature] multi_instance` is on, the minimization phase follows the
report's structure: **MultiplicityDetect runs *before* the view minimizer** (on the sampled
post-Cs2 DB), and its result is used as the minimizer's **per-table floor** —
`ViewMinimizer.min_rows = {R: mult(R)}` — so `do_intraPage_copyBased_binary_halving` stops at
`mult(R)` rows, never collapsing a k-way self-join below `k`. `populate_dict_info` then captures
the full `k`-row witness set into `global_min_instance_dict` (Algorithms 2–4 consume it — and
Algorithm 2 short-circuits, reusing it directly instead of re-deriving from a pool). Finally the
*live* tables (and their D_min copies) are **re-collapsed to the first row**, so the (not-yet
alias-aware) Filter / equi-join / AOA / generation extractors keep seeing a single witness row
exactly as in the legacy pipeline — the only difference for them is *which* row, and they update
all rows uniformly anyway, so they still recover the tightest/legacy predicates while Algorithm 4
recovers the per-alias ones. **Exception:** a *strict* self-join (`t1.x < t2.x`) has no single
row on which `Q_H` is FIT, so the 1-row collapse would break `Q_H`; the re-collapse detects this
(it re-runs `Q_H` after collapsing) and instead keeps the `k`-row witness set, so the legacy
extractors at least start from a FIT instance — they still won't fully handle a strict self-join
(that needs the alias-aware extractors, §6), but the multi-instance artifacts are all recovered.
With `multi_instance` off, `min_rows` is empty ⇒ floor 1 ⇒ legacy behaviour, plus the
(always-safe) no-crash fix.

---

## 3. What it returns

`MultiplicityDetect.doJob(query)` populates:

| field | meaning |
|---|---|
| `mult` | `{table → int}` — multiplicity of every core relation (1 = appears once) |
| `method_used` | `{table → "scaling" \| "fresh-tuple" \| "trivial"}` |
| `ambiguous` | tables whose multiplicity could not be pinned down (see §5) |
| `cardinalities` | `{table → [f(1), f(2), …]}` raw probe results, for diagnostics |

The extraction pipeline stores the `mult` map on `self.mult`, the alias-aware D_min on
`self.alias_aware_min_instance_dict`, the cross-alias predicates on
`self.cross_alias_predicates` (+ `self.cross_alias_coupled_columns`), the projection
attribution on `self.projection_alias_attribution`, the per-alias filter bounds on
`self.per_alias_filters` (+ the alias-attributed bounds on `self.per_alias_pinned_filters`), and
the candidate multi-instance query on `self.alias_aware_query` (+ `self.alias_aware_query_verified`
∈ {True, False, None}); plus info blobs `self.info['MULTIPLICITY']`, `self.info['ALIAS_AWARE_DMIN']`,
`self.info['CROSS_ALIAS_PREDICATES']`, `self.info['PER_ALIAS_FILTERS']`,
`self.info['PER_ALIAS_PINNED_FILTERS']`, `self.info['ALIAS_AWARE_QUERY']` (the last including
`'verified'`). The legacy single-instance query is still the pipeline's primary result.

---

## 4. How to enable it

Off by default so legacy behaviour and app-call counts are unchanged. In `config.ini`:

```ini
[feature]
...
multi_instance = yes
```

Cost when enabled: Algorithm 1 ≈ `kmax + 3` extra black-box calls per core relation (≈ 7
with the default `kmax = 4`); Algorithms 2–4 run only for multi-instance relations —
Algorithm 2 ≈ `log(pool) + 2k` calls each, Algorithm 3 ≈ `O(|cols|^2)` (intra-alias scan)
+ one call per discriminable column group + one final read, Algorithm 4 ≈ `O(|cols|)` ×
(one outward probe + a ≤30-step bisection) per side. All probe inside rolled-back transactions.

---

## 5. Boundary cases (what it deliberately does *not* solve)

* **Idempotent self-joins** whose canonical core folds to the diagonal and whose projection
  erases the redundancy (`SELECT t1.x FROM T t1, T t2 WHERE t1.key = t2.key` with `key`
  unique) are homomorphism-equivalent to a single scan (Chandra–Merlin, STOC'77) — *no*
  black-box scheme can tell them apart. Algorithm 1 reports `mult = 2` here (the query
  genuinely has two instances and `Q_H` does scale under bag semantics); it does **not**
  attempt the homomorphism-folding analysis that would prefer the smaller equivalent query.
  The truly indistinguishable cases are reported as `mult = 1`.
* If the (pre-)minimized D_min that Algorithm 1 runs on does **not** hold a complete witness
  set — e.g. the legacy minimizer collapsed or *failed* on a hard self-join *before*
  Algorithm 1 ran — the result inherits that outcome (such tables land in `ambiguous`).
  In particular, the legacy `ViewMinimizer` with the *base* executable can abort on a
  self-join that needs `> 1` distinct row per table (it takes a half without re-checking
  FIT); fixing that needs the pipeline restructure noted below.
* Very wide `kmax`: a `(kmax+1)`-way self-join is reported as `kmax` (assumption SJ-A1).

---

## 6. Implementation status / next steps

Implemented:

* `src/core/multiplicity.py` — `MultiplicityDetect` (Algorithm 1) + `_fresh_tuple_probe`
  (Algorithm 1b), with transaction-isolated probing and the finite-difference degree
  estimator `_finite_difference_degree`.
* `src/core/alias_aware_minimizer.py` — `AliasAwareMinimizer` (Algorithm 2): builds the
  alias-aware D_min for every multi-instance relation via the colour-aware FIT-guided
  `kcolour_halve` routine, in a rolled-back transaction; published on
  `self.alias_aware_min_instance_dict` without disturbing the legacy D_min.
* `src/core/cross_alias_predicate.py` — `CrossAliasPredicate` (Algorithm 3): intra-alias
  self-equi-joins, same-column inter-alias predicates, **and the projection alias-lift**
  (`output_attribution`: output-column → `(table, alias, source_col)`) via discriminator
  injection + slot-value inference, in a rolled-back transaction; published on
  `self.cross_alias_predicates` / `self.projection_alias_attribution` /
  `self.info['CROSS_ALIAS_PREDICATES']`.
* `src/core/per_alias_filter.py` — `PerAliasFilter` (Algorithm 4, filter part): per-alias
  filter bounds via a cardinality-step search + bisection, with the per-alias bound
  *multiset* (multiplicities read off the cardinality-jump sizes), in a rolled-back
  transaction; published on `self.per_alias_filters` / `self.info['PER_ALIAS_FILTERS']`.
* `src/core/per_alias_pinned_filter.py` — `PerAliasPinnedFilter` (report §F probe): attributes
  Algorithm 4's per-alias bound multiset to specific aliases via a targeted-mutation probe when
  an inter-alias chain pins them (`k = 2` in v1); published on `self.per_alias_pinned_filters` /
  `self.info['PER_ALIAS_PINNED_FILTERS']`.
* `src/core/alias_aware_assembler.py` — `AliasAwareAssembler`: multi-instance reconstruction of
  the extracted query from the Algorithm-1–4 + probe artifacts, then a **verifier-guided search**
  (build a few variants — probe-attributed / default / bound-tail-reversed — run each vs `Q_H` on
  the original DB, keep the first that matches); published on `self.alias_aware_query`
  (+ `self.alias_aware_query_verified`) / `self.info['ALIAS_AWARE_QUERY']` *alongside* the legacy
  result.
* `src/core/abstract/MinimizerBase.py` / `src/core/view_minimizer.py` — the no-crash fix for hard
  self-joins (`check_sanity_when_base_exe` re-checks the upper half / signals stop;
  `do_intraPage_copyBased_binary_halving` restores-and-stops with a multi-row D_min) + the
  `ViewMinimizer.min_rows` per-table-floor infrastructure.
* Opt-in wiring: Algorithms 1–4 + the §F probe after the view minimizer in
  `DisjunctionPipeLine._mutation_pipeline` (when any `mult > 1`), and the assembler in
  `ExtractionPipeLine.extract` after the legacy `eq` is built; `self.mult`,
  `self.alias_aware_min_instance_dict`, `self.cross_alias_predicates`,
  `self.cross_alias_coupled_columns`, `self.projection_alias_attribution`,
  `self.per_alias_filters`, `self.per_alias_pinned_filters`, `self.alias_aware_query`,
  `self.alias_aware_query_verified` threaded onto the pipeline.
* `[feature] multi_instance` flag (`configParser.py`, `constants.py`, the `config*.ini`).
* `test/{Multiplicity,AliasAwareMinimizer,CrossAliasPredicate,PerAliasFilter,PerAliasPinnedFilter,AliasAwareAssembler}Test.py`
  — pure-logic tests (no DB).
* `test/EQC_SJ_workload.sql` + `test/MultiInstancePipelineTest.py` — the EQC+SJ benchmark
  queries (TPC-H Q2/Q17/Q21-flavoured self-join rewrites + synthetic `Sx-*` queries, each
  annotated with what the pipeline should recover) and the live-DB integration-test scaffold
  (skips if no TPC-H PostgreSQL is reachable). Validated against TPC-H SF=0.1 (5 pass, the
  heavy 3-way one skipped); see `docs/multi_instance_HANDOFF.md` for the remaining issues.

Not yet done (the rest of the EQC+SJ framework):

* Make the legacy SPJGAOL extractors (Filter / equi-join / AOA / projection / group-by /
  aggregation / order-by / limit) **natively alias-aware** — so the live D_min can stay at its
  `k` rows and they recover per-alias predicates directly, rather than the current arrangement
  where the minimization phase keeps `k` rows, hands the full set to Algorithms 2–4, and then
  *re-collapses to one row* so those extractors keep working unchanged (§2g). This is the
  remaining structural piece; the rest of §F (and §E.3) presupposes it. **Detailed plan:**
  `docs/multi_instance_extractors_plan.md`.
* Probe the *legacy join* attribution (which alias of `R` the `R.fk = S.x` edge belongs to) —
  currently `R_a1`, exact when `fk` is alias-coupled (the coupled-column chain carries it to
  every alias then); the non-coupled case needs the legacy join graph to be alias-aware (above).
  *(The §F probe itself now handles any `k ≥ 2`, not just `k = 2` — done.)*
* Algorithm 3 extensions — cross-*column* cross-alias predicates (`t_p.a < t_q.b`), and
  cross-alias predicates on non-projected columns (lift Xpose's s-value-bound floating to
  alias pairs).
* Algorithm 4 — **per-alias HAVING** (`HAVING l ≤ AGGR(t_i.x) ≤ u`); composes with
  Xpose/Alaap's aggregate-predicate "diagram" machinery rather than the cardinality probe.
* Validate the benchmark suite end-to-end against a live TPC-H PostgreSQL (the dev env had no
  DB; the integration test's assertions are written against the *intended* behaviour and may
  need tuning on first real run — especially the bisected per-alias bound values and which
  alias the §F probe pins).

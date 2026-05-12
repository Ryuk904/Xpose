# Multi-instance / self-join support — handoff for the next session

This is a continuation doc. The full design rationale is in **`docs/multi_instance.md`** (read it
first — it has §-by-§ explanations of every algorithm). This file is the "what's done, what's
left, what to watch out for" summary.

## 0. Where things are

- **Repo:** the work is entirely inside the `Xpose` submodule at `/home/swan/Desktop/unmasque/Xpose/`
  (the outer repo `/home/swan/Desktop/unmasque` is on `main` and its submodule pointer was *not*
  bumped — bump it with `cd .. && git add Xpose` if you want).
- **Branch:** `selfjoin-multi-instance` (branched off `disjunction-refinement`).
- **Commit:** `ba1b1fc2` — "Add EQC+SJ multi-instance (self-join) support to the extraction pipeline"
  (21 files, +3499 / -7).
- **Pushed to:** the `myfork` remote (`github.com/Ryuk904/Xpose`) and the `claude` remote
  (`github.com/Ryuk904/Xpose_disjunction_claude`). **Not** pushed to `origin` (`ahanapradhan/Xpose`
  — no write access). **No PR opened** (gh CLI isn't authenticated in the dev env; PR link:
  `https://github.com/Ryuk904/Xpose_disjunction_claude/pull/new/selfjoin-multi-instance`).
- **Source:** the research report "Extending UNMASQUE to Self-Joins and Multi-Instance Tables: An
  Algorithmic and Theoretical Framework" (the long doc that started this work — it defines EQC+SJ,
  Algorithms 1–4, §E (per-alias filter/HAVING), §F (alias-lifting + the per-(alias,attribute)
  probe), the boundary cases B1–B4, Lemmas, etc.).

## 1. Hard constraints this work was done under (READ THIS)

- **No live database.** The dev env has no `pandas`/`psycopg2`/PostgreSQL installed and no TPC-H
  data. So **nothing has been run end-to-end.** Everything is `py_compile`-checked, and the pure
  (DB-free) logic of each module was unit-tested by `exec`-ing the module-level functions standalone.
  The DB-touching code (the actual probes, the assembler verification, the minimizer restructure)
  has **never executed**. Assume there are bugs there; the first real task next session (if a DB is
  available) is to run it on TPC-H and the synthetic Sx-* queries.
- **Singleton pattern gotcha:** `src/core/abstract/ExtractorBase.py` `Base.__new__` returns a
  *singleton* shared across all `Base` subclasses (`__init__` re-runs each time). So extractor
  instances must not be used interleaved. The new modules follow the existing convention (each is
  a `Base`/`AppExtractorBase` subclass, fully reconfigured by `__init__`).
- **Postgres ctid gotcha:** an in-place `UPDATE` changes a row's ctid. So the new modules that
  perturb multi-row tables (`cross_alias_predicate`, `per_alias_pinned_filter`) **rebuild the table
  via `TRUNCATE` + bulk `INSERT … VALUES`** instead of updating rows. (`per_alias_filter` uses
  `{A, A-with-c=V}` two-row rebuilds for the same reason.)
- **Transaction isolation:** every DB-perturbing probe runs inside `connectionHelper.begin_transaction()
  … rollback_transaction()` (the `from_clause` EbV pattern), preceded by a `commit_transaction()` to
  lock in earlier stages' work so the rollback doesn't undo them. So the live DB is left exactly as
  found even on failure.
- **Everything is opt-in.** A new config flag `[feature] multi_instance = yes` gates *all* of it.
  With the flag off, behaviour is identical to before (modulo the always-safe minimizer no-crash fix
  in `check_sanity_when_base_exe`, which only changes anything when *neither* halving half is FIT —
  impossible for `mult = 1`). Every new stage is wrapped in `try/except` so a failure never aborts
  extraction; the legacy single-instance query is always the primary result.
- The pure-logic test files (`test/*Test.py`) **import the modules**, which pulls in `pandas` etc.,
  so they only run in an env with deps installed. They cover only the DB-free helper functions.

## 2. What's implemented (file by file)

All in `mysite/unmasque/src/core/` unless noted. Each is gated behind `multi_instance` and wired
into the pipeline (see §3).

### `multiplicity.py` — Algorithm 1 (`MultiplicityDetect`)
Detects `mult(R)` (how many times each core relation appears in the hidden query). Cardinality-
scaling fingerprint: replace `R` with `n` copies of its witness content (`n = 1..kmax+2`), record
`|Q_H|`; under bag semantics it's a polynomial in `n` of degree `mult(R)` (equi-joins → `c·n^k`,
non-strict inequalities → `c·C(n,k)` — same degree). The degree is read off via the **leading
non-zero finite difference** (`_finite_difference_degree`, robust to leading coeff & `n^k` vs
`C(n,k)`). Fallback `_fresh_tuple_probe` (report §B.3): insert one sentinel row, count max
sentinel-cells per output row ÷ `|attrs|`. Outputs: `mult`, `method_used`, `ambiguous`,
`cardinalities`. Boundary cases B1/B4 noted in the docstring (idempotent self-joins, GROUP-BY/
DISTINCT suppression). Pure-tested: `_finite_difference_degree`, `_almost_equal`.
**Now runs *before* the view minimizer** (on the post-Cs2 sampled DB) so `mult` is known early.

### `alias_aware_minimizer.py` — Algorithm 2 (`AliasAwareMinimizer`)
Builds the alias-aware D_min: ≥ `k` diverse rows of `R` on which `Q_H` is still FIT. `kcolour_halve`
(the testable core): start from a pool (legacy D_min row ∪ a bounded original sample), binary-halve
keeping a half that's FIT *and* has ≥ `k` rows, then colour-partition into `k` blocks and greedily
drop rows never below `k`. **Short-circuit added:** if `global_min_instance_dict[tab]` already has
≥ `k` rows (because the floored minimizer left them), use them directly — so post the restructure,
the alias-aware D_min comes from the actual minimization, not a pool sample. Outputs:
`alias_aware_min_instance_dict`, `expanded`, `fallback`. Pure-tested: `kcolour_halve`,
`split_into_k_blocks`, `_flatten`.

### `cross_alias_predicate.py` — Algorithm 3 (`CrossAliasPredicate`)
Two things:
1. **Intra-alias self-equi-joins `t.c = t.c'`** — probed on a 1-row copy of the witness: if it has
   `R.c = R.c'`, changing `R.c` alone makes `Q_H` UNFIT but also setting `R.c' := R.c` makes it FIT
   again ⇒ `t.c = t.c'`.
2. **Same-column inter-alias predicates `t_p.c REL t_q.c`** (`= / < / >` / none) — discriminate
   each column (give the `k` rows distinct ascending values where FIT allows), run `Q_H` once, and
   for two output slots both exposing `c`, read the relationship from which discriminator windows
   their values fall in across all output rows (`infer_inter_alias_predicates`). Columns whose `k`
   values can't be made distinct without breaking `Q_H` → `coupled_columns` (cross-alias equi-join /
   FK shared by aliases / tight const filter).
3. **Projection alias-lift** (`attribute_output_columns`, `output_attribution`): each output column
   whose value is constant across all rows and uniquely equals one discriminator window → attributed
   to `(table, alias_index, source_col)` — tells the assembler which `R_a<j>.c` a projected column
   really is. (Varying-value output columns are alias-symmetric → left unattributed.)
Outputs: `cross_alias_predicates` (list of `{kind:'intra_eq',cols:(c,c')}` or
`{kind:'inter',col:c,op,slots:(sp,sq)}`), `coupled_columns`, `output_attribution`, `notes`.
Pure-tested: `spread_values`, `_window_index`, `infer_inter_alias_predicates`,
`attribute_output_columns`.
**Not done:** cross-*column* cross-alias preds (`t_p.a < t_q.b`); cross-alias preds on non-projected
columns (would need lifting Xpose's s-value-bound-floating in `aoa.py` to alias pairs).

### `per_alias_filter.py` — Algorithm 4, filter part (`PerAliasFilter`)
Per-alias filter bounds (`t1.x ≤ 10 AND t2.x ≤ 20` → recovers *both*, not just the tightest). With
`R = {A, B(V)}` (A = legacy D_min row, B = A with `c := V`), `|Q_H| = C·∏_a(1 + [V ∈ [l_a,u_a]])`
is a step function whose break points (as V moves from `A.c`) are the per-alias bounds; the *size*
of each jump (a ×2 per alias sharing that bound) gives the bound **multiset** (`upper_multiset` /
`lower_multiset` — one entry per alias with a finite bound, tightest first). `find_step_breakpoints`
returns `(low_v, high_v, from_card, to_card)` quads; `_alias_mult` = `round(log2(from/to))` clamped.
Pushes V to the column type's domain limit (`_domain_endpoint`) to detect whether a bound exists,
then bisects. Skips columns Algorithm 3 flagged as coupled. Outputs: `per_alias_filters[tab][col]`
= `{lower, upper, lower_multiset, upper_multiset, tightest, loosest}`. Pure-tested:
`find_step_breakpoints`, `_alias_mult`, `_midpoint`, `_adjacent`, `_add`, type predicates.
**Not done:** per-alias HAVING (`HAVING l ≤ AGGR(t_i.x) ≤ u`) — composes with Xpose/Alaap's
aggregate-predicate "diagram" machinery (`aggregation.py` / Alaap's thesis §6), not this probe.

### `per_alias_pinned_filter.py` — report §F per-(alias,attribute) probe (`PerAliasPinnedFilter`)
Attributes Algorithm 4's bound multiset to *specific* aliases when an inter-alias chain pins them.
v1 = `k = 2` only: find a column `d` with an inter-alias chain covering both aliases (via
`_topo_order_slots` imported from the assembler); discriminate `d` so the chain forces `a1` onto
the smaller-`d` row; for a column `c` with distinct upper bounds `[u_tight, u_loose]`, set `a1`'s
row's `c := u_loose` — FIT ⇒ `a1` owns the loose bound (`a2` the tight), else vice versa. Lower
bounds symmetrically. Output: `pinned_filters[tab] = {alias_index → {col → {lower, upper}}}`.
Reuses `spread_values` / `_is_numeric` from `cross_alias_predicate` and `_topo_order_slots` from
`alias_aware_assembler` (one-way imports, no cycle). No pure-test file (DB-coupled); the reused
helpers are tested elsewhere. **Not done:** `k ≥ 3`; probing which alias the legacy *join*
(`R.fk = S.x`) belongs to.

### `alias_aware_assembler.py` — the alias-aware query assembler (`AliasAwareAssembler` + `build_alias_aware_query`)
Reconstructs the *multi-instance* query from the legacy extracted query string + the Algorithm-1–4
+ §F-probe artifacts (the legacy single-instance query stays primary; this is published alongside).
- `FROM` → `R AS R_a1, …, R AS R_ak` for multi-instance `R`.
- `SELECT` rebound via the projection attribution (`_rewrite_select`): a plain projected column `c`
  pinned to alias `j` → `R_a<j>.c`; a single-column aggregate `f(c)` (`f ≠ COUNT`) → `f(R_a<j>.c)`;
  composite/COUNT/unattributed items → qualified to `R_a1`; `AS`-names and string literals untouched.
- `GROUP BY` / `ORDER BY` (`_qualify_clause`) inherit the alias the column was projected under (from
  `col_attr`) when unambiguous, else `R_a1`.
- Legacy `WHERE` rebound to `R_a1` (`_qualify_text`) — exact when the aliases are symmetric on that
  column (a `t1↔t2` relabel is a no-op then), best-effort otherwise.
- Appended to `WHERE`: intra-alias `R_ai.c = R_ai.c'` (every alias); inter-alias `R_ap.c REL R_aq.c`
  (slots topo-ordered into aliases); "coupled" columns chained `R_a1.c = R_a2.c = … = R_ak.c`; the
  per-alias filter bound multiset (`R_a1` ← tightest via the legacy WHERE, the rest → `R_a2 …` in
  order — or reversed in the `reverse_tails` variant); **OR**, when the §F probe attributed bounds
  for `(tab,col)`, the rebound legacy atoms on that column are *stripped* (`_strip_col_atoms`) and
  the probed bounds emitted with the correct alias identity.
- **Verifier-guided search** (`doActualJob`): build a handful of variants (`probe-attributed` /
  `default` / `bound-tail-reversed`), run each *and* `Q_H` against the original DB
  (`_run_and_compare`), keep the first whose result multiset matches `Q_H` → `verified = True`. If
  none matches, keep the probe/default variant with `verified = False`. `None` = couldn't check
  (no comparable result / > 200k rows / error). `doJob` is called as `doJob(q_h, legacy_query)`.
Outputs: `alias_aware_query`, `verified`, `notes`. Pure-tested: `_split_clauses`, `_qualify_text`,
`_qualify_select`, `_rewrite_select`, `_qualify_clause`, `_topo_order_slots`, `_result_multiset`,
`_strip_col_atoms`, `build_alias_aware_query`.

### Minimizer changes — `src/core/abstract/MinimizerBase.py`, `src/core/view_minimizer.py`
- **No-crash fix:** `check_sanity_when_base_exe` now mirrors the null-free version — takes the upper
  half only if that half keeps `Q_H` FIT, signals stop (`None, None`) when *neither* half does;
  `do_intraPage_copyBased_binary_halving` handles that by restoring the table and stopping (was:
  break the table → sanity check fails → whole pipeline aborts on `… WHERE t1.x < t2.x`). Safe for
  `mult = 1`.
- **Per-table floor:** `ViewMinimizer.min_rows` (`{table → min rows}`, default ⇒ 1 everywhere) +
  `min_rows_for(tabname)`; `do_intraPage_copyBased_binary_halving` stops at `min_rows_for(tabname)`.
  The pipeline sets `vm.min_rows = {R: mult(R)}` (when `multi_instance` is on) so a k-way self-join
  is not collapsed below `k` rows.

### Config / misc
- `[feature] multi_instance` flag — `src/util/constants.py` (`DETECT_MULTI = "multi_instance"`),
  `src/util/configParser.py` (`self.detect_multi_instance`, read with `fallback="no"` so old config
  files don't break), the three `mysite/config*.ini` (`multi_instance = no`).

## 3. Pipeline wiring (where everything is called)

In `mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py::_mutation_pipeline` (the shared
mutation pipeline), the order is now:

```
db_restorer → Cs2 (sampling)
  → [if multi_instance]  MultiplicityDetect            ← Algorithm 1, runs HERE now (on the sampled DB)
  → ViewMinimizer  (with vm.min_rows = {R: mult(R)})   ← floored minimization
  → [if multi_instance] re-collapse each multi-instance table (and its dmin copy) to row 0
        # global_min_instance_dict keeps the k rows; the LIVE tables go back to 1 row so the
        # not-yet-alias-aware Filter/equi_join/aoa/generation extractors keep working unchanged
  → [if multi_instance]
        AliasAwareMinimizer    (Algorithm 2 — short-circuits on the k-row global_min_instance_dict)
        CrossAliasPredicate    (Algorithm 3 + projection attribution)
        PerAliasFilter         (Algorithm 4)
        PerAliasPinnedFilter   (§F probe)
  → Filter → equi_join (U2EquiJoin) → aoa (InequalityPredicate) → ...   ← unchanged legacy stages
```

Then in `mysite/unmasque/src/pipeline/ExtractionPipeLine.py::extract`, after the legacy `eq` is
built (and before `closeConnection`), `_build_alias_aware_query(query, eq)` runs the assembler →
`self.alias_aware_query`, `self.alias_aware_query_verified`, `self.info['ALIAS_AWARE_QUERY']`.

Pipeline fields threaded onto `DisjunctionPipeLine`/`ExtractionPipeLine`:
`self.mult`, `self.alias_aware_min_instance_dict`, `self.cross_alias_predicates`,
`self.cross_alias_coupled_columns`, `self.projection_alias_attribution`, `self.per_alias_filters`,
`self.per_alias_pinned_filters`, `self.alias_aware_query`, `self.alias_aware_query_verified`.
Info blobs: `self.info['MULTIPLICITY' / 'ALIAS_AWARE_DMIN' / 'CROSS_ALIAS_PREDICATES' /
'PER_ALIAS_FILTERS' / 'PER_ALIAS_PINNED_FILTERS' / 'ALIAS_AWARE_QUERY']`.

## 4. Known weak spots / things to verify first (if you get a DB)

1. **The whole DB-touching path is unrun.** Run the pipeline with `multi_instance = yes` on:
   `SELECT * FROM partsupp ps1, partsupp ps2 WHERE ps1.ps_partkey=ps2.ps_partkey AND ps1.ps_supplycost <= ps2.ps_supplycost`
   (Q2-rewrite style), `SELECT l1.l_partkey,l2.l_quantity FROM lineitem l1,lineitem l2 WHERE l1.l_partkey=l2.l_partkey AND l1.l_shipdate < l2.l_shipdate`,
   and synthetic `t1.x ≤ 10 AND t2.x ≤ 20` queries. Check `self.alias_aware_query` and
   `self.alias_aware_query_verified`.
2. **The re-collapse** (`_mutation_pipeline`, right after `vm.doJob`) — it `set search_path`, then
   per multi-instance table: `truncate {fq}; insert {attribs} values ({row0})`, then re-creates the
   dmin table. The `insert_into_tab_attribs_format(attribs, "", fq)` escape-string `""` may be wrong
   for text columns (see how `UN2WhereClause.insert_into_dmin_dict_values` does it — it also passes
   `""`, so probably fine, but verify).
3. **MultiplicityDetect on the Cs2 sample** could be slow on big tables (it inflates the sample to
   `n` copies and runs `Q_H`). For TPC-H SF=0.1 it should be OK. If `use_cs2` is off, the tables
   after the Cs2 stage are the full restored ones → very slow → maybe require `use_cs2` too, or cap.
4. **`min_rows` floor + `do_intraPage`** — when size is `k+1` and floor is `k`, a single halving step
   could go *below* `k` (e.g. size 3, floor 2 → halve → could land on 1 row). The current
   `check_sanity_when_base_exe` doesn't enforce the floor inside the halving — it just signals stop
   when neither half is FIT. So the floor is enforced by the `while int(core_sizes[tabname]) >
   self.min_rows_for(tabname)` loop condition, but a single iteration can overshoot. Probably fine in
   practice (the FIT-guided halving usually can't overshoot far) but worth checking.
5. **`Filter` / `equi_join` / `aoa` with the re-collapsed-to-1-row tables** — should behave exactly
   as in the legacy pipeline (the re-collapse is meant to guarantee this). Verify they don't somehow
   see the dmin tables with `k` rows (the re-collapse re-creates the dmin tables too).
6. **Verification (`_run_and_compare`)** runs `Q_H` against the original DB — could be slow / huge.
   There's a 200k-row cap (`_VERIFY_ROW_CAP`); tune if needed.

## 5. What remains to be implemented (prioritized)

1. **Make the legacy SPJGAOL extractors natively alias-aware** — `Filter`, `equi_join` (`U2EquiJoin`),
   `aoa` (`InequalityPredicate`), `Projection`, `GroupBy`, `Aggregation`, `OrderBy`, `Limit`. Right
   now the minimizer keeps `k` rows for Algorithms 2–4 and then re-collapses to 1 row so these keep
   working unchanged; the report's §F/§E.3 endgame is for these to enumerate over `(alias, attribute)`
   pairs and work on the `k`-row D_min directly. **This is the big remaining structural piece.** It's
   a large rewrite of intricate code (these are 200–600-line files in `src/core/`); needs a live DB
   to validate.
2. **Extend the §F probe past `k = 2`** (`per_alias_pinned_filter.py`) — the chained-bound recovery
   iterating top-down (set the higher-alias rows to their found bounds, binary-search the next one).
3. **Probe the legacy join attribution** — which alias of `R` the `R.fk = S.x` edge belongs to
   (currently `R_a1`; exact when `fk` is alias-coupled, best-effort otherwise).
4. **Algorithm 3 extensions** — cross-*column* cross-alias predicates (`t_p.a < t_q.b`, `a ≠ b`);
   cross-alias predicates on columns `R` doesn't project (lift Xpose's s-value-bound floating in
   `aoa.py` to alias pairs).
5. **Algorithm 4 — per-alias HAVING** (`HAVING l ≤ AGGR(t_i.x) ≤ u`) — compose with Xpose/Alaap's
   aggregate-predicate "diagram" machinery (`aggregation.py`, Alaap's thesis §6).
6. **Validation:** build an EQC+SJ benchmark suite (TPC-H Q2/Q17/Q21 decorrelated-to-self-join
   rewrites + the synthetic `Sx-*` queries from the report's §K) and an integration test that runs
   the probes against a live PostgreSQL + TPC-H setup. **Nothing has been run end-to-end yet.**
7. (Housekeeping) Open the PR on `Ryuk904/Xpose_disjunction_claude` (gh wasn't authenticated:
   `https://github.com/Ryuk904/Xpose_disjunction_claude/pull/new/selfjoin-multi-instance`); update
   Xpose's XFE guideline G6 from "spec" to "verification" role per the report's recommendations.

## 6. Pointers

- Full design: `docs/multi_instance.md` (§2 = Algorithm 1, §2b = Algorithm 2, §2c = Algorithm 3,
  §2d = Algorithm 4, §2e = assembler, §2f = §F probe, §2g = minimizer restructure, §3 = what the
  pipeline stores, §4 = how to enable, §5 = boundary cases, §6 = remaining work).
- Project memory: `~/.claude/projects/-home-swan-Desktop-unmasque/memory/multi-instance-selfjoin-work.md`.
- The report itself (the long markdown doc that started the first session) is the authoritative spec.

# Making the legacy SPJGAOL extractors alias-aware — implementation plan

> Status: **plan only — not implemented.** This is the design for the "big remaining
> structural piece" of the multi-instance work (docs/multi_instance.md §6, item 1).
> Read `docs/multi_instance.md` and `docs/multi_instance_HANDOFF.md` first. This piece
> *needs a live PostgreSQL + TPC-H instance* to develop against — it's a rewrite of
> intricate, well-trodden code (`filter.py`, `equi_join.py`, `aoa.py`, `projection.py`,
> `groupby_clause.py`, `aggregation.py`, `orderby_clause.py`, `limit.py` and the bases
> `un2_where_clause.py`, `filter_holder.py`, `GenerationPipeLineBase.py`) and must not
> regress the single-instance path.

## 0. Why this is the endgame

Today the multi-instance pipeline does a *workaround* (docs/multi_instance.md §2g): the
view minimizer is floored at `mult(R)` so a k-way self-join keeps ≥ k witness rows; the
k-row witness set is captured into `global_min_instance_dict` for Algorithms 2–4; then the
*live* tables are **re-collapsed to one row** so the legacy SPJGAOL extractors keep
working unchanged. The alias-aware query is then *re-assembled* from the legacy
single-instance query string + the Algorithm-1–4 artifacts (`alias_aware_assembler.py`).

Two consequences this plan removes:

1. **Strict self-joins (`t1.x < t2.x`)** have *no* single row on which `Q_H` is FIT, so the
   1-row re-collapse breaks `Q_H`. (As of this session the re-collapse falls back to keeping
   the k-row witness set in that case — see `DisjunctionPipeLine._mutation_pipeline` — but the
   legacy extractors still mutate columns *uniformly across all rows*, so a strict-self-join
   column gets garbage. End-to-end extraction of strict self-joins only works once the
   extractors below are alias-aware.)
2. **Per-alias predicates** (filters, joins, projections, group-by, aggregates, having,
   order-by) are recovered by *bolt-on probes* (Algorithms 3–4, the §F probe) and re-stitched
   by the assembler, rather than fall out of the extractors directly. The assembler is
   best-effort + verifier-checked; native alias-awareness makes it exact by construction.

## 1. The core representation change

Everywhere the legacy code keys things by **`(table, attribute)`** it must instead key by
**`(table, alias_index, attribute)`** (alias_index 1-based; 1 for single-instance tables).
The chokepoints:

- `un2_where_clause.py` — `global_all_attribs`, `attrib_types_dict`, `global_min_instance_dict`
  (the dict's value list is `[header, row]` for one row; becomes `[header, row_1, …, row_k]`),
  `mutate_global_min_instance_dict` (mutates row 0; must take an alias index),
  `insert_into_dmin_dict_values` (truncate + insert *one* row; must insert all k),
  `get_datatype`, `get_dmin_val_of_attrib_list`.
- `filter_holder.py` / `GenerationPipeLineBase.py` — the shared "current D_min" plumbing.
- The SQL helpers in `postgres_queries.py` (`update_tab_attrib_with_value` does
  `UPDATE {tab} SET {attrib}={val}` — *all rows*; need a per-ctid or per-row variant) — or,
  cleaner, keep the "rebuild the table via TRUNCATE + multi-row INSERT" approach the new
  multi-instance modules already use (`cross_alias_predicate._materialize`,
  `per_alias_filter._materialize_two`, `per_alias_pinned_filter._materialize`) and never
  `UPDATE` in place (also dodges the ctid-changes-on-UPDATE gotcha).

Mechanically: the D_min for table `R` with `mult(R)=k` is a `k`-row instance; "alias `a_i`"
is pinned to "row `i`" by the cross-alias predicates Algorithm 3 already recovers (the
discriminator/coupled-column machinery). Mutating "alias `a_i`'s attribute `x`" = rebuild
the table with row `i`'s `x` changed (others untouched). Running `Q_H` and reading whether it
stayed FIT is the same black-box step as today.

## 2. Per-extractor plan

Bring `mult` (+ the alias-aware D_min, the cross-alias predicates, the per-alias filters —
everything `DisjunctionPipeLine` already threads onto `self`) into the SPJGAOL stages.
Recommended order (cheapest / most self-contained first):

1. **`filter.py` (`Filter`) → per-alias filters.** Already prototyped *out of band* by
   `per_alias_filter.py` (Algorithm 4) + `per_alias_pinned_filter.py` (§F). The native version:
   for each `(R, a_i, x)` run the same cardinality-step / FIT-bisection but on the k-row D_min
   directly (vary row `i`'s `x`), so the per-alias bound *and* its alias identity come out in
   one place. Output `filter_predicates` keyed by `(R, a_i, x)`. Then Algorithm 4 / the §F probe
   become redundant for the cases this covers (keep them for `mult=1`-on-the-surface oddities or
   delete). **This one is low risk** — `per_alias_filter.py` already has the bisection logic
   factored & unit-tested (`find_step_breakpoints`, `recover_bound_via_fit_probe`).

2. **`equi_join.py` (`U2EquiJoin`) → which alias each equi-join edge belongs to.** The legacy
   code groups `(table, attrib)` by the *constant they equal* (`algo2_preprocessing`) and finds
   the equi-join graph by mutation. Alias-aware: group by `(table, alias, attrib)`; the
   coupled-column chain Algorithm 3 finds (`R_a1.c = R_a2.c = … = R_ak.c`) is exactly the
   self-equi-join-on-the-key case. The remaining work: the *cross-table* edge `R.fk = S.x`
   when `fk` is **not** alias-coupled (the aliases have different `fk` values) — discriminate
   `R.fk` per alias, perturb `S.x`, see which alias' join breaks. (When `fk` *is* alias-coupled
   the chain already carries the join to every alias — that case is exact today.)

3. **`projection.py` / `groupby_clause.py` / `orderby_clause.py`.** The alias-lift is already
   done out of band (`cross_alias_predicate.attribute_output_columns` →
   `projection_alias_attribution`, consumed by the assembler's `_rewrite_select` /
   `_qualify_clause`). Native version: have `Projection` discriminate the multi-instance table
   while extracting and read which alias each output column exposes (same logic, just *inside*
   the extractor), so the projection / group-by / order-by come out alias-qualified directly.
   Output-column → `(R, a_i, x)` for the columns Algorithm 3 can pin; varying-value columns
   stay alias-symmetric (any alias works).

4. **`aoa.py` (`InequalityPredicate`) → cross-alias inequalities, incl. cross-*column*.**
   Algorithm 3 currently does *same-column* inter-alias predicates (`t_p.c REL t_q.c`) via
   discriminator-window slot inference. `aoa.py` already floats s-value bounds for single-table
   algebraic predicates (`t.a REL c·t.b + d` style); lifting that to alias pairs gives the
   cross-*column* cross-alias case (`t_p.a < t_q.b`). This is the heaviest of the rewrites.

5. **`aggregation.py` → per-alias aggregate-predicate ("diagram") composition + per-alias
   HAVING.** Compose with Alaap's aggregate-predicate "diagram" machinery (`aggregation.py`,
   Alaap's thesis §6) on the k-row D_min: `HAVING l ≤ AGGR(t_i.x) ≤ u` per alias. The §F
   per-alias *filter* probe doesn't reach this; per-alias HAVING is genuinely its own piece.

6. **`limit.py`.** `LIMIT n` is a scalar — alias-orthogonal. Probably no change; just make sure
   it doesn't trip over the k-row D_min (it caps `|Q_H|`, which a k-way self-join inflates —
   the `MultiplicityDetect` fresh-tuple fallback already handles the dual case).

## 3. Pipeline / assembler changes once the above lands

- Drop the **re-collapse to 1 row** (`DisjunctionPipeLine._mutation_pipeline`): keep the k-row
  D_min live. The minimizer floor stays.
- `alias_aware_assembler.py` flips from "re-stitch the single-instance query" to "render the
  alias-aware predicate set the extractors produced". The verifier-guided variant search stays
  (now mostly a confidence check, not a search) — and the `verified` flag stays meaningful.
- The standalone `multiplicity.py` (Algorithm 1) stays (it's the prerequisite); Algorithms 2–4
  + the §F probe can be folded into the extractors (above) or kept as a fallback for the
  cases the extractors don't (yet) cover. Decide per-extractor.
- Update XFE guideline **G6** from "if `Q_S` has a multi-instance table, keep all the table
  instances" (spec role) to a verification role (the report's recommendation): the LLM no longer
  has to *guess* the alias structure — it's extracted; the LLM verifies / polishes.

## 4. Risks & how to de-risk

- **Single-instance regression** is the big one. Mitigation: gate every change behind
  `mult(R) == 1 ⇒ exactly the old code path` (alias_index always 1, k-row D_min has k=1 ⇒ the
  multi-row INSERT degenerates to one row). Run the *whole* existing test suite (`test/*.py`)
  with `multi_instance = no` after every change — it must be byte-for-byte the same behaviour.
- **The k-row D_min must stay FIT through every extractor's mutations.** Add an assertion (under
  a debug flag) after each extractor: `Q_H` still FIT on the live D_min.
- **Probe-call budget.** Per-alias probing is `O(k)` × the single-instance cost. For TPC-H k≤4
  that's fine; cap and log.
- Develop against `EQC_SJ_workload.sql` + `MultiInstancePipelineTest.py` (this session's
  benchmark scaffold) — start with the *non-strict* A/C/D queries (work today), then the
  *strict* B/E queries (need this rewrite), then mixed-multiplicity F, then boundary G.

## 5. Suggested first PR

`Filter` → per-alias (item 2.1) only: smallest, the bisection logic is already factored &
tested, and it immediately makes the A/C/D-class queries' per-alias filters fall out natively
(so the assembler's `_strip_col_atoms` dance + Algorithm 4 + the §F probe become belt-and-braces
rather than load-bearing). Then `equi_join` (2.2), then `projection` (2.3). Leave `aoa` /
`aggregation` for last — they're the deep ones.

# Self-Join / Multi-Instance Table Extraction in UNMASQUE — Approach Report

## 1. The problem

UNMASQUE's hidden-query extractor reconstructs SQL from a black-box `Qh` by
mutating a working database `D¹` and observing how `Qh`'s result changes.
Before this change, the framework assumed every base table in `Qh`'s FROM
clause appeared **at most once**. A query like

```sql
SELECT l1.l_orderkey
FROM   lineitem l1, lineitem l2
WHERE  l1.l_orderkey = l2.l_orderkey
  AND  l1.l_quantity < l2.l_quantity
```

was unrepresentable end-to-end:

1. [from_clause.py](mysite/unmasque/src/core/from_clause.py) returned
   `core_relations: List[str] = ["lineitem"]` — the rename/error trick
   discovered *which* tables Qh touches, never *how many times*.
2. [view_minimizer.py:22](mysite/unmasque/src/core/view_minimizer.py#L22)
   hard-coded `max_row_no = 1`, so `D¹` had exactly one row per table.
   A self-join with an inequality (`l1.q < l2.q`) makes `Qh` empty on a
   one-row `D¹`, and the minimizer raised `ERROR_002` — the framework
   *crashed* on self-joins instead of degrading.
3. Predicate tuples `(tab, attr, op, lb, ub)` collided: two filters that
   should belong to different aliases of the same table fell into the
   same `(tab, attr)` bucket.
4. [QueryStringGenerator.py:396](mysite/unmasque/src/util/QueryStringGenerator.py#L396)
   emitted `", ".join(core_relations)` — bare table names, never alias
   syntax.

## 2. Core idea

**The view-minimizer's halving loop already knows the answer.** It bisects
each base table's row set, keeping the half that preserves Pop. If a
self-join with a distinguishing predicate is present, halving from 2 → 1
will fail (neither half on its own satisfies Qh), and the minimizer's
floor for that table will be `> 1`. That floor *is* the multiplicity
signal — the original code just threw it away by raising an error.

Recovering it costs one localised patch, and once the floor is known, the
rest of the pipeline can be lifted to be alias-aware. Aliases are
**synthetic** (`lineitem__a1`, `lineitem__a2`) — we reconstruct a
*result-equivalent* query, not character-identical to `Qh`.

## 3. The seven-phase pipeline

Each phase is independently testable and a no-op for single-instance
queries (alias == table, predicate tuples unchanged, FROM emitted bare).

### Phase 1 — Floor detection

Files: [view_minimizer.py](mysite/unmasque/src/core/view_minimizer.py),
[MinimizerBase.py](mysite/unmasque/src/core/abstract/MinimizerBase.py),
[DisjunctionPipeLine.py](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py).

- New field `ViewMinimizer.min_card: Dict[str, int]` records the smallest
  row count per table that keeps `Qh` non-empty.
- Halving loops (`do_intraPage_copyBased_binary_halving`,
  `do_interPage_viewBased_binary_halving`) break cleanly when neither
  half preserves Pop, instead of either pressing on with bad ctids or
  raising `ERROR_002`.
- **Floor-state recovery:** when the intra-page loop hits the floor,
  `get_start_and_end_ctids` has already renamed `T → T_unmasque_View_Minimizer`
  in preparation for halving, then bailed out. The post-halving step that
  rebuilds `T` from the working copy never ran, so subsequent `sanity_check`
  fails with `relation "T" does not exist`. The floor-break path now
  explicitly re-creates `T` from the dirty copy and drops the dirty copy,
  restoring the DB to the same shape the non-floor path leaves it in.
- **Latent bug fix:** `MinimizerBase.check_sanity_when_base_exe`
  previously took the upper half blindly without testing it. It now
  tests both halves and returns `None, None` if neither preserves Pop —
  same contract as the nullfree path.
- `DisjunctionPipeLine` exposes `min_card` and logs
  `Multi-instance tables detected (min_card > 1): {'lineitem': 2}`
  when the floor signal fires.

### Phase 2 — Alias data model

File: [util/instance.py](mysite/unmasque/src/util/instance.py) (new).

```python
@dataclass(frozen=True)
class Instance:
    table: str
    alias: str

def build_instances(core_relations, min_card) -> (List[Instance], Dict[str, str]):
    ...
```

For `min_card[T] = 1` the alias equals the table — no behavioural change.
For `min_card[T] = k > 1` the aliases are `T__a1, ..., T__ak`.
`alias_to_table` is the reverse map. Both are threaded onto the
pipeline next to `core_relations`.

### Phase 3 — Multi-row D¹ + ctid-scoped mutation

> **Hard-won lesson — Postgres MVCC moves the ctid on every UPDATE.** A row at
> ctid `(0,1)` updated to `val=X` reappears at `(0,3)`; the next
> `UPDATE … WHERE ctid='(0,1)'` silently matches 0 rows, the revert never
> reverts, and the table corrupts after the first probe. The ctid-scoped
> UPDATE template now appends `RETURNING ctid::text` and every UPDATE goes
> through `_exec_alias_ctid_update` which captures the new ctid and writes it
> back into `global_alias_row_dict[alias]["ctid"]`. Without this the
> multi-instance probe is silently no-op.


Files: [view_minimizer.py](mysite/unmasque/src/core/view_minimizer.py),
[abstract_queries.py](mysite/unmasque/src/util/abstract_queries.py),
[MutationPipeLineBase.py](mysite/unmasque/src/core/abstract/MutationPipeLineBase.py),
[un2_where_clause.py](mysite/unmasque/src/core/abstract/un2_where_clause.py),
[filter_holder.py](mysite/unmasque/src/core/abstract/filter_holder.py),
[filter.py](mysite/unmasque/src/core/filter.py).

After ViewMinimizer settles at the floor, `populate_dict_info` pulls
each table together with its `ctid` and produces:

```python
global_alias_row_dict[alias] = {
    "table": tabname,
    "cols": tuple(col_names),
    "row":  tuple(values),
    "ctid": "(p,r)",
}
```

The i-th physical row backs the i-th alias. New SQL templates emit
`UPDATE T SET col=v WHERE ctid='(p,r)'` so a probe can mutate one
alias's row without touching its siblings. Helpers
`mutate_global_alias_row_dict` and `updateTab_alias_attrib_with_value`
keep the dict and the DB in sync.

The dict is threaded through `MutationPipeLineBase` (optional
parameter, default `None` — keeps every existing call site working
unchanged) into `Filter`, `FilterHolder`, `U2EquiJoin`, and `AOA`.

### Phase 4 — Alias-aware Filter & AOA

Files: [filter.py](mysite/unmasque/src/core/filter.py),
[un2_where_clause.py](mysite/unmasque/src/core/abstract/un2_where_clause.py),
[MutationPipeLineBase.py](mysite/unmasque/src/core/abstract/MutationPipeLineBase.py).

- `Filter.get_filter_predicates` now iterates `self.instances` instead
  of `self.core_relations`. The probe builds `[(alias, attr)]` and
  emits predicate tuples keyed by alias.
- `Filter.checkAttribValueEffect` uses the new ctid-scoped UPDATE
  template when an alias dict entry exists; falls back to the
  whole-table UPDATE for legacy paths.
- `UN2WhereClause.get_datatype`, `mutate_dmin_with_val`, and
  `MutationPipeLineBase.get_dmin_val` route lookups through a new
  `_to_base(name)` helper that resolves an alias to its base table,
  so type/metadata lookups still hit dicts keyed by base table while
  per-row state is keyed by alias.
- AOA inherits the alias-aware mutation automatically (it uses
  `mutate_dmin_with_val`).

### Phase 4 — corollary: alias propagation in FilterHolder

> **Bug found during SJ1 run.** `FilterHolder` (parent of `U2EquiJoin` and
> `InequalityPredicate`) originally only inherited `global_alias_row_dict`
> from the filter_extractor. With `instances` / `alias_to_table` left at
> `None`, AOA's `_to_base('lineitem__a1')` returned the alias verbatim and
> emitted SQL against `unmasque.lineitem__a1` — a table that doesn't exist.
> FilterHolder now propagates all three (`alias_dict`, `instances`,
> `alias_to_table`), and binds the dict by reference (not deep-copy) so
> Filter's ctid refreshes are visible to AOA's mutations.

### Phase 5 — Self-equi-join discovery

File: [equi_join.py](mysite/unmasque/src/core/equi_join.py).

`algo2_preprocessing` already groups predicates by their equi-class
constant. With Phase 4's alias-keyed predicate tuples, a group like
`[(lineitem__a1, l_orderkey), (lineitem__a2, l_orderkey)]` is exactly
a self-equi-join — no new grouping logic needed. The only addition is
a diagnostic log line when two group members resolve to the same base
table, so self-equi-join detection is visible in the run log.

### Phase 6 — Query reassembly with aliases

Files: [QueryStringGenerator.py](mysite/unmasque/src/util/QueryStringGenerator.py),
[ExtractionPipeLine.py](mysite/unmasque/src/pipeline/ExtractionPipeLine.py).

`QueryDetails` carries `instances` and `alias_to_table`.
`formulate_query_string` now emits

```python
parts = [table if alias == table else f"{table} {alias}" for inst in instances]
self.from_op = ", ".join(parts)
```

For single-instance, the output is `lineitem, orders` (identical to
before). For multi-instance, `lineitem lineitem__a1, lineitem lineitem__a2`.
Predicate rendering (`formulate_predicate_from_filter`) already
emits `f"{tab}.{attrib}"` where `tab` is the predicate's first
element — so it transparently emits `lineitem__a1.l_orderkey` once
the predicate tuples carry aliases.

### Phase 7 — Cardinality-scaling verification probe

File: [multiplicity_probe.py](mysite/unmasque/src/core/multiplicity_probe.py) (new).

The floor signal misses cases where an aggregation collapses the
result shape (e.g. `SELECT COUNT(*) FROM T t1, T t2 WHERE ...` —
the aggregate flattens the cartesian product so 1 row suffices for
Pop). Phase 7 adds a post-extraction probe that runs `Qh` and the
extracted `Q_E` on the unmodified DB and compares row counts. A
ratio ≥ 2 with no `min_card > 1` signal is reported as a possible
missed self-join. `min_card > 2` is also surfaced as a warning
since the initial implementation targets `k = 2`.

## 4. Detection — what we can and can't see

| Self-join shape | Detectable? | How |
|---|---|---|
| `l1, l2 WHERE l1.x < l2.x` (inequality) | yes | min_card floor = 2 |
| `l1, l2 WHERE l1.x = 5 AND l2.x = 10` (conflicting constants) | yes | min_card floor = 2 |
| `l1, l2 WHERE l1.x = l2.x` (pure equality, no distinguisher) | **no** | result-equivalent to single-instance |
| `SELECT COUNT(*) FROM T t1, T t2 WHERE t1.x = t2.x` (aggregation) | partial | min_card may stay 1; Phase 7 probe catches via row-count divergence |

The pure-equality case is **mathematically undetectable** from black-box
`Qh` — the simpler single-instance query produces an identical result, so
no probe can distinguish them. We document this and accept the simplified
extraction as semantically correct.

## 5. What stays backwards-compatible

Every change has a `single-instance == legacy behaviour` path:

- `min_card[T] = 1` ⇒ no extra rows kept on D¹ ⇒ `populate_dict_info`
  emits exactly one row per table as before.
- `build_instances` with all `min_card = 1` returns
  `Instance(table=T, alias=T)` ⇒ predicate tuples emit `(T, attr, ...)`
  exactly as today.
- `global_alias_row_dict` is `None` ⇒ ctid-scoped UPDATE path is
  skipped and the whole-table UPDATE path runs.
- `instances=None` ⇒ QSG falls through to `", ".join(core_relations)`.
- All new constructor parameters are keyword-optional, defaulting to
  `None`.

The existing TPC-H test suite (none of which use self-joins) should
produce byte-identical output.

## 6. Known limitations

1. **k > 2 out of scope.** Floor detection gives `≥ 2`, not an exact
   k. The Phase 7 probe reports `min_card[T] ≥ 3` as a warning and
   does not attempt extraction.
2. **Pure-equality self-joins** unrecoverable (see §4).
3. **`global_d_plus_value`** is still keyed by `attrib` alone — for two
   aliases sharing the same column name, the last-mutated value wins.
   Affects gap-aware extraction and string-filter probing on
   multi-instance tables. Single-instance unaffected.
4. **String-filter probes** still use whole-table UPDATE rather than
   ctid-scoped (Filter.run_updateQ_with_temp_str). Multi-instance string
   filters will mutate both aliases together.
5. **NEP** doesn't yet receive the alias dict when it constructs its
   own Filter instance — multi-instance NEP extraction is a follow-up.

## 7. Files changed (summary)

| File | Phase | Role |
|---|---|---|
| [view_minimizer.py](mysite/unmasque/src/core/view_minimizer.py) | 1, 3 | min_card; ctid bookkeeping; alias-row dict |
| [MinimizerBase.py](mysite/unmasque/src/core/abstract/MinimizerBase.py) | 1 | check both halves before halving |
| [DisjunctionPipeLine.py](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py) | 1, 2, 3 | propagate min_card / instances / alias dict |
| [util/instance.py](mysite/unmasque/src/util/instance.py) | 2 | `Instance` + `build_instances` |
| [abstract_queries.py](mysite/unmasque/src/util/abstract_queries.py) | 3 | ctid-scoped UPDATE templates + `select_ctid_star_from` |
| [MutationPipeLineBase.py](mysite/unmasque/src/core/abstract/MutationPipeLineBase.py) | 3, 4 | alias dict; `_to_base`; alias-aware get_dmin_val |
| [un2_where_clause.py](mysite/unmasque/src/core/abstract/un2_where_clause.py) | 3, 4 | per-alias mutation helpers; alias-aware get_datatype |
| [filter_holder.py](mysite/unmasque/src/core/abstract/filter_holder.py) | 3 | inherit alias dict from filter_extractor |
| [filter.py](mysite/unmasque/src/core/filter.py) | 3, 4 | alias-iterating probe; ctid-scoped checkAttribValueEffect |
| [equi_join.py](mysite/unmasque/src/core/equi_join.py) | 5 | self-equi-join diagnostic log |
| [QueryStringGenerator.py](mysite/unmasque/src/util/QueryStringGenerator.py) | 6 | emit `table alias` FROM syntax |
| [ExtractionPipeLine.py](mysite/unmasque/src/pipeline/ExtractionPipeLine.py) | 6, 7 | wire QSG aliases; invoke probe |
| [multiplicity_probe.py](mysite/unmasque/src/core/multiplicity_probe.py) | 7 | Qh vs Q_E cardinality comparison |

## 8. Run results on SJ1 (`SELECT l1.l_orderkey FROM lineitem l1, lineitem l2 WHERE l1.l_orderkey = l2.l_orderkey AND l1.l_quantity < l2.l_quantity`)

**Validated end-to-end (multiple runs against the live TPC-H `tpch` DB):**

- Phase 1 floor detection fires on every run:
  - `Intra-page halving floor for lineitem at size 2`
  - `Multi-instance signal: lineitem requires 2 rows to keep Qh non-empty (likely self-join).`
  - `Multi-instance tables detected (min_card > 1): {'lineitem': 2}`
- Phase 2 alias model produces correct witness rows:
  - `alias_row_dict[lineitem__a1]: table=lineitem ctid=(0,1) row_preview=(3676642, 78373, …, 39.0)`
  - `alias_row_dict[lineitem__a2]: table=lineitem ctid=(0,2) row_preview=(3676642, 13884, …, 29.0)`
  - Both rows correctly share `l_orderkey=3676642` (required for the join) and differ on `l_quantity` (the distinguishing predicate).
- Phase 3 ctid-RETURNING tracking visibly works for numeric columns:
  - `UPDATE … SET l_orderkey=… WHERE ctid='(0,1)' RETURNING ctid::text;` → new ctid (0,3) captured.
  - Sequential probes advance ctid monotonically (0,3 → 0,4 → 0,5 → …) proving the alias dict stays in sync with Postgres MVCC.
- Phase 4 emits per-alias predicates: filter_predicates contains both `('lineitem__a1', 'l_orderkey', …)` and `('lineitem__a2', 'l_orderkey', …)`.

**Open bugs blocking a clean end-to-end extraction (next steps):**

1. **String-column probe bypasses the alias path.**
   `filter.handle_string_filter` → `run_updateQ_with_temp_str` still calls
   `update_tab_attrib_with_quoted_value` (whole-table UPDATE). This mutates
   BOTH alias rows simultaneously and moves their ctids, so the very next
   `_exec_alias_ctid_update` against the cached ctid logs
   `alias ctid UPDATE on lineitem__a1.l_returnflag matched 0 rows at (0,39); row likely moved`.
   The cascade silently leaves wrong values in the second alias's row.
   Fix: route `run_updateQ_with_temp_str` through `_exec_alias_ctid_update`
   when the alias is in the alias dict.
2. **AOA `'<=' not supported between instances of 'str' and 'int'`.**
   `InequalityPredicate.__extract_aoa_core` builds an edge set over `(alias, attr)`
   pairs and compares values across them. With alias-keyed predicates containing
   string columns alongside int columns (because the upstream bug left wrong
   string values in the dict), AOA's edge comparison crashes. Fixing bug #1
   above is a prerequisite — but AOA likely also needs explicit guarding so
   it never compares across mismatched datatypes even when input is messy.
3. **`restore_d_min_from_dict` collapses the table to 1 row.**
   `un2_where_clause.restore_d_min_from_dict` calls `insert_into_dmin_dict_values`
   which TRUNCATES the table and inserts `values[1]` — a single row. For
   multi-instance D¹ this drops alias `__a2`'s witness. Verified by checking
   `unmasque.lineitem` post-run: 1 row, not 2. Fix: when the alias dict is
   populated, insert one row per alias (using the alias dict's rows) instead
   of a single row from `global_min_instance_dict`.

The three bugs are well-localized and independent. Together they correspond
to the gap between "Phase 4 emits per-alias predicates correctly" and a
clean SJ1 round-trip extraction.

## 9. Verification

Test query added to [test_queries.sql](mysite/unmasque/test/disjunction/test_queries.sql)
(SJ1) and to the runnable workload in
[main_cmd.py](mysite/unmasque/src/main_cmd.py) under the same qid.

Expected log lines on a self-join query:

- `Intra-page halving floor for lineitem at size 2`
- `Multi-instance signal: lineitem requires 2 rows to keep Qh non-empty (likely self-join).`
- `Multi-instance tables detected (min_card > 1): {'lineitem': 2}`
- `Self-equi-join candidate on constant <K>: [('lineitem__a1', 'l_orderkey'), ('lineitem__a2', 'l_orderkey')]`

Expected emitted SQL FROM clause: `lineitem lineitem__a1, lineitem lineitem__a2`.

Existing single-instance test queries should produce byte-identical
output to before — the alias machinery is a no-op when `min_card[T] = 1`
for every base table.

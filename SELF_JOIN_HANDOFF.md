# Self-Join Extraction — Handoff for Next Session

> **TL;DR.** I added a 7-phase end-to-end mechanism to extract self-joins
> (multi-instance base tables) in UNMASQUE. Phases 1–4 are validated working
> on the live TPC-H DB; three localized bugs in the downstream pipeline still
> block a clean final extraction. The architecture is sound; what remains is
> targeted bug-fix work, not redesign. Start with bug #1 in §6.

## 1. What was asked

User: *"In unmasque, we don't have a way to find self-join and multi-instance
table (e.g. lineitem l1, lineitem l2). Can you think thoroughly and suggest
a way in order to extract them."* Then: *"Start implementing phase by phase.
We don't need to be character-identical, if it's semantically identical also,
that's good."* Then: *"Create a report of the approach. Second, add
`SELECT l1.l_orderkey FROM lineitem l1, lineitem l2 WHERE l1.l_orderkey = l2.l_orderkey AND l1.l_quantity < l2.l_quantity`
to test_queries.sql and run via main_cmd.py and check whether the code works
or not."*

User-confirmed scope decisions (via AskUserQuestion):
- Full design: all 7 phases.
- Target multiplicity `k = 2` initially; document `k ≥ 3`.
- Semantic equivalence is enough — synthetic aliases (`lineitem__a1`,
  `lineitem__a2`) acceptable.

Repo: `/home/ryuk/Xpose_new`. Branch: `master`. Python venv at `.venv/`,
Python 3.12.3. DB: Postgres 16 on `localhost:5432`, schema `public`,
working schema `unmasque`, dbname `tpch`, user/pass `postgres/postgres`.

## 2. Existing artifacts in the repo (pre-existing, useful context)

- [CLAUDE.md](CLAUDE.md) — project orientation, pipeline stages, config, conventions.
- [GAP_AWARE_HANDOFF.md](GAP_AWARE_HANDOFF.md), [GAP_AWARE_V2_REPORT.md](GAP_AWARE_V2_REPORT.md) — prior work on within-attribute disjunction extraction (`gap_aware` flag). Unrelated to self-join but shares the Filter probe machinery.

## 3. Pre-existing pipeline at a glance (so you know what we're modifying)

Stages execute in this order (entry: `python -m unmasque.src.main_cmd <qid>`
from `/home/ryuk/Xpose_new/mysite/`):

1. `Initialization` (schema, cardinality) — [src/core/initialization.py](mysite/unmasque/src/core/initialization.py)
2. `DbRestorer` — [src/core/db_restorer.py](mysite/unmasque/src/core/db_restorer.py)
3. `Cs2` (correlated sampling, off for SJ1) — [src/core/cs2.py](mysite/unmasque/src/core/cs2.py)
4. `ViewMinimizer` — halves D¹ to **1 row per table** (was hard-coded `max_row_no=1`); the floor *was* discovered by the loop but discarded as `ERROR_002` — [src/core/view_minimizer.py](mysite/unmasque/src/core/view_minimizer.py)
5. `Filter` (constant WHERE predicates) — [src/core/filter.py](mysite/unmasque/src/core/filter.py)
6. `U2EquiJoin` — [src/core/equi_join.py](mysite/unmasque/src/core/equi_join.py)
7. `InequalityPredicate` (AOA) — [src/core/aoa.py](mysite/unmasque/src/core/aoa.py)
8. `Projection`, `GroupBy`, `Aggregation`, `OrderBy`, `Limit`, NEP, render via `QueryStringGenerator` — [src/util/QueryStringGenerator.py](mysite/unmasque/src/util/QueryStringGenerator.py)

Predicate tuple shape **was** `(tab, attr, op, lb, ub)` — collided for two
aliases of the same table.

`global_min_instance_dict[tabname] = [cols_tuple, row_tuple]` — single row,
keyed by base table.

D¹ on disk: exactly one row per base table → any self-join with a
distinguishing predicate makes Qh empty → minimizer raised
`UnmasqueError(ERROR_002)` and crashed.

## 4. The 7-phase implementation (all merged onto master/working tree)

### Phase 1 — Floor detection
**Files:** [view_minimizer.py](mysite/unmasque/src/core/view_minimizer.py), [MinimizerBase.py](mysite/unmasque/src/core/abstract/MinimizerBase.py), [DisjunctionPipeLine.py](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py).

- `ViewMinimizer.min_card: Dict[str, int]` — records smallest cardinality per table that keeps Qh non-empty.
- `do_intraPage_copyBased_binary_halving` breaks cleanly on `get_start_and_end_ctids → None, None`, recreates `T` from the dirty copy (the halving infra left `T` renamed; without the recreation, the post-halving `sanity_check` fails with "relation T does not exist" — this was a real bug I hit on the first run).
- `do_interPage_viewBased_binary_halving` already handled `None`; now logs floor.
- **Latent bug fix:** `MinimizerBase.check_sanity_when_base_exe` previously took the upper half blindly without testing. Now tests both, returns `None, None` if neither preserves Pop (matches the nullfree path contract).
- `DisjunctionPipeLine` exposes `min_card` and logs the multi-instance signal.

### Phase 2 — Alias data model
**Files:** [util/instance.py](mysite/unmasque/src/util/instance.py) (NEW), [DisjunctionPipeLine.py](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py).

```python
@dataclass(frozen=True)
class Instance:
    table: str
    alias: str

def build_instances(core_relations, min_card) -> (List[Instance], Dict[alias→table]):
    ...
```

For `min_card[T]=1`: `alias == table` (no-op). For `k > 1`: synthetic
aliases `f"{table}__a{i}"` (`ALIAS_SEP = "__a"`). `is_synthetic_alias()` helper for callers that need to tell them apart.

### Phase 3 — Multi-row D¹ + ctid-scoped mutation
**Files:** [view_minimizer.py](mysite/unmasque/src/core/view_minimizer.py), [abstract_queries.py](mysite/unmasque/src/util/abstract_queries.py), [MutationPipeLineBase.py](mysite/unmasque/src/core/abstract/MutationPipeLineBase.py), [un2_where_clause.py](mysite/unmasque/src/core/abstract/un2_where_clause.py), [filter_holder.py](mysite/unmasque/src/core/abstract/filter_holder.py), [filter.py](mysite/unmasque/src/core/filter.py).

- `ViewMinimizer.populate_dict_info` also builds `global_alias_row_dict[alias] = {table, cols, row, ctid}` using `select_ctid_star_from`.
- New SQL templates in `abstract_queries.py`:
  - `update_tab_attrib_with_value_at_ctid(tab, attrib, value, ctid)` → `UPDATE tab SET attrib = value WHERE ctid = '…' RETURNING ctid::text;`
  - `update_tab_attrib_with_quoted_value_at_ctid` (quoted)
  - `update_tab_date_attrib_value_at_ctid` (delegate)
  - `select_ctid_star_from(tab)`
- `MutationPipeLineBase.__init__` accepts `global_alias_row_dict`, `instances`, `alias_to_table` (all optional). `_to_base(name)` resolves alias→table or returns name unchanged.
- `UN2WhereClause` adds `mutate_global_alias_row_dict(alias, attr, val)` and `updateTab_alias_attrib_with_value(alias, attr, val, quoted, is_date)`.
- **Key helper** `UN2WhereClause._exec_alias_ctid_update` — centralised path that:
  1. Looks up cached ctid.
  2. Issues `UPDATE … WHERE ctid='Y' RETURNING ctid::text`.
  3. Captures the new ctid from `execute_sql_fetchone_0`.
  4. Writes new ctid back to `global_alias_row_dict[alias]["ctid"]`.
  5. Mutates the in-memory row.
  6. Returns `True` (caller proceeds) or `False` (caller falls back to whole-table UPDATE).
- `FilterHolder` (parent of `U2EquiJoin`, `InequalityPredicate`) inherits all three alias fields from filter_extractor AND **shares the alias dict by reference** (not deep-copy) so probes routed through `filter_extractor.extract_filter_on_attrib_set` and mutations routed through inherited `mutate_dmin_with_val` both see the same live ctids.

### Phase 4 — Alias-aware Filter & AOA
**Files:** [filter.py](mysite/unmasque/src/core/filter.py), [un2_where_clause.py](mysite/unmasque/src/core/abstract/un2_where_clause.py), [MutationPipeLineBase.py](mysite/unmasque/src/core/abstract/MutationPipeLineBase.py).

- `Filter.get_filter_predicates` now iterates `self.instances` (not `self.core_relations`); for each `Instance(table, alias)` probes per-attribute and emits `(alias, attr, op, lb, ub)`.
- `Filter.checkAttribValueEffect` routes through `_exec_alias_ctid_update` (falls back to whole-table when alias dict absent).
- `UN2WhereClause.get_datatype` and `mutate_dmin_with_val` resolve alias→base via `_to_base()` for metadata lookups; `mutate_dmin_with_val` routes through `_exec_alias_ctid_update`.
- `MutationPipeLineBase.get_dmin_val(attrib, tab)` reads from the alias dict if `tab` is an alias (so per-alias witness values are honored).
- AOA inherits the alias-aware mutations transparently via the shared inheritance chain.

### Phase 5 — Self-equi-join discovery
**Files:** [equi_join.py](mysite/unmasque/src/core/equi_join.py).

`algo2_preprocessing` already groups by equi-class constant. With Phase 4
emitting `(alias, attr)` keys, a group like `[(lineitem__a1, l_orderkey), (lineitem__a2, l_orderkey)]`
is naturally a self-equi-join. Added a diagnostic log:
`Self-equi-join candidate on constant K: [...]` when two group members
resolve to the same base table via `_to_base`.

### Phase 6 — Alias-aware FROM emission
**Files:** [QueryStringGenerator.py](mysite/unmasque/src/util/QueryStringGenerator.py), [ExtractionPipeLine.py](mysite/unmasque/src/pipeline/ExtractionPipeLine.py).

- `QueryDetails` carries `instances` and `alias_to_table`.
- `formulate_query_string` emits `T alias` per instance when alias ≠ table; falls back to `", ".join(core_relations)` otherwise.
- ExtractionPipeLine sets `q_generator.instances` and `q_generator.alias_to_table` next to existing `from_clause` wiring.
- Predicate rendering (`formulate_predicate_from_filter`) already emits `f"{tab}.{attrib}"` — works transparently because `tab` is `pred[0]` which is now the alias.
- Smoke-tested: `lineitem lineitem__a1, lineitem lineitem__a2, orders` rendered correctly from a synthetic input.

### Phase 7 — Multiplicity verification probe
**Files:** [multiplicity_probe.py](mysite/unmasque/src/core/multiplicity_probe.py) (NEW), [ExtractionPipeLine.py](mysite/unmasque/src/pipeline/ExtractionPipeLine.py).

Runs after extraction. Compares Qh and Q_E row counts; warns if Qh/Q_E ≥ 2 (possible missed self-join masked by aggregation) or if `min_card[T] ≥ 3` (out of scope). Stores result in `pipeline.info["MULTIPLICITY_PROBE"]`.

## 5. What's been validated on the live DB

**Test query: SJ1**
```sql
SELECT l1.l_orderkey FROM lineitem l1, lineitem l2
WHERE l1.l_orderkey = l2.l_orderkey AND l1.l_quantity < l2.l_quantity;
```

Added to two places (next session: don't add again):
- [mysite/unmasque/test/disjunction/test_queries.sql](mysite/unmasque/test/disjunction/test_queries.sql) (comment block)
- [mysite/unmasque/src/main_cmd.py](mysite/unmasque/src/main_cmd.py) (runnable workload entry, `qid="SJ1"`)

Run with: `cd /home/ryuk/Xpose_new/mysite && ../.venv/bin/python -m unmasque.src.main_cmd SJ1`

I disabled `gap_aware` in `config.ini` once because it triggered a 6M-row
copy and timed out the first attempt. Restored to `yes` at end of session.
**If you need fast iteration, set `gap_aware = no` in config.ini** during
testing. Remember to restore.

**Log file:** `/home/ryuk/Xpose_new/mysite/unmasque.log` — DEBUG level by
default (`config.ini → [logging] level = DEBUG`). Filter by timestamp to
isolate a run.

**Confirmed working on 3 separate runs (timestamps 20:00, 20:08, 20:27, 20:40 on 2026-05-25):**

| Phase | Evidence |
|---|---|
| 1 | `View_Minimizer - INFO: Intra-page halving floor for lineitem at size 2` |
| 1 | `View_Minimizer - INFO: Multi-instance signal: lineitem requires 2 rows to keep Qh non-empty (likely self-join).` |
| 1 | `NEP PipeLine - INFO: Multi-instance tables detected (min_card > 1): {'lineitem': 2}` |
| 2 | `alias_row_dict[lineitem__a1]: table=lineitem ctid=(0,1) row_preview=(3676642, 78373, …, 39.0)` |
| 2 | `alias_row_dict[lineitem__a2]: table=lineitem ctid=(0,2) row_preview=(3676642, 13884, …, 29.0)` |
| 3 | `UPDATE unmasque.lineitem SET l_orderkey = … WHERE ctid = '(0,1)' RETURNING ctid::text;` and ctid monotonically advancing through subsequent probes (0,1 → 0,3 → 0,4 → 0,5 → …) |
| 4 | Final `Filter.filter_predicates`: `[('lineitem__a1', 'l_orderkey', 'range', …), ('lineitem__a2', 'l_orderkey', 'range', …)]` — both aliases present |

**Hard-won discovery during the run — Postgres MVCC ctid instability.**
The first naive ctid-scoped UPDATE silently broke: `UPDATE … WHERE ctid='(0,1)'`
moves the row to `(0,3)` (MVCC); subsequent `WHERE ctid='(0,1)'` matches 0
rows and the revert never reverts. I empirically reproduced this in a
sandbox schema (now dropped). Fix is the `RETURNING ctid::text` plumbing
in Phase 3 above. Critical learning — keep this in mind for any future
multi-row D¹ work.

## 6. Three remaining bugs — start here

All three are localized, independent, and block a clean SJ1 round-trip. The
runtime error you'll see is `Extracted Query: '<=' not supported between
instances of 'str' and 'int'`, which is downstream of bug #1.

### Bug #1 — String-column probe bypasses the alias path (FIX FIRST)

`Filter.handle_string_filter` → `Filter.run_updateQ_with_temp_str` still
calls `connectionHelper.queries.update_tab_attrib_with_quoted_value`
(whole-table UPDATE). This:
1. Mutates BOTH alias rows simultaneously.
2. Moves both rows' ctids (Postgres MVCC).
3. Leaves the cached ctids in `global_alias_row_dict` stale.

Visible cascade in log: `Filter - DEBUG: alias ctid UPDATE on
lineitem__a1.l_returnflag matched 0 rows at (0,39); row likely moved`
followed by every subsequent string column. The fallback (whole-table
UPDATE) then sets BOTH rows to the same value, which collapses the
self-join distinguisher and makes Qh return non-empty when it shouldn't,
producing wrong predicates.

**Fix outline.** Route `run_updateQ_with_temp_str` through `_exec_alias_ctid_update`
when `tabname` (which is now an alias here) is in `self.global_alias_row_dict`.
The helper signature already supports `quoted=True`. Both the probe UPDATE
and the revert UPDATE in `run_updateQ_with_temp_str` need this treatment.

`mysite/unmasque/src/core/filter.py` around line 885 (`run_updateQ_with_temp_str`).

### Bug #2 — AOA `'<=' not supported between str and int`

`InequalityPredicate.__extract_aoa_core` ([aoa.py](mysite/unmasque/src/core/aoa.py))
builds an edge set over `(alias, attr)` pairs and compares values across
edges. With alias-keyed predicates, when the filter predicate list contains
a mix of string and int constants (e.g., `('lineitem__a1', 'l_returnflag', 'equal', 'N', 'N')`
and `('lineitem__a1', 'l_orderkey', '=', 2431303, 2431303)`), AOA's edge
comparator chokes.

Bug #1 likely caused this — once string columns produced wrong (or any)
predicates, AOA's downstream comparator hit the mixed type. Fixing #1 may
make #2 disappear in our test query. But it's also possible AOA needs
explicit guarding so it never compares across mismatched datatypes even
on messy input. Re-test after #1 lands; if AOA still crashes, add a type
check in `__extract_aoa_core`.

### Bug #3 — `restore_d_min_from_dict` truncates D¹ to 1 row

`UN2WhereClause.restore_d_min_from_dict` calls `insert_into_dmin_dict_values(tab)`
which does `TRUNCATE` + `INSERT VALUES(values[1])` — a single row from
`global_min_instance_dict[tab]`. For multi-instance D¹ this drops alias
`__a2`'s witness.

Confirmed empirically: post-run `SELECT * FROM unmasque.lineitem` returns
1 row (should be 2).

**Fix outline.** In `restore_d_min_from_dict`, when `global_alias_row_dict`
has entries for this table's aliases, insert ONE row per alias (using the
alias dict's `row` field) instead of inserting only `values[1]`.

`mysite/unmasque/src/core/abstract/un2_where_clause.py` around lines 58–73.

## 7. Architectural details that aren't obvious from the code

- **Why aliases use `__a` separator (not `_a`).** Some TPC-H attributes
  use `_` as separator. `__a` is unlikely to collide. Defined in
  [util/instance.py](mysite/unmasque/src/util/instance.py) as `ALIAS_SEP`.
- **`global_alias_row_dict` is shared by reference** between Filter and
  FilterHolder (AOA, EquiJoin). Set in FilterHolder.__init__ after the
  base-class deep-copy. This is essential — Filter probes mutate ctids
  during the run; AOA needs to see the latest ctid, not a stale snapshot.
- **`global_d_plus_value`** is keyed by `attrib` only (no tab). For two
  aliases sharing a column name, the last-mutated value wins. Affects
  gap-aware extraction and string-filter probing. Single-instance
  unaffected. Possible follow-up if multi-instance + gap-aware is needed
  simultaneously.
- **`min_card[T] ≥ 3` is out of scope.** Phase 7 probe warns; the rest of
  the pipeline targets `k = 2`. Generalising requires alias generation,
  witness-row labelling, and AOA inequality discovery to handle k > 2,
  which the plan calls a deliberate follow-up project.
- **Pure-equality self-joins** (`l1.x = l2.x` with no distinguishing
  predicate) are mathematically unrecoverable from black-box Qh — they're
  result-equivalent to the single-instance form. Documented limitation.
- **From-clause discovery doesn't know multiplicity.** Phase 1's floor is
  the only signal that says "k > 1." If you ever need pre-floor detection,
  Phase 7's cardinality-scaling probe is the existing fallback skeleton.

## 8. Files changed in this session

| Path | Phase | Role |
|---|---|---|
| [mysite/unmasque/src/core/view_minimizer.py](mysite/unmasque/src/core/view_minimizer.py) | 1, 3 | `min_card`, floor break + recovery, `global_alias_row_dict` populate |
| [mysite/unmasque/src/core/abstract/MinimizerBase.py](mysite/unmasque/src/core/abstract/MinimizerBase.py) | 1 | `check_sanity_when_base_exe` now tests both halves |
| [mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py) | 1, 2, 3, 4 | propagate `min_card`, build instances, propagate alias dict; diagnostic log |
| [mysite/unmasque/src/util/instance.py](mysite/unmasque/src/util/instance.py) | 2 | NEW — `Instance` + `build_instances` + helpers |
| [mysite/unmasque/src/util/abstract_queries.py](mysite/unmasque/src/util/abstract_queries.py) | 3 | ctid-scoped UPDATE templates with RETURNING, `select_ctid_star_from` |
| [mysite/unmasque/src/core/abstract/MutationPipeLineBase.py](mysite/unmasque/src/core/abstract/MutationPipeLineBase.py) | 3, 4 | alias dict, `_to_base`, alias-aware `get_dmin_val` |
| [mysite/unmasque/src/core/abstract/un2_where_clause.py](mysite/unmasque/src/core/abstract/un2_where_clause.py) | 3, 4 | `_exec_alias_ctid_update` helper, alias-aware `get_datatype`, `mutate_dmin_with_val` |
| [mysite/unmasque/src/core/abstract/filter_holder.py](mysite/unmasque/src/core/abstract/filter_holder.py) | 3 | inherits + shares alias dict by reference |
| [mysite/unmasque/src/core/filter.py](mysite/unmasque/src/core/filter.py) | 3, 4 | alias iteration in `get_filter_predicates`; ctid-scoped `checkAttribValueEffect` |
| [mysite/unmasque/src/core/equi_join.py](mysite/unmasque/src/core/equi_join.py) | 5 | self-equi-join diagnostic log |
| [mysite/unmasque/src/util/QueryStringGenerator.py](mysite/unmasque/src/util/QueryStringGenerator.py) | 6 | emit `T alias` syntax |
| [mysite/unmasque/src/pipeline/ExtractionPipeLine.py](mysite/unmasque/src/pipeline/ExtractionPipeLine.py) | 6, 7 | wire QSG instances; invoke multiplicity probe |
| [mysite/unmasque/src/core/multiplicity_probe.py](mysite/unmasque/src/core/multiplicity_probe.py) | 7 | NEW — post-extraction Qh vs Q_E comparison |
| [mysite/unmasque/src/main_cmd.py](mysite/unmasque/src/main_cmd.py) | test | added `SJ1` to runnable workload |
| [mysite/unmasque/test/disjunction/test_queries.sql](mysite/unmasque/test/disjunction/test_queries.sql) | test | added SJ1 as comment for reference |

**Also created (documentation, in repo root):**
- [SELF_JOIN_REPORT.md](SELF_JOIN_REPORT.md) — full approach doc.
- [SELF_JOIN_HANDOFF.md](SELF_JOIN_HANDOFF.md) — this file.
- Plan file at `/home/ryuk/.claude/plans/in-unmasque-we-don-t-optimized-brook.md` — the approved plan from the planning phase.

## 9. How to verify what I'm telling you

```bash
# 1. Confirm the alias model imports and works in isolation
cd /home/ryuk/Xpose_new
.venv/bin/python -c "
import sys; sys.path.insert(0, 'mysite')
from unmasque.src.util.instance import Instance, build_instances
i, a = build_instances(['lineitem','orders'], {'lineitem':2,'orders':1})
print(i); print(a)
"

# 2. Confirm all modified modules still import
.venv/bin/python -c "
import sys; sys.path.insert(0, 'mysite')
from unmasque.src.pipeline.ExtractionPipeLine import ExtractionPipeLine
from unmasque.src.core.multiplicity_probe import MultiplicityProbe
print('OK')
"

# 3. Run SJ1 end-to-end (~3 minutes; ~6M row table so view minimizer is slow)
cd mysite && ../.venv/bin/python -m unmasque.src.main_cmd SJ1

# 4. Inspect log for phase signals
grep -E "Multi-instance|halving floor|alias_row_dict|filter_predicates" mysite/unmasque.log | tail -20
```

DB state expectations:
- `public.lineitem` has 6,001,215 rows (TPC-H scale).
- `unmasque.lineitem` (working copy) may have 1 row post-run due to bug #3.

## 10. Don't forget

- `config.ini` `gap_aware = yes` was on originally; I restored it. Turn off
  for fast SJ1 iteration if needed (avoids a 6M-row full-D copy).
- `psycopg2.execute_sql_fetchone_0` raises a `TypeError` (not
  `psycopg2.ProgrammingError`) when `UPDATE … RETURNING` matches 0 rows
  because `cur.fetchone()` returns `None` and `None[0]` blows up. My
  `_exec_alias_ctid_update` catches `Exception` broadly to handle this.
- Postgres prints two `pandas` warnings on every run (DBAPI2 + Series
  position deprecation). Cosmetic; ignore.
- I empirically confirmed the Postgres MVCC ctid behavior in a sandbox
  (now deleted): `UPDATE … WHERE ctid='(0,1)'` moves the row to `(0,3)`;
  subsequent `WHERE ctid='(0,1)'` matches 0 rows. If you need to
  re-verify, the test command is in the SELF_JOIN_REPORT.md or just run:
  ```sql
  CREATE SCHEMA IF NOT EXISTS t; DROP TABLE IF EXISTS t.foo;
  CREATE TABLE t.foo (id int, val int); INSERT INTO t.foo VALUES (1,100);
  UPDATE t.foo SET val=999 WHERE ctid='(0,1)';
  SELECT ctid, * FROM t.foo;
  DROP SCHEMA t CASCADE;
  ```

## 11. Session 2 update (2026-05-26) — bugs #1 & #3 fixed, three more uncovered

Picked up at §6's bug #1 and walked it down. The string-probe alias fix
removed the SQL syntax errors, but each successive SJ1 run surfaced one
more cascading failure where some code path was still alias-unaware. All
five bugs below are now fixed; SJ1 reaches Projection without crashing
but does not yet emit a clean extracted query (one architectural gap left,
see §12).

### Bug #1 — string-column probe alias path — FIXED
[filter.py:884](mysite/unmasque/src/core/filter.py#L884) `run_updateQ_with_temp_str`
now routes through `_exec_alias_ctid_update(quoted=True)` first, falls back
to whole-table only if the alias dict is absent. Fallback resolves alias→base
via `_to_base`, so even on the fallback path it never targets a synthetic
alias as a relation.

### Bug #3 — multi-alias `restore_d_min_from_dict` — FIXED
[un2_where_clause.py:136](mysite/unmasque/src/core/abstract/un2_where_clause.py#L136)
`restore_d_min_from_dict` now also restores `global_alias_row_dict` from
`_bkp`. [`insert_into_dmin_dict_values`](mysite/unmasque/src/core/abstract/un2_where_clause.py#L148)
inserts one row per alias (sorted, deterministic) and re-anchors each
alias's ctid via `select_ctid_star_from` after the TRUNCATE+INSERT.
[abstract_queries.py:139](mysite/unmasque/src/util/abstract_queries.py#L139)
`select_ctid_star_from` now `ORDER BY ctid` so the i-th SELECT row maps to
the i-th INSERT row deterministically.

### Bug #4 (new, exposed by #1) — `_exec_alias_ctid_update` wrote SQL-formatted vals into the alias dict — FIXED
Symptom: `UPDATE … SET l_returnflag = ''N''` syntax errors after string probes.
Root cause: `mutate_dmin_with_val` passed `get_format(datatype, val)` (e.g.
`"'N'"` for strings) as the SQL value, and `_exec_alias_ctid_update` stored
that same already-quoted string into the alias row. The next probe's
`prev_values` read the quoted form back, the revert re-quoted it
(`get_format('str', "'N'")` → `"''N''"`), and the SQL parser choked.
Fix at [un2_where_clause.py:90](mysite/unmasque/src/core/abstract/un2_where_clause.py#L90):
`_exec_alias_ctid_update` now accepts an optional `raw_val=` and stores it
(separately from the SQL-spliced `val`). [`mutate_dmin_with_val`](mysite/unmasque/src/core/abstract/un2_where_clause.py#L198)
passes `formatted` for SQL and `val` (raw) for the dict.

### Bug #5 (new) — `get_filter_value` binary search ran whole-table UPDATEs against the alias as the relation name — FIXED
Symptom: filter_predicates for non-str columns came back as `range MIN_DOMAIN MAX_DOMAIN`
on every column (binary search never narrowed). Root cause:
[filter.py:261](mysite/unmasque/src/core/filter.py#L261) `get_filter_value` built
a `query_front_set` via `update_sql_query_tab_attribs(tabname, attrib)` where
`tabname` was the alias (`lineitem__a1`). The generated SQL was
`update lineitem__a1 set l_orderkey = …`, which fails silently against a
non-existent relation. Qh ran unmutated every iteration → search exhausted to
domain extremes. Fix: rewrote `get_filter_value` to pass `attrib_list` to a
new `_update_attrib_list_with_val` helper that goes through the alias-ctid
path (with whole-table fallback resolving alias→base). `run_app_with_mid_val`
and `run_app_for_a_val` now take `attrib_list` instead of `query_front_set`.

### Bug #6 (new) — Generation pipeline rebuilt D¹ as `LIMIT 1`, dropping the second alias — FIXED
Symptom: AOA finished cleanly with the equi-join detected, but Projection
crashed immediately with "Result is empty. Cannot identify projections."
Root cause: [GenerationPipeLineBase.do_init](mysite/unmasque/src/core/abstract/GenerationPipeLineBase.py#L57)
called `create_table_as_select_star_from_limit_1` then UPDATEd the single
row to dmin values, throwing away alias #2's witness row. With 1 row, the
self-join joins the row to itself → `l1.l_quantity < l2.l_quantity` is
trivially false → Qh empty before any probe. Fix: `restore_d_min_from_dict`
in GenerationPipeLineBase now TRUNCATE+INSERTs all rows from
`global_min_instance_dict[tab][1:]` when the table is multi-instance
(`len(values) > 2`); single-instance path is unchanged.

### Bug #2 from §6 — did NOT recur
After Bugs #1, #4, #5 landed, the `'<=' not supported between str and int`
error never reappeared. It was downstream of the corrupted filter
predicates produced by the original whole-table fallbacks. No type-guard
added in AOA; the upstream fix removed the trigger.

### Verified signals on SJ1 (single run, 2026-05-26 ~10:53)

| Stage | Evidence |
|---|---|
| View_Minimizer | `Intra-page halving floor for lineitem at size 2`; `min_card={'lineitem': 2}` |
| Filter | predicates: `[('lineitem__a1','l_orderkey','=',5958405,5958405), ('lineitem__a2','l_orderkey','=',5958405,5958405)]` (constants matched the dmin orderkey — equi-join detection input was correct) |
| Equi_Join | `Self-equi-join candidate on constant 5958405: [(lineitem__a1,l_orderkey),(lineitem__a2,l_orderkey)]`; `algebraic_eq_predicates = [[(lineitem__a1,l_orderkey),(lineitem__a2,l_orderkey)]]` |
| Restore | post-AOA `global_alias_row_dict` has both aliases; D¹ has 2 rows |
| Projection | 19 probes run (was 0 in pre-fix runs); per-attribute mutations execute; fails at "find projection dependencies" — see §12 |

## 12. What's still open — cross-alias AOA + alias-aware Projection

These are real and known. They're outside what §6's bug list called out;
the handoff treated AOA as "may need type guarding," but the deeper gap
is structural.

**Cross-alias inequality detection.** AOA's `__isolate_ineq_aoa_preds_per_datatype`
([aoa.py:563](mysite/unmasque/src/core/aoa.py#L563)) only ingests inequality
predicates from `arithmetic_ineq_predicates` (filter output). For SJ1,
`l1.l_quantity < l2.l_quantity` is **not** a filter — Filter never emits
it, AOA never sees it, the edge_set comes out empty. UNMASQUE's existing
AOA detects inequalities **between attributes of the same row**
(`l_quantity < l_extendedprice`); detecting an inequality between **the same
attribute across two alias rows** needs new logic: probe each pair
`(alias_i, attr) < (alias_j, attr)` by mutating one alias's value above/
below the other and checking Pop. This is a feature, not a bug.

**Alias-aware Projection probes.** [GenerationPipeLineBase.update_with_val](mysite/unmasque/src/core/abstract/GenerationPipeLineBase.py#L125)
does a whole-table UPDATE. With both alias rows present, this sets BOTH
to the same value — which collapses any cross-alias inequality and makes
Qh return empty after the first mutated column. Projection then sees
"empty result" on every subsequent probe and bails. Fix needs the same
alias-ctid plumbing as Filter: pass `global_alias_row_dict` into the
Generation pipeline (probably via genCtx) and route `update_with_val`
through `_exec_alias_ctid_update` for a chosen alias (or all aliases
individually).

Both are required for a clean SJ1 round-trip. The architecture is now
right — alias info propagates correctly through Filter, EquiJoin, AOA,
and Restore; the inequality detection and Projection probing are the
remaining feature gaps.

## 13. Files changed in Session 2

| Path | What changed |
|---|---|
| [mysite/unmasque/src/core/filter.py](mysite/unmasque/src/core/filter.py) | Bug #1: `run_updateQ_with_temp_str` alias path. Bug #5: `get_filter_value` / `run_app_with_mid_val` / `run_app_for_a_val` take `attrib_list`; new `_update_attrib_list_with_val` helper. |
| [mysite/unmasque/src/core/abstract/un2_where_clause.py](mysite/unmasque/src/core/abstract/un2_where_clause.py) | Bug #3: `restore_d_min_from_dict` restores alias dict; `insert_into_dmin_dict_values` inserts k rows + ctid re-anchor. Bug #4: `_exec_alias_ctid_update` adds `raw_val`. `mutate_dmin_with_val` passes raw. |
| [mysite/unmasque/src/util/abstract_queries.py](mysite/unmasque/src/util/abstract_queries.py) | `select_ctid_star_from` now `ORDER BY ctid`. |
| [mysite/unmasque/src/core/abstract/GenerationPipeLineBase.py](mysite/unmasque/src/core/abstract/GenerationPipeLineBase.py) | Bug #6: `restore_d_min_from_dict` TRUNCATE+INSERT for multi-instance. |

## 14. Start-here for the next session

1. Cross-alias inequality detection in AOA (§12). Strawman: after Equi_Join,
   for each pair of aliases mapping to the same base table and each common
   attribute, probe one alias's value above/below the other via the ctid
   path and check Pop. Emit `(alias_i, alias_j, '<')` style predicates that
   QueryStringGenerator renders as `alias_i.attr < alias_j.attr`.
2. Alias-aware Projection (§12). Plumb `global_alias_row_dict` into
   GenPipelineContext → GenerationPipeLineBase, and either (a) route
   `update_with_val` through `_exec_alias_ctid_update` per-alias, or
   (b) probe one alias at a time so the other still satisfies the
   inequality.
3. Re-run SJ1 and check that `Extracted Query` renders as
   `SELECT lineitem__a1.l_orderkey FROM lineitem lineitem__a1, lineitem lineitem__a2 WHERE lineitem__a1.l_orderkey = lineitem__a2.l_orderkey AND lineitem__a1.l_quantity < lineitem__a2.l_quantity`
   (semantically equivalent to SJ1 — synthetic aliases are acceptable per the
   user-confirmed scope).

## 15. Session 3 (2026-05-27) — Cardinality probe for pure-equality self-joins

**SJ2** (`SELECT l1.l_orderkey FROM lineitem l1, lineitem l2 WHERE l1.l_orderkey = l2.l_orderkey`)
is the pure-equality self-join case. Before this session it was undetectable:
the Pop-oracle view minimizer halves to 1 row (1 row joined to itself returns
1 row → Qh non-empty → no signal to keep two rows), so all downstream stages
saw a single-instance table and extracted a bag-inequivalent single-instance
query. After this session, SJ2 extracts cleanly:

```
Select lineitem__a1.l_orderkey as l_orderkey
From lineitem lineitem__a1, lineitem lineitem__a2
Where lineitem__a1.l_orderkey = lineitem__a2.l_orderkey;
```

### Design — two-probe detection between minimizer and Filter

[CardinalityProbe](mysite/unmasque/src/core/cardinality_probe.py) runs in
`DisjunctionPipeLine._mutation_pipeline` immediately after `ViewMinimizer`
populates `min_card` / `global_alias_row_dict`, and immediately before
`Filter` is constructed.

For each table with `min_card[T] == 1`:

1. **Cardinality probe (self-join existence)**
   `B_orig = |Qh|`, then `INSERT INTO T SELECT * FROM T;` (duplicates the
   single row), then `B_dup = |Qh|`. If `B_dup / B_orig ∈ [3.5, 4.5]`
   (≈ m² for m=2, k=2), the query is a 2-alias self-join — promote the
   table. Otherwise `DELETE` the duplicate row.

2. **Rename probe (Qh's column-reference set)** — *user's suggestion*
   For each column of the promoted table:
   `BEGIN; ALTER TABLE T RENAME COLUMN X TO X__cp; <run Qh>; ROLLBACK;`
   If Qh fails (Postgres reports the renamed column as "does not exist"),
   X is referenced by Qh. ROLLBACK both clears any aborted-transaction
   state and reverts the ALTER in one shot. Borrowed verbatim from the
   pattern at [from_clause.py:73](mysite/unmasque/src/core/from_clause.py#L73).

3. **Mutation probe (JOIN vs SELECT discrimination)**
   For each Qh-referenced col X, mutate `alias2.X` to an unused dummy
   value via `_exec_alias_ctid_update`, measure `B_mut`. If
   `B_mut < B_dup × 0.9`, X is a join key (mutating it broke the
   equi-join); if not, X is referenced only in SELECT/projection. Revert
   the mutation either way.

4. **Promote in place**
   Update `min_card[T] = 2`; append a duplicate of `row1` to
   `global_min_instance_dict[T]`; rename the existing
   `global_alias_row_dict[T]` entry to `T__a1`, and add `T__a2` with the
   duplicate row's ctid (captured via `INSERT … RETURNING ctid::text`);
   rebuild `instances` and `alias_to_table`. Downstream
   Filter/EquiJoin/AOA constructors read these in their `__init__` and
   see the k=2 state.

5. **Seed predicates**
   For each join key X, emit
   `(T__a1, X, '=', K, K)` and `(T__a2, X, '=', K, K)`. After
   `Filter.doJob` returns, the pipeline appends these to
   `filter_extractor.filter_predicates`. Filter itself emits nothing
   for the join columns because of row-joined-to-itself self-
   satisfaction (`min_present`/`max_present` both True →
   `handle_point_filter` falls through). EquiJoin's
   `algo2_preprocessing` groups the seeded constants by `K`; Phase 5
   self-equi-join detection recognises the same-table alias group and
   `algebraic_eq_predicates` carries the join through to render.

### QueryStringGenerator alias-aware SELECT

A bare `SELECT col` is ambiguous when two FROM aliases of the same base
table both expose `col`. [QSG.__generate_select_clause](mysite/unmasque/src/util/QueryStringGenerator.py)
now consults a new `cols_by_alias` map (derived from
`global_alias_row_dict`) wired in by
[ExtractionPipeLine](mysite/unmasque/src/pipeline/ExtractionPipeLine.py)
to qualify any bare SELECT col that belongs to a synthetic alias. For
SJ2's `l_orderkey` and SJ3's `n_name`, this resolves to `lineitem__a1.l_orderkey`
/ `nation__a1.n_name` automatically.

### Verified results

| Query | Hidden Qh | Extracted Q_E | Status |
|---|---|---|---|
| SJ1 | `SELECT l1.l_orderkey FROM lineitem l1, lineitem l2 WHERE l1.l_orderkey = l2.l_orderkey AND l1.l_quantity < l2.l_quantity` | (Projection bails) | Unchanged — CardinalityProbe no-ops because `min_card[lineitem]=2` already; the cross-alias inequality gap (§12) still blocks |
| SJ2 | `SELECT l1.l_orderkey FROM lineitem l1, lineitem l2 WHERE l1.l_orderkey = l2.l_orderkey` | `SELECT lineitem__a1.l_orderkey FROM lineitem lineitem__a1, lineitem lineitem__a2 WHERE lineitem__a1.l_orderkey = lineitem__a2.l_orderkey` | **CORRECT** — semantically equivalent |
| SJ3 | `SELECT n1.n_name FROM nation n1, nation n2 WHERE n1.n_regionkey = n2.n_nationkey` | `SELECT nation__a1.n_name FROM nation nation__a1, nation nation__a2 WHERE nation__a1.n_nationkey = nation__a1.n_regionkey AND nation__a1.n_nationkey = nation__a1.n_regionkey AND nation__a2.n_nationkey = nation__a2.n_regionkey` | **WRONG SHAPE** — see §16 |

CardinalityProbe log signals for SJ2:
- `CardinalityProbe: lineitem B_orig=1 B_dup=4 ratio=4.00`
- `CardinalityProbe: promoted lineitem to k=2 (aliases lineitem__a1, lineitem__a2; dup_ctid=(0,2))`
- `CardinalityProbe: lineitem qh_cols=['l_orderkey']`
- `CardinalityProbe: lineitem join_keys=['l_orderkey']`
- `Self-equi-join candidate on constant 5958405: [(lineitem__a1,l_orderkey),(lineitem__a2,l_orderkey)]`

CardinalityProbe log signals for SJ3:
- `CardinalityProbe: nation B_orig=1 B_dup=4 ratio=4.00`
- `CardinalityProbe: nation qh_cols=['n_name', 'n_nationkey', 'n_regionkey']` — rename probe finds 3 referenced cols
- `CardinalityProbe: nation join_keys=['n_regionkey', 'n_nationkey']` — mutation probe correctly **excludes** `n_name` (it's a SELECT col, mutation doesn't drop |Qh|)

## 16. Open after session 3 — EquiJoin partition for cross-column self-joins

For SJ3, the cardinality probe correctly identifies both `n_regionkey` and
`n_nationkey` as join keys and seeds four predicates all keyed on the same
constant `K`. EquiJoin groups all four into one group:
`[(a1,n_regionkey), (a2,n_regionkey), (a1,n_nationkey), (a2,n_nationkey)]`.
The existing `algo3_find_eq_joinGraph` / `handle_higher_eq_groups` partitions
this group looking for a satisfying sub-equality, and ends up rendering
**intra-alias** equalities (`a1.n_nationkey = a1.n_regionkey`,
`a2.n_nationkey = a2.n_regionkey`) instead of the **cross-alias** join
(`a1.n_regionkey = a2.n_nationkey`). The duplicate predicate in the output
suggests the partition algo emits two intra-alias groups for `a1` rather than
preferring a cross-alias edge.

The root issue is that EquiJoin's partition heuristic doesn't prefer cross-
alias edges over intra-alias ones. For the multi-instance / self-join case it
should: an intra-alias equality `a1.X = a1.Y` is rarely the actual hidden
predicate, while a cross-alias `a1.X = a2.Y` is the whole reason the table
was promoted to k=2 in the first place.

**Strawman for the next session.** When `handle_higher_eq_groups` processes a
group on a promoted multi-instance table, partition the members by alias and
generate cross-alias edges directly: for `[(a1,X), (a1,Y), (a2,X), (a2,Y)]`
emit edges `(a1,X)=(a2,X)`, `(a1,X)=(a2,Y)`, `(a1,Y)=(a2,X)`, `(a1,Y)=(a2,Y)`
as candidate predicates, verify each via Qh on the multi-row D¹, keep the
ones that hold. This needs `instances` / `alias_to_table` (or just
`_to_base`) plumbed into U2EquiJoin (mostly already there via FilterHolder).

**Workaround now.** SJ2 (same-column self-join) is fully handled. For SJ3
(different-column), the extracted query is wrong-shape but doesn't crash;
multiplicity_probe (§4 phase 7) reports the discrepancy. Run via
`python -m unmasque.src.main_cmd SJ3` to reproduce.

## 17. Files changed in session 3

| Path | What changed |
|---|---|
| [mysite/unmasque/src/core/cardinality_probe.py](mysite/unmasque/src/core/cardinality_probe.py) | **NEW.** `CardinalityProbe(UN2WhereClause)` with `_probe_table`, `_run_rename_probe`, `_mutation_probe_is_join_key`, `_promote_to_k2`. Outputs `seed_filter_predicates`, `promoted_tables`. |
| [mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py) | Insert `CardinalityProbe` between ViewMinimizer and Filter. After `Filter.doJob`, append `probe.seed_filter_predicates` to `filter_extractor.filter_predicates`. |
| [mysite/unmasque/src/util/QueryStringGenerator.py](mysite/unmasque/src/util/QueryStringGenerator.py) | Add `cols_by_alias` field on QueryDetails + setter. `__generate_select_clause` qualifies bare SELECT cols using `_build_alias_for_attr_map` (which prefers `cols_by_alias` first, then falls back to predicate-tagged aliases). |
| [mysite/unmasque/src/pipeline/ExtractionPipeLine.py](mysite/unmasque/src/pipeline/ExtractionPipeLine.py) | Populate `q_generator.cols_by_alias` from `global_alias_row_dict` before render. |
| [mysite/unmasque/src/main_cmd.py](mysite/unmasque/src/main_cmd.py) | Added `SJ2` (pure-equality self-join on lineitem) and `SJ3` (different-column self-join on nation). |

## 18. Start-here for session 4

1. **EquiJoin partition for cross-column self-joins (§16).** Make
   `handle_higher_eq_groups` (or a new path) prefer cross-alias edges for
   multi-instance promoted tables. Verify by re-running SJ3 — expected
   output: `… WHERE n1.n_regionkey = n2.n_nationkey` (and the symmetric
   `n1.n_nationkey = n2.n_regionkey` if EquiJoin can't disambiguate
   direction — both are valid since cross-product equi-join is symmetric).
2. Cross-alias inequality detection (still open, §12). Needed for SJ1 to
   produce a complete extracted query.
3. Alias-aware Projection probes (still open, §12). Needed for SJ1.

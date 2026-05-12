# Work summary — disjunction refinement + environment setup + bug fixes

This document records everything done in one working session: a new feature (the
**disjunction refiner**), the **local environment / TPC-H setup** that was created to run
and test it, and the **pre-existing bugs** that had to be fixed along the way. It's meant
as a hand-off / "what changed and why" reference.

See also:
- `docs/disjunction_refinement.md` — the design write-up + implementation plan + status for the refiner.

---

## 1. The problem that started this

For a numeric attribute, a range predicate is extracted by **binary search over the
attribute's domain** (`filter.py::get_filter_value`, similar searches in `aoa.py`). Binary
search assumes the satisfying values form *one solid block*. For a disjunction they form
*several blocks with gaps*, and a gap that's small relative to the search stride gets
**stepped over** — so a hidden

```sql
WHERE l_quantity between 10 and 20 OR l_quantity between 30 and 40
```

is extracted as the over-approximated

```sql
WHERE l_quantity between 10 and 40
```

and the disjunction loop never recovers the real structure (falsifying `10..40` also kills
the legitimate `30..40` block). The "gap" `21..29` is silently swallowed.

Goal: after extraction, **no gap is ever missed**, while staying strictly black-box (the
tool may only *run* the hidden query, never parse it) and without brute-force scanning of
attribute domains.

---

## 2. The approach (the "disjunction refiner")

Full write-up in `docs/disjunction_refinement.md`. In short:

- **Key idea** — instead of *searching* for gap values, let the database point at them: run
  the hidden query `Q_H` and the current extracted query `Q_E` on the database and compare.
  If `Q_E` is too loose, the extra rows are witnesses; their attribute values sit inside a
  gap. This is the NEP idea turned into a *repair loop* run after the rest of the pipeline.
- **Assumption** — every value that matters to the predicate has a *signature* row in the
  database (otherwise no black-box method could observe the gap at all). Under this
  assumption, "`Q_E` ≡ `Q_H` on the database" ⇔ "no gap missed".
- **Algorithm (implemented)** — after the pipeline produces `Q_E`:
  1. Compare `Q_H` vs `Q_E` on the (full) database. If they match → done, no work.
  2. Otherwise, for every numeric `range`/`IN` atom (and every `LIKE` atom) of `Q_E`:
     enumerate the *distinct data values that atom currently keeps*, probe each against the
     hidden query using the single-tuple `D_min` mutation oracle (re-using `Filter`'s
     helpers); the values the hidden query rejects mark gaps. Each original interval is
     processed *separately* so original gaps are never bridged; gap edges are sharpened with
     `Filter.get_filter_value`.
  3. Rebuild `Q_E` from the refined predicates and repeat (bounded number of rounds).
- **Safety** — the refiner can only ever *tighten* `Q_E`, and it keeps the refined query
  *only if* it now matches `Q_H` exactly — otherwise it returns the query it was given. The
  whole thing is wrapped in `try/except`. So it can never regress an extraction that already
  worked.
- **Flag** — no new flag; it's gated by the **existing `[feature] or` flag**
  (`config.detect_or`) — the same flag that already turns on the disjunction loop. In
  `main_cmd.py` that's the `orf` field of a `TestQuery`.

---

## 3. Files changed / added (the feature)

| File | Change |
| --- | --- |
| `mysite/unmasque/src/core/disjunction_refiner.py` | **New.** `DisjunctionRefiner` class + `make_filter_for_refiner` helper. Uses `NepComparator` for the `Q_H` vs `Q_E` check (so it works per-arm for union queries, where other relations are nullified in the working schema), and a `Filter` extractor for the `D_min` mutation-oracle probes. |
| `mysite/unmasque/src/util/QueryStringGenerator.py` | Added `replace_range_with_intervals(tab, attrib, intervals)` (single interval → `range` atom; ≥2 → an `IN` atom that QSG already renders as `between … OR between …`), `add_not_in_predicate(tab, attrib, values)` (for string leaks), `rebuild_after_predicate_change()` (regenerate only the WHERE clause and re-assemble). |
| `mysite/unmasque/src/core/elapsed_time.py` | Added a `t_refinement` / `app_refinement` time-profile bucket + `update_for_refinement()`; appears as "Disjunction Refinement" in the time profile table. |
| `mysite/unmasque/src/pipeline/ExtractionPipeLine.py` | Renamed the body of `_after_from_clause_extract` to `_extract_spjgaol`; new `_after_from_clause_extract` = `_extract_spjgaol` then `_refine_disjunctions`. `_refine_disjunctions` is gated on `config.detect_or` and is a no-op otherwise; on any error it logs and returns the unrefined query. |
| `mysite/unmasque/src/pipeline/OuterJoinPipeLine.py` | `_after_from_clause_extract` now runs `_extract_spjgaol` → outer-join extraction → `_refine_disjunctions` (so refinement sees the fully assembled query, including any `ON`-clause filters). It no longer routes through the base wrapper. |
| `mysite/unmasque/src/pipeline/UnionPipeLine.py` | Unchanged. Per-arm refinement happens for free because `extract()` calls `_after_from_clause_extract` per `UNION ALL` arm with the other relations nullified, which makes the per-arm `Q_H` vs `Q_E` diff valid. |
| `mysite/unmasque/test/DisjunctionRefinementTest.py` | **New.** End-to-end tests (need a live TPC-H DB). Uses small tables (`nation` / `region`) so it runs in seconds; the big-table case is covered by the `DISJ2` query in `main_cmd.py`. |
| `docs/disjunction_refinement.md` | **New.** Design write-up + implementation plan + status. |
| `mysite/unmasque/src/main_cmd.py` | Added three sample queries to `create_workload()`: `DISJ0` (control, no disjunction), `DISJ1` (`nation` disjunction, projects the disjunction column), `DISJ2` (`lineitem` `l_quantity` disjunction — the motivating example). |

### Deferred (v2, noted in the design doc)
- over-loose `<=` / `>=` half-line atoms (only two-sided `range` atoms are refined today);
- standalone `<>` (left to `aoa.py` / NEP; `<>` mixed with a range is already handled — it shows up as a width-one gap);
- sharper text repair (today: `… NOT IN (leaking strings)`; later: a tighter `LIKE` or a split);
- over-loose filters that live inside an outer-join `ON` clause (the refiner currently can only verify-and-keep-or-revert there);
- "Part 1" — a cheap gap-aware boundary search inside `filter.py` / `aoa.py` to reduce how many refinement rounds are needed (Part 2 alone is correct).

---

## 4. Pre-existing bugs found and fixed (not the refiner)

These were hit while getting the pipeline to actually run; they are unrelated to the
disjunction refiner but had to be fixed to demo it.

1. **numpy version** — with `numpy 2.4.x`, `projection.py` crashes with
   `'ImmutableDenseNDimArray' object has no attribute 'getO'` (a 1-element numpy array
   multiplied by a sympy symbol becomes a sympy `NDimArray`, which then reaches code
   expecting an `Expr`). Fix: use `numpy==2.1.2`, exactly as `requirements.txt` already
   pins. (`sympy==1.4`, also pinned, is fine.)

2. **`mysite/unmasque/src/core/projection.py`** — `__assign_next_s_value_for_or_attrib`
   had a typo (`if in_vals not in used_vals` instead of `if val not in used_vals`) and,
   worse, returned an OR/IN alternative *as-is*; for a disjunctive range that alternative is
   an interval **tuple** `(1, 15)`, which then got assigned into a numpy float matrix →
   `ValueError: setting an array element with a sequence` whenever an **integer column that
   also carries an OR/IN filter is projected**. Fix: flatten the alternatives (interval
   tuples → their `lb`/`ub`; scalars → themselves) into candidate **scalar** values and pick
   one not used yet; fix the typo; and as a belt-and-suspenders, `__assign_s_val_in_coeffMatrix`
   now coerces any list/tuple s-value to a scalar (and `None` → the d_min value). No-op for
   ordinary scalar s-values, so no regression for normal queries.

3. **`mysite/unmasque/src/core/orderby_clause.py`** — the same root cause surfaced one stage
   downstream once projection stopped crashing: `get_non_text_attrib` did
   `first = filter_attrib_dict[key][0]` then `first + 1`, but for an OR/IN column
   `filter_attrib_dict[key]` is a list of interval tuples, so `first` was `(1, 15)` →
   `TypeError: can only concatenate tuple (not "int") to tuple`. Fix: reduce the OR/IN
   filter value to scalar bounds inside one satisfying interval before using them. Again a
   no-op for the normal `(lb, ub)` scalar-pair case.

---

## 5. Local environment that was created (TPC-H was not set up)

There was no PostgreSQL and no Python deps on the machine. Set up:

- **Conda env `unmasque`** at `/home/swan/miniforge3/envs/unmasque` — Python 3.11,
  **PostgreSQL 18.3** (userspace, conda-forge), and the Python deps from `requirements.txt`
  (notably `numpy==2.1.2`, `sympy==1.4`, `Django==4.2.4`, `psycopg2-binary`, `pandas`,
  `tabulate`, `frozenlist`, `python-dateutil`, `sqlparse`, `oracledb`).
  - Activate it before doing anything: `conda activate unmasque` (after
    `source /home/swan/miniforge3/etc/profile.d/conda.sh`).
- **PostgreSQL cluster** — data dir `/home/swan/pgdata`, listening on `localhost:5432`,
  superuser role `postgres` with password `postgres` (matches `config.ini`).
  - Start: `export PGDATA=/home/swan/pgdata; pg_ctl -D $PGDATA -l $PGDATA/server.log -w start`
  - Status / stop: `pg_ctl -D /home/swan/pgdata status` / `pg_ctl -D /home/swan/pgdata stop`
  - The cluster has `fsync=off` / `synchronous_commit=off` (it's a throwaway dev cluster).
- **`tpch` database** — loaded the repo's own `mysite/unmasque/test/experiments/data/tpch_tiny.zip`
  (TPC-H sf=0.1: lineitem 600 572 rows, orders 150 000, customer 15 000, partsupp 80 000,
  part 20 000, supplier 1 000, nation 25, region 5) into the `public` schema. The `unmasque`
  working schema is created by the pipeline itself (`Initiator` / `TpchSanitizer`).
  - The CSVs are also extracted at `/home/swan/tpch_tiny/` (≈108 MB).
  - To rebuild from scratch: `DROP DATABASE tpch; CREATE DATABASE tpch;`, recreate the 8
    tables from `tpch_tiny/schema.sql`, then `\copy <tab> FROM '<tab>.csv' WITH (FORMAT csv,
    HEADER true, DELIMITER '|', QUOTE '"')` for each.
- **Config / repo tweaks needed for the tiny data:**
  - `mysite/config.ini` — emptied the `[table_sizes]` section (it hard-codes sf=1 sizes like
    `lineitem:6001215`, which would break against sf=0.1 data — now the tool reads the real
    row counts) and set `level = INFO` (DEBUG is extremely verbose). Logs go to
    `mysite/unmasque.log`.
  - `mysite/pkfkrelations.csv` — removed the `supplier1` row (that table isn't in the tiny
    dataset; leaving it makes the Initiator try to back up a non-existent table).

---

## 6. How to run things

All from the `unmasque` conda env, with the server running, from the `mysite/` directory.

**A single workload query** (relative imports require `-m`):
```
cd mysite
python -m unmasque.src.main_cmd DISJ1     # or DISJ0 / DISJ2 / any qid in create_workload()
```
`main_cmd.py` reads `mysite/config.ini`; each `TestQuery`'s flags (`cs2, union, oj, nep, orf`)
override the config for that run (`orf=True` ⇒ `config.detect_or = True` ⇒ disjunction loop +
refiner).

**The disjunction-refinement test suite:**
```
cd mysite
python -m unmasque.test.DisjunctionRefinementTest
```

**Note on a log line that looks scary but is normal:** during from-clause extraction the
tool renames relations to probe which ones are "core", so you'll see e.g.
`relation "nation" does not exist` in `unmasque.log` — it's caught and handled; successful
runs still log it.

---

## 7. Verification results

- **`main_cmd.py` queries** (live TPC-H, sf=0.1):
  - `DISJ0` — `select n_name from nation where n_nationkey between 5 and 18;` →
    `Where nation.n_nationkey between 5 and 18` — correct (control; `detect_or` off, refiner not run).
  - `DISJ1` — `select n_nationkey, n_name from nation where n_nationkey between 1 and 5 or n_nationkey between 10 and 15;` →
    `Where (nation.n_nationkey between 1 and 5 OR nation.n_nationkey between 10 and 15)` — exact; gap `6..9` not swallowed; the disjunction column is also *projected* (exercises the fixed projection path). `verify_correctness` ⇒ "Extracted Query is Correct."
  - `DISJ2` — `select l_shipmode from lineitem where l_quantity between 10 and 20 or l_quantity between 30 and 40;` →
    `Where (lineitem.l_quantity between 10.00 and 20.00 OR lineitem.l_quantity between 30.00 and 40.00)` — exact; gap `21..29` not swallowed; "Extracted Query is Correct." (≈60 s — the refiner re-restores the 600 K-row table per comparison round.)
- **`DisjunctionRefinementTest.py`** — `Ran 5 tests in ~10 s — OK`; "Extracted Query is Correct." for all 5:

  | Test | Hidden WHERE | Extracted WHERE |
  | --- | --- | --- |
  | `test_two_ranges_one_attribute` (projects the OR column) | `n_nationkey between 1 and 5 or between 10 and 15` | `(n_nationkey between 1 and 5 OR n_nationkey between 10 and 15)` |
  | `test_three_ranges_one_attribute` | `… 0..3 or 8..11 or 18..21` | `(n_nationkey between 0 and 3 OR … 8 and 11 OR … 18 and 21)` |
  | `test_not_equal_inside_range` | `n_nationkey between 5 and 18 and n_nationkey <> 10` | `(n_nationkey between 5 and 9 OR n_nationkey between 11 and 18)` |
  | `test_gaps_on_two_attributes` | disjunction on `n_nationkey` *and* `n_regionkey` | both rendered as exact disjunctions |
  | `test_plain_conjunctive_no_disjunction` (regression) | `n_nationkey between 5 and 18 and n_regionkey >= 1` | unchanged — refiner is a no-op |

---

## 8. Open caveats / things a reviewer should know

- The refiner's `Q_H`-vs-`Q_E` comparison restores the *full* table each round; on a large
  table that's the dominant cost (≈60 s for sf=0.1 `lineitem`). It's bounded by roughly
  *(number of gaps + 1)* rounds, and it does nothing at all when there's no discrepancy.
- `disjunction_refiner.py` has `MAX_DATA_VALUES = 15000`: if a believed range holds more
  distinct candidate data values than that, that atom is skipped (status quo, no regression)
  and a line is logged. (For TPC-H the relevant columns are tiny, so it never fires.)
- The `numpy==2.1.2` / `sympy==1.4` pins in `requirements.txt` matter — newer numpy breaks
  `projection.py`. The conda env was built to those pins.
- The `mysite/config.ini` and `mysite/pkfkrelations.csv` edits are specific to running
  against the sf=0.1 sample data; revert (restore the `[table_sizes]` section, re-add the
  `supplier1` row) if pointing the tool at the real sf=1 TPC-H.
- The PostgreSQL cluster at `/home/swan/pgdata` and the conda env `unmasque` were created in
  this session; they persist on the machine.

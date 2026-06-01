# CLAUDE.md — Xpose_new

Notes for future Claude sessions working in this repo. Keep this file terse.

## Purpose

Xpose is a hidden-query extraction framework (UNMASQUE-style). Given a black-box `Qh`
that can be executed against a database, it reconstructs the SQL by mutating the DB,
observing `Pop`/result-shape changes, and assembling SQL fragments stage by stage.

## Entry points

- **CLI:** `python -m unmasque.src.main_cmd` (from [mysite/](mysite/))
- **GUI:** `python3 manage.py runserver` (from [mysite/unmasque/](mysite/unmasque/)), then `http://localhost:8080/unmasque/`
- **Sample queries:** [mysite/unmasque/test/util/queries.py](mysite/unmasque/test/util/queries.py)
- **Disjunction test queries:** [mysite/unmasque/test/disjunction/test_queries.sql](mysite/unmasque/test/disjunction/test_queries.sql)

## Config

- [mysite/config.ini](mysite/config.ini) — DB credentials (`[database]`), feature flags (`[feature]`: `union`, `nep`, `cs2`, `or`, `outer_join`, `gap_aware`), logging level (`DEBUG`/`INFO`/`ERROR`), `[options]`, `[table_sizes]`.
- [mysite/pkfkrelations.csv](mysite/pkfkrelations.csv) — PK/FK relationships for the TPC-H schema. Replace for other schemas.
- Config parsed by [mysite/unmasque/src/util/configParser.py](mysite/unmasque/src/util/configParser.py).

## Pipeline stages (in execution order)

| Stage | Module |
|---|---|
| Initialization (schema, cardinality) | [src/core/initialization.py](mysite/unmasque/src/core/initialization.py) |
| DB restore | [src/core/db_restorer.py](mysite/unmasque/src/core/db_restorer.py) |
| Correlated sampling (Cs2) | [src/core/cs2.py](mysite/unmasque/src/core/cs2.py) |
| View minimization → single-row D¹ | [src/core/view_minimizer.py](mysite/unmasque/src/core/view_minimizer.py) |
| From / Join | [src/core/from_clause.py](mysite/unmasque/src/core/from_clause.py), [src/core/equi_join.py](mysite/unmasque/src/core/equi_join.py) |
| Filter (WHERE constants) | [src/core/filter.py](mysite/unmasque/src/core/filter.py) |
| AOA (inequality predicates) | [src/core/aoa.py](mysite/unmasque/src/core/aoa.py) |
| Projection | [src/core/projection.py](mysite/unmasque/src/core/projection.py) |
| Aggregation, GROUP BY / HAVING | [src/core/aggregation.py](mysite/unmasque/src/core/aggregation.py) |
| ORDER BY | [src/core/orderby_clause.py](mysite/unmasque/src/core/orderby_clause.py) |
| LIMIT | [src/core/limit.py](mysite/unmasque/src/core/limit.py) |
| Disjunction orchestrator (cross-predicate OR via falsify-and-rerun) | [src/pipeline/fragments/DisjunctionPipeLine.py](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py) — loop at [:191](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py#L191) |

## Filter internals

- Predicate tuple shape: `(tab, attr, op, lb, ub)`.
- Operators: `'='`, `'<='`, `'>='`, `'range'`, `'equal'`, `'LIKE'`.
- Per-attribute binary search drives endpoint discovery: [`get_filter_value`](mysite/unmasque/src/core/filter.py#L219).
- D¹ mutation/revert: [`checkAttribValueEffect`](mysite/unmasque/src/core/filter.py#L154) UPDATEs a column, runs the query, reverts.
- **Within-attribute OR-of-intervals** (e.g. `A∈[10,20] ∨ A∈[30,40]`) is handled via gap-aware extraction when `gap_aware = yes` is set in config. Off by default. Resolution chain inside [`_refine_with_gap_search`](mysite/unmasque/src/core/filter.py):
  1. **v2 NEP-style witness** ([`_refine_by_nep_witness`](mysite/unmasque/src/core/filter.py) → [`GapWitnessFinder`](mysite/unmasque/src/core/gap_witness.py)): swap working `<tab>` to full D from user_schema, build `Re = SELECT <Qh_cols> FROM <from> WHERE <intervals_so_far>` with Qh's projection so `Re EXCEPT ALL Rh` (Comparator pattern, `r_h LIKE r_e`) is meaningful, ctid-bisect `<tab>` to a single witness row, read A directly from the base-table row. Then bisect outward (`_binsearch_first_sat`/`_last_sat`) for gap edges. Pop-precheck guards against false witnesses (witness gap may belong to a different attribute).
  2. **v1 data-witness sampling** ([`_refine_by_data_witness`](mysite/unmasque/src/core/filter.py)): samples distinct A-values from user_schema and probes each via Pop oracle on D¹. Used when v2 bails (Qh has aggregate/expression projection, full-D swap fails, etc.).
  3. **v1 speculative bisection** ([`_find_gaps_recursive`](mysite/unmasque/src/core/filter.py)): depth-bounded midpoint search.
- **Cross-attribute OR** (e.g. `A=x OR B=y`) is handled by [`DisjunctionPipeLine`](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py)'s falsify-and-rerun loop (gated by `or = yes`).
- **Dead code:** `handle_filter_for_subrange` at [filter.py:110-143](mysite/unmasque/src/core/filter.py#L110) is unreachable from the main flow (`get_filter_predicates → extract_filter_on_attrib_set → handle_filter_for_nonTextTypes → handle_point_filter | handle_precision_filter`). Do not pattern off it.

## DB helpers

- Connection abstraction: [src/core/abstract/abstractConnection.py](mysite/unmasque/src/core/abstract/abstractConnection.py).
- Postgres impl: [src/core/abstract/PostgresConnectionHelper.py](mysite/unmasque/src/core/abstract/PostgresConnectionHelper.py).
- SQL templates: [src/core/postgres_queries.py](mysite/unmasque/src/core/postgres_queries.py).
- Re/Rh diff (full materialization via `EXCEPT ALL`): [src/core/abstract/Comparator.py](mysite/unmasque/src/core/abstract/Comparator.py).
- Pop check: `self.app.isQ_result_nonEmpty_nullfree(result)` defined in [src/core/abstract/nullfree_executable.py](mysite/unmasque/src/core/abstract/nullfree_executable.py).
- App execution: `self.app.doJob(query)` returns a result rows list.

## Tests

- Test root: [mysite/unmasque/test/](mysite/unmasque/test/).
- Per-stage: `FilterTest.py`, `WhereClauseTest.py`, `JoinTest.py`, `AggregationTest.py`, etc.
- Disjunction queries (DQ1–DQ9): [test/disjunction/test_queries.sql](mysite/unmasque/test/disjunction/test_queries.sql).
- Run a single test file with `python -m unittest mysite.unmasque.test.<module>` from the repo root (verify locally if needed).

## Conventions

- All `core/` modules inherit a `self.logger` from `ExtractorBase`; honor `config.ini` `[logging] level`.
- Predicate lists are read by `aoa.py`, `equi_join.py`, and the query-reassembly path. When changing predicate shape semantics (e.g. allowing multiple range tuples on the same `(tab, attr)` to mean OR), audit those consumers.

## Quick orientation for common tasks

- **Add a feature flag:** add line in [config.ini](mysite/config.ini) `[feature]`, then add a property in [configParser.py](mysite/unmasque/src/util/configParser.py), then read via `self.connectionHelper.config.<flag>`.
- **Add a SQL template:** put it in [postgres_queries.py](mysite/unmasque/src/core/postgres_queries.py) and call via `self.connectionHelper.queries.<method>(...)`.
- **Probe Pop on D¹ with a mutated value:** use `self.checkAttribValueEffect(query, val, [(tab, attr)])` — it UPDATEs, runs the query, reverts, and returns a bool.

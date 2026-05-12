# Extracting Disjunctive Range Predicates Correctly (No Missed Gaps)

> Status: a first version (Part 2) is implemented - see `mysite/unmasque/src/core/disjunction_refiner.py`
> and the "Implementation status" section at the bottom. It still needs a test run against a live
> PostgreSQL+TPC-H setup.
> Audience: anyone working on the UNMASQUE / XPOSE extraction pipeline.

---

## 1. The problem in one example

Hidden query:

```sql
SELECT ... FROM lineitem WHERE l_quantity BETWEEN 10 AND 20 OR l_quantity BETWEEN 30 AND 40
```

What UNMASQUE extracts today:

```sql
SELECT ... FROM lineitem WHERE l_quantity BETWEEN 10 AND 40
```

The extracted query is **wrong**: it also accepts rows with `l_quantity` between 21 and 29.
We call `21..29` a **gap** — a stretch of values that the hidden query rejects but the extracted query accepts.

The same thing can happen:

* with several attributes at once (`a BETWEEN 1 AND 5 OR a BETWEEN 8 AND 9 ... AND b BETWEEN ...`),
* with several gaps in one attribute (`a IN [1,5] ∪ [8,9] ∪ [20,25]`),
* with the `<>` / `!=` operator (`a <> 7` is just a "gap" of width one at value `7`),
* with **text columns** (`name LIKE 'A%' OR name LIKE 'C%'` extracted as something looser).

We want: **after extraction, no gap is ever missed.**

UNMASQUE is a strictly **black-box** tool — it may only *run* the hidden query against a database and look at the result. It may **not** read the query text. So any solution has to work by running queries, not by parsing SQL.

---

## 2. Why the current code misses gaps

To find a range predicate on an attribute `A`, UNMASQUE puts one representative row in the minimised database, then **mutates the value of `A` in that row and re-runs the hidden query** to see whether the result is still non-empty. It uses **binary search** over the attribute's domain to find the lower and upper bound (`filter.py::get_filter_value`, and the similar searches in `aoa.py`).

Binary search has a built-in assumption:

> *"If the value in the middle satisfies the predicate, then everything between the starting point and the middle also satisfies it."*

That is true only when the satisfying values form **one solid block**. For a disjunction they form **several blocks with gaps between them**. When a gap is small compared to the jumps binary search makes, the search simply **steps over the gap**, lands in the next block, and reports one big block that swallows the gap.

The disjunction loop (`DisjunctionPipeLine._extract_disjunction` / `__run_extraction_loop`) tries to find more blocks by "falsifying" the block it already found and re-running the pipeline. But if the first block it found is the *over-approximated* `10..40`, then falsifying `10..40` also wipes out the legitimate `30..40` block, so it is never rediscovered. End result: `BETWEEN 10 AND 40`, gap missed.

---

## 3. Why we can't just scan every value

The obvious fix — try every value in `10..40` one by one — works, but does not scale. Real databases have wide domains and many attributes; doing a full scan of each attribute's domain, for every attribute, on every extraction, is far too slow. We need something that only does work proportional to *how many gaps there actually are*, not to *how big the domains are*.

---

## 4. The key idea: let the database point at the gaps

We already have something more powerful than "mutate one row and check": we have **the whole database `D`**, and the ability to run **both** the hidden query `Q_H` and our current extracted query `Q_E` on it and **compare the two result sets**.

If `Q_E` is too loose, then `Q_E(D)` contains rows that `Q_H(D)` does not. Each such extra row is a **witness**: the row that produced it has, in some column, a value that sits inside a gap. So instead of *searching* for gap values, **we let the database hand them to us** — in one query comparison we can discover *many* gap values at once.

This is the same idea NEP uses (comparing the hidden and extracted queries to find a discrepancy), but turned into a **repair loop** and used *during* extraction rather than just as a final check.

---

## 5. The assumption we rely on

For the database to be able to "point at" a gap, the gap value has to actually occur in the data. So we make this assumption explicit:

> **Signature assumption.** Every value that matters to the predicate has a *signature* in the database: for every value `v` that the hidden query treats specially on attribute `A` (a boundary of a block, or a value inside a gap), the database `D` contains at least one row of that relation with `A = v`, placed so that — together with matching rows of the other relations — it would flow through the *extracted* query `Q_E` and appear in `Q_E(D)`.

In plain words: *the database contains example rows that exercise the values used by the query.* This is reasonable: the database minimiser already keeps witness rows, and a query that filters on values which never appear in the data would be pointless. Without this assumption no black-box method can find a gap at all (there is simply nothing to observe), so it is the natural assumption to make.

Under this assumption, "the extracted query gives the same result as the hidden query on `D`" is the same as "no gap is missed" — because a missed gap would always show up as an extra row in the comparison.

---

## 6. The algorithm

There are two parts. Part 1 is a cheap improvement to the existing search that prevents most over-approximations from happening in the first place. Part 2 is the loop that *guarantees* no gap survives.

### Part 1 — gap-aware boundary search (cheap, optional but recommended)

When the existing binary search finishes and claims a block `[lo, hi]`, it actually only ever *probed* a handful of points on the way there; between two consecutive probed points there are un-probed stretches, and a gap can only be hiding in one of those `O(log domain)` stretches. So, right after settling a boundary:

1. for each un-probed stretch between two accepted (satisfying) probe points, probe its midpoint;
2. if that midpoint is **not** satisfying, we have found a gap — binary-search left and right around it to get the gap's edges, record a split, and recurse into the two remaining pieces (with a small depth limit);
3. if it is satisfying, optionally recurse once more.

Cost when there is no gap: a handful of extra probes. This catches every gap that is a reasonable fraction of the block — i.e. most real-world cases — for almost free. It does **not** by itself guarantee completeness; Part 2 does.

### Part 2 — the Disjunction Refinement Loop (the real fix)

Run this **after the rest of the pipeline has produced a first extracted query `Q_E`** (it fits naturally next to / inside the NEP stage, which already runs last and already knows how to compare `Q_H` with `Q_E`). It needs the **full database `D`** restored (not the minimised one), exactly as the result comparator already does.

```
build Q_E from the current extracted predicates (including the disjunction structure)

repeat:
    diff_plus  = Q_E(D)  -  Q_H(D)        # rows the extracted query wrongly KEEPS  (too loose)
    diff_minus = Q_H(D)  -  Q_E(D)        # rows the extracted query wrongly DROPS  (too tight / missing block)

    if diff_plus is empty and diff_minus is empty:
        STOP — Q_E now matches Q_H on D, and (by the signature assumption) no gap is missed

    if diff_plus is not empty:
        pick a witness row t from diff_plus
        find t's source rows (one per base relation) under Q_E          # Q_E per clause is conjunctive; join-match them
        # figure out WHICH predicate atom is too loose for t:
        put just t's source rows into the working database (a tiny instance)
        # on this tiny instance Q_H is empty (t is not in Q_H(D)) but Q_E is non-empty
        for each numeric / text atom  (R, A, current-range)  in the clause that matched t:
            set R's value of A back to a known-good value (from D_min); keep the other values as in t
            run Q_H:
                if the result becomes non-empty:
                    # this atom is (a) culprit, and t's value of A lies in a gap of it
                    v = t's value of A
                    find the gap around v:
                        e_lo = largest value < v that still satisfies   (binary search, or just the nearest smaller "good" data value)
                        e_hi = smallest value > v that still satisfies  (binary search, or the nearest larger "good" data value)
                    replace  [lo, hi]  by  [lo, e_lo] ∪ [e_hi, hi]  for atom (R, A)
                    (for a text atom: re-run the string-pattern extractor seeded with v to split the LIKE/= predicate,
                     or add an extra disjunct — same effect, different mechanics)
            restore R's value of A
        # (handle every witness row from diff_plus this way before rebuilding — many gaps get fixed per round)

    else:   # diff_plus empty but diff_minus not empty
        # we are too TIGHT somewhere — either we removed slightly too much, or a whole block is missing
        re-trigger block discovery (the existing disjunction loop step) for the relevant attribute, seeded by a row from diff_minus

    rebuild Q_E from the updated predicates
```

After the loop, `Q_E` matches `Q_H` on `D`; under the signature assumption that means **every gap has been found** and the extracted predicate on every attribute is exact.

### Worked example

* First extraction gives `l_quantity BETWEEN 10 AND 40` (gap `21..29` swallowed).
* Build `Q_E`, compare with `Q_H` on `D`.
* `diff_plus` contains every result row coming from a `lineitem` row with `l_quantity` in `21..29` (the signature assumption says such rows exist).
* Pick one, say `l_quantity = 25`. Attribute it: it's the `l_quantity` atom; `25` is in a gap.
* Find the gap edges: nearest satisfying values are `20` below and `30` above.
* Replace `BETWEEN 10 AND 40` with `BETWEEN 10 AND 20 OR BETWEEN 30 AND 40`.
* Rebuild `Q_E`, compare again → no difference → done. ✅

If the hidden query had been `l_quantity BETWEEN 10 AND 20 OR BETWEEN 30 AND 40 OR BETWEEN 50 AND 60`, the first comparison's `diff_plus` would already contain witnesses for *both* gaps (`21..29` and `41..49`), so one round of repair fixes both, and the second comparison confirms.

### Text columns

For a string attribute the "satisfying set" is not an interval but a set of strings matched by some `LIKE`/`=` pattern(s). The detection step is identical: a witness row in `diff_plus` whose string value `s` is rejected by `Q_H` tells us `s` is a "gap" in the string predicate. The repair step is different in mechanics only — re-run the existing string-pattern extractor (`filter.py::getStrFilterValue` / `handle_string_filter`) seeded with `s` to produce a tighter pattern or an extra `LIKE` disjunct — but it plugs into the same loop.

### Many attributes, many ranges

The loop handles these automatically:

* **Many attributes:** each witness row is blamed on one atom at a time; over several rounds, every over-loose atom on every attribute gets tightened.
* **Many ranges per attribute:** an atom's range is allowed to be a *union of intervals*; each repair splits one interval into two, so the union grows one block at a time until it is exact.

---

## 7. Why this finds every gap (the guarantee)

Assume the signature assumption holds. Suppose, for contradiction, that the loop has stopped but the extracted predicate on some attribute `A` is still too loose — i.e. there is a value `v` that `Q_E` accepts but the hidden query rejects. By the signature assumption, `D` contains a row `t` with `A = v` that flows through `Q_E`, so `t`'s result row is in `Q_E(D)`. Because `v` is rejected by the hidden query, that result row is **not** in `Q_H(D)`. So `diff_plus` is non-empty — but the loop only stops when `diff_plus` is empty. Contradiction. Therefore when the loop stops, **no over-approximation (no missed gap) remains.**

(The complementary direction — never *dropping* rows the hidden query keeps — is handled by the existing block-discovery logic plus the `diff_minus` branch.)

---

## 8. Why it stops, and how fast it is

* **It stops.** Every repair step *only tightens* `Q_E` (it removes a region that we have just verified, with the mutation oracle, is non-satisfying for that witness context). So `diff_plus` strictly shrinks each round and never grows. `D` is finite, so the loop ends.
* **It is not a brute-force scan.** One result-set comparison reveals *all* current gap values at once, so the number of comparison rounds is roughly *(number of gaps + 1)*, not *(number of rows)* or *(size of domain)*. The only per-value work is (a) blaming a witness on an atom — bounded by the number of atoms in that clause — and (b) finding a gap's two edges with a short binary search. Domains are never scanned.
* **No cost when there is nothing wrong.** If the first extraction was already exact, the very first comparison shows no difference and the loop exits immediately. (It can even reuse the comparison `verify_correctness` already performs.)

---

## 9. Where this plugs in, and keeping the other pipelines working

* **Part 1** is a localised change inside the boundary searches: `filter.py::get_filter_value` and the analogous searches in `aoa.py`. It does not change any interface, so all three pipelines benefit automatically.
* **Part 2** is a new pipeline fragment, run as the **last step** of extraction, after `_extract_NEP`. It:
  * reuses `ResultComparator` for the `Q_H` vs `Q_E` comparison,
  * reuses `db_restorer` / `TpchSanitizer` to make sure it runs against the full database `D` and to undo its probing afterwards,
  * reuses the existing mutation/restore helpers (`checkAttribValueEffect`, `revert_filter_changes_in_tabset`, etc.) for attribution and edge-finding,
  * reuses `filter.py`'s string-pattern extractor for text atoms,
  * feeds the refined predicates back through `QueryStringGenerator.formulate_query_string()` so `Q_E` is rebuilt the normal way.
* **Other pipelines:**
  * `ExtractionPipeLine` — call the refinement loop at the end of `_after_from_clause_extract`, after `_extract_NEP`.
  * `OuterJoinPipeLine` (subclass) — it adds the outer-join structure *after* the core extraction, so the refinement loop must run **after** that, on the fully assembled `Q_E`; an over-loose filter sitting inside an `ON` clause is just another atom and is repaired the same way.
  * `UnionPipeLine` (subclass) — each `UNION ALL` arm is extracted separately and then stitched. Run the refinement loop **per arm** (each arm has its own predicate set) before stitching, so attribution stays simple; the stitched query is then exact by construction.
  * The refinement loop must be a **no-op** when there is no discrepancy, so turning it on cannot regress any query that already extracts correctly.

No new config flag: refinement is part of disjunction handling, so it is gated by the **existing `[feature] or` flag** (`config.detect_or`) — the same flag that turns on the disjunction-discovery loop. When you run a query through `main_cmd.py` with its OR flag on (`orf=True` in its `TestQuery`, which sets `conn.config.detect_or`), both the existing disjunction loop and the new refinement run; with the OR flag off, neither runs.

---

## 10. Things to be careful about during implementation

1. **Run against the full database, not the minimised one.** The gap values only exist in `D`. Restore before the loop; clean up after.
2. **Attribute the blame correctly.** A too-loose result row can be caused by any one of several atoms; always confirm with the mutation oracle (restore that one attribute to a known-good value, see if the hidden query "lights up") before editing a predicate. Never edit a predicate on a guess.
3. **Verify before you remove.** Only carve a gap out of a range after the mutation oracle confirms that region is non-satisfying for the witness context — this is what keeps the loop from ever creating a *false negative* and is what makes termination hold.
4. **Both directions of the diff.** `diff_plus` → tighten / add gaps. `diff_minus` → we were too tight or a block is missing → re-trigger block discovery. Don't ignore `diff_minus`.
5. **`<>` is just a width-one gap.** No special case needed; the same machinery produces `... AND a <> 7` (or the equivalent split) when the "gap" found around `7` is a single value.
6. **Numeric precision and dates.** Edge-finding must use the column's scale / one-day granularity, exactly like the existing `get_constants_for` / `un_precision` logic.
7. **NULLs.** Keep using the existing "non-empty and null-free" test for "satisfying", everywhere.
8. **Performance knob.** Each round runs `Q_H` and `Q_E` over the full `D`; batch all repairs found in a round before re-running, so the number of full-database passes stays close to *(number of gaps + 1)*.

---

# Implementation Plan (against the actual code)

This is the concrete plan for landing the approach above. **v1 implements Part 2 only** — that alone gives the "no gap missed" guarantee under the signature assumption. **Part 1 (gap-aware boundary search) is a later, optional speed-up** that just reduces how many refinement rounds Part 2 needs.

## A. Flag — reuse the existing `or` flag (no new flag, no config changes)

Refinement runs **iff `connectionHelper.config.detect_or` is true** — the same `[feature] or` flag that already controls the disjunction loop. So:

* nothing to add in `constants.py` / `configParser.py` / `config.ini`;
* in `main_cmd.py` you already pick flags per query via the `TestQuery(... cs2, union, oj, nep, orf)` tuple, and `orf=True` already sets `conn.config.detect_or` — that one flag now also enables refinement;
* the GUI / Django path picks up `config.detect_or` the same way.

(Note: the binary-search over-approximation that produces `BETWEEN 10 AND 40` happens regardless of the `or` flag, but *fixing* it is disjunction work, so it is correct to gate the fix on the `or` flag — exactly matching "if the disjunction flag is on, disjunction handling works".)

## B. The refiner module

**Reuse over rewrite.** The refiner is mostly glue around existing code — it should write almost no new low-level logic:

| Need | Reuse |
| --- | --- |
| run `Q_H` / `Q_E` on full `D`, get the difference | `ResultComparator` / `NepComparator` (`run_diff_queries`, `is_match`, `row_count_r_e`, `row_count_r_h`) — add only a tiny directed-count helper if one isn't already there |
| "is value `v` satisfying?" / "set `R.A := v`, run `Q_H`" | `Filter.run_app_for_a_val`, `Filter.checkAttribValueEffect`, `Filter.get_filter_value('=' / '<=' / '>=')`, `Filter.revert_filter_changes_in_tabset` |
| reset / mutate the `D_min` working instance | `UN2WhereClause.restore_d_min_from_dict`, `mutate_dmin_with_val`, `insert_into_dmin_dict_values` |
| restore the full DB / clean up working tables | `DbRestorer`, `TpchSanitizer` (already used by `verify_correctness` / the signal handler) |
| string-pattern (re)extraction for text atoms | `Filter.handle_string_filter`, `Filter.getStrFilterValue`, `QueryStringGenerator._getStrFilterValue` |
| render a multi-interval / `IN` / `NOT IN` predicate | `QueryStringGenerator.formulate_predicate_from_filter`, `__generate_predicate_string_for_in_operator`, the `arithmetic_disjunctions` setter, `optimize_arithmetic_filters`, `updateExtractedQueryWithNEPVal` (its `op == 'IN'` branch already builds `between … OR between …`) |
| re-discover a genuinely missing block (the `diff_minus` case) | `DisjunctionPipeLine._mutation_pipeline`, `__falsify_predicates`, `__run_extraction_loop` — v1 only logs this case |

New file `mysite/unmasque/src/core/disjunction_refiner.py` (a plain helper class, not a pipeline stage), used by the pipelines:

```
class DisjunctionRefiner:
    REFINE_CUTOFF = 10                       # max rounds, like NEP_CUTOFF

    def __init__(self, connectionHelper, core_relations, all_sizes,
                 genCtx, q_generator, get_datatype): ...

    def refine(self, query, eq) -> str:
        if not connectionHelper.config.detect_or: return eq          # reuse the existing OR flag
        comparator = ResultComparator(connectionHelper, isHash=False, core_relations)  # reuse existing comparator
        comparator.full_db_restore = True
        probe = Filter(connectionHelper, core_relations, genCtx.global_min_instance_dict)  # reuse Filter's mutation+check helpers
        probe.do_init()
        for _round in range(self.REFINE_CUTOFF):
            fp_exists = self._has_false_positives(comparator, query, eq)   # Q_E(D) \ Q_H(D) non-empty?
            if not fp_exists:
                return eq                       # by signature assumption: no missed gap
            progressed = False
            for atom in self._suspect_range_and_text_atoms():             # from q_generator's working copy
                new_pred = self._reverify_atom(probe, query, atom)        # exact good/bad partition over data values in range
                if new_pred != atom.current:
                    self._apply(atom, new_pred); progressed = True
            if not progressed:
                self.logger.error("disjunction refinement could not make progress"); break
            eq = self._rebuild_eq()
        return eq
```

### B.1 `_has_false_positives` / the diff

Reuse `ResultComparator`. It already builds `r_h` (result of `Q_H`) and `r_e` (result of `Q_E`) views/tables under a full DB restore and runs the symmetric-difference query. We need the **directed** difference, not just the boolean match:

* add a small method to `ResultComparator` (or `Comparator`) like `count_extra_in_r_e()` → `SELECT count(*) FROM (r_e EXCEPT ALL r_h)` and `count_missing_from_r_e()` → `SELECT count(*) FROM (r_h EXCEPT ALL r_e)`. v1 only needs `count_extra_in_r_e() > 0` to decide "there is a gap somewhere". (`diff_minus = count_missing_from_r_e() > 0` is a *different* bug — log it and stop; do not try to fix it in v1.)
* sequencing note: after the comparison, the working schema / D_min has been disturbed by the full restore; before the next mutation-probe step, rebuild D_min via `probe.restore_d_min_from_dict()` (from `un2_where_clause`). Mirror exactly what `_extract_NEP` already does around `NepComparator` + `NepMinimizer`.

### B.2 `_suspect_range_and_text_atoms`

Read from `q_generator._workingCopy`:
* every `(tab, attrib, 'range', lb, ub)` in `arithmetic_filters`,
* every `(tab, attrib, 'IN'/'in', value_list, …)` in `filter_in_predicates` whose `value_list` contains a `(lb,ub)` tuple (a multi-interval predicate that may itself still over-approximate inside one of its intervals),
* every `(tab, attrib, 'equal'/'LIKE', val, val)` in `arithmetic_filters` (text atoms).

(As a safe superset we re-verify *all* of these whenever `count_extra_in_r_e() > 0`; the work is bounded by the number of filter atoms, which is small.)

### B.3 `_reverify_atom` — the exact fix for one numeric atom `(tab, attrib, [lo, hi])`

```
vals = SELECT DISTINCT attrib FROM <user_schema>.tab WHERE attrib BETWEEN lo AND hi ORDER BY attrib
       # only the data values that can actually cause a discrepancy on D
good = []
for v in vals:                                  # optional: subdivide the list + probe-budget; see Part 1 / perf note
    set tab.attrib := v in D_min                 # probe.mutate_dmin_with_val / form_update_query_with_value
    res = run Q_H on D_min
    if result non-empty & null-free:  good.append(v)
    restore tab.attrib                           # probe.revert_filter_changes_in_tabset
new_intervals = maximal consecutive runs of `good`   # each run -> (run_min, run_max); a length-1 run -> a point value
return new_intervals
```

Soundness of probing on `D_min` rather than on `t`'s context: a "range" atom is `attrib op const`, which is **context-independent** — if `v ∉ S_A` then setting `attrib := v` drops the row in *every* context, so `D_min` is a valid witness instance. (Correlated `attrib1 op attrib2` predicates are *not* "range" atoms and are out of scope here.)

Precision/dates: enumerate `vals` at the column's natural granularity; when forming run boundaries use `get_constants_for` / `un_precision` exactly as `filter.py` does.

### B.4 `_reverify_atom` for a text atom `(tab, attrib, 'LIKE'/'equal', p, p)`

```
vals = SELECT DISTINCT attrib FROM <user_schema>.tab WHERE attrib LIKE p   (or = p)
leaks = [ v for v in vals if probing tab.attrib := v on D_min gives EMPTY result ]   # accepted by Q_E, rejected by Q_H
if leaks:
    # v1 repair: keep the pattern, add  tab.attrib NOT IN (leak1, leak2, …)
    add (tab, attrib, 'NOT IN', FrozenList(leaks), FrozenList(leaks)) to q_generator._workingCopy.filter_not_in_predicates
# v2 repair (later): re-run Filter.getStrFilterValue seeded with a kept value + a leaking value to get a tighter LIKE,
#                    or split into  attrib LIKE p1 OR attrib LIKE p2.
```

(`NOT IN` rendering already exists via `QueryStringGenerator.formulate_predicate_from_filter` / `rewrite_for_NEP`.)

### B.5 `_apply` — write the refined predicate back into `q_generator`

For a numeric atom that became a single interval: just update the `(tab,attrib,'range',lb,ub)` tuple in `arithmetic_filters` in place.

For a numeric atom that became ≥2 intervals (or interval(s) + point(s)):
* remove the old `(tab,attrib,'range',lo,hi)` from `_workingCopy.arithmetic_filters`,
* add/extend a `(tab, attrib, 'IN', FrozenList([(lb1,ub1),(lb2,ub2),…, point, …]), FrozenList(...))` in `_workingCopy.filter_in_predicates` — this is exactly the shape `__generate_predicate_string_for_in_operator` already renders as `(tab.attrib between … OR tab.attrib between … OR tab.attrib IN (…))`.
* this is the same data movement the existing `arithmetic_disjunctions` setter does; factor a small helper `q_generator.replace_range_with_intervals(tab, attrib, intervals)` so both call sites share it.

### B.6 `_rebuild_eq`

After predicate edits, regenerate the query string. Don't call `formulate_query_string()` blindly — `__generate_select_clause` *appends* to `select_op`, so re-running it duplicates the SELECT list. Add `QueryStringGenerator.rebuild_after_where_change()` that:
* resets `select_op`, `group_by_op`, `where_op` to `''`,
* re-runs `__generate_where_clause()` (now picks up the new `filter_in_predicates` / `filter_not_in_predicates`), then re-appends any NEP `NOT IN` predicates the way `rewrite_for_NEP` does,
* re-runs `generate_groupby_select()`,
* returns `write_query()`.
Simplest safe alternative: snapshot `_workingCopy` before refinement, and on each rebuild start from that snapshot + apply all accumulated edits.

## C. Wiring it into the pipelines

`mysite/unmasque/src/pipeline/ExtractionPipeLine.py`:
* rename the body of `_after_from_clause_extract` (everything from `time_profile = create_zero_time_profile()` through `eq = self._extract_NEP(...)`) into a new method `_extract_core_pipeline(self, query, core_relations) -> eq`,
* `_after_from_clause_extract` becomes:
  ```
  eq = self._extract_core_pipeline(query, core_relations)
  if eq is None: return None
  return self._refine_disjunctions(query, eq)
  ```
* `_refine_disjunctions(self, query, eq)` constructs `DisjunctionRefiner(self.connectionHelper, core_relations, self.all_sizes, self.genPipelineCtx, self.q_generator, self.filter_extractor.get_datatype)` and returns `refiner.refine(query, eq)`; it also feeds `time_profile` (add `time_profile.update_for_*` — reuse an existing bucket or add `update_for_refinement`).

`mysite/unmasque/src/pipeline/OuterJoinPipeLine.py`:
* `_after_from_clause_extract` must NOT call `super()._after_from_clause_extract` anymore (that would refine before the OJ structure is added). Instead:
  ```
  eq = self._extract_core_pipeline(query, core_relations)   # inherited from ExtractionPipeLine
  if eq is None: return None
  ... existing OuterJoin block; if oj.Q_E is not None: eq = oj.Q_E ...
  return self._refine_disjunctions(query, eq)
  ```
  (The OJ extractor already updates `self.q_generator`, so the refiner sees the OJ-augmented working copy; an over-loose filter that sits inside an `ON` clause is just another `arithmetic_filters` entry and is handled identically.)

`mysite/unmasque/src/pipeline/UnionPipeLine.py`:
* **no change** — `extract()` already calls `self._after_from_clause_extract(query, core_relations)` once per `UNION ALL` arm with the other relations *nullified* (`__nullify_relations`). Because the non-arm relations are empty during that call, running `Q_H` against that DB returns exactly that arm's contribution, so the per-arm `Q_E` vs `Q_H` diff inside the refiner is valid. The stitched query is then exact by construction. (Double-check: `q_generator.reset()` happens *after* `_after_from_clause_extract` returns, so the refiner still sees the arm's working copy — good.)

## D. Tests

New `mysite/unmasque/test/DisjunctionRefinementTest.py` (model it on `FilterTest` / `OldPipelineTest`), with hidden queries:
1. `... FROM lineitem WHERE l_quantity BETWEEN 10 AND 20 OR l_quantity BETWEEN 30 AND 40` → expect a disjunctive predicate, not `BETWEEN 10 AND 40`.
2. two attributes each with a gap.
3. one attribute with three blocks (two gaps) — confirm one round fixes both (diff reveals both witnesses).
4. `... WHERE l_quantity BETWEEN 10 AND 40 AND l_quantity <> 21` — the width-1 gap case.
5. a text case: `... WHERE n_name LIKE 'A%' OR n_name LIKE 'C%'` mis-extracted loosely → `NOT IN` repair restores equivalence.
6. **regression**: a plain conjunctive query with no disjunction → refiner is a no-op (first diff empty), extracted query unchanged, no extra DB work beyond one comparison.
Also run the existing suites that touch this area: `PipelineFactoryTest`, `OldPipelineTest`, `FilterTest`, `WhereClauseTest`, `OjAndUnionTest`, `NEPTest`, `MutationPipelineTest` — these must stay green. Tests need data with the right *signature* rows (e.g. `lineitem` rows whose `l_quantity` falls in the gap); extend the test fixtures / `test/util` queries accordingly.

## E. (Later, optional) Part 1 — gap-aware boundary search

In `filter.py`: have the binary-search loops in `get_filter_value` record their `(mid_val, satisfied)` trace; after settling a bound, add `__verify_no_gap_on_path(...)` that probes the midpoint of each un-probed stretch between two consecutive satisfied probes and, on a miss, locates that gap's edges and returns the segment list. Surface this through a new `extract_range_predicate(...)` wrapper used by `handle_point_filter` / `handle_precision_filter` / `handle_filter_for_subrange` so the existing scalar-returning `get_filter_value` is untouched and callers can receive a list of `(lb,ub)` segments. Mirror the same trace-and-reprobe idea in the inequality searches in `aoa.py`. This is purely an optimisation — Part 2 already guarantees correctness — so it can ship separately.

## F. Open questions to settle before coding

1. Probe budget for `_reverify_atom` on value-dense ranges: hard cap (and then *flag* the atom as unverified, sacrificing the guarantee for speed) vs. no cap (guarantee always, but a pathological column could be slow). Recommended default: no cap, since by the signature assumption only the *distinct data values inside an already-narrow believed range* are probed.
2. Time-profile bucket: reuse `update_for_nep` / `update_for_view_minimization`, or add a dedicated `update_for_refinement`?
3. Whether `_extract_NEP` and the refiner should be merged into one post-extraction loop (they share the `Q_H` vs `Q_E` comparison and the `D_min`/full-DB juggling) or kept as two consecutive steps. Recommended: keep separate for v1, revisit later.

---

# Implementation status

**Done (v1, Part 2):**

* `mysite/unmasque/src/core/disjunction_refiner.py` - the `DisjunctionRefiner` class plus `make_filter_for_refiner`.
  Re-verifies each numeric `range` / `IN` atom (and each `LIKE` atom) by enumerating the distinct *data*
  values the atom currently keeps and probing each against the hidden query via the `Filter` mutation
  oracle on `D_min`; processes each original interval separately (so original gaps are never bridged);
  sharpens gap edges with `Filter.get_filter_value`; rewrites the atom; rebuilds `Q_E`; repeats up to
  `REFINE_CUTOFF` rounds. Uses `NepComparator` for the `Q_H` vs `Q_E` check (so it works per-arm for
  union queries, where the other relations are nullified in the working schema). Wrapped so it can only
  ever return either an *exact* refined query or the query it was given - never a regression.
* `mysite/unmasque/src/util/QueryStringGenerator.py` - `replace_range_with_intervals`, `add_not_in_predicate`,
  `rebuild_after_predicate_change`.
* `mysite/unmasque/src/core/elapsed_time.py` - a "Disjunction Refinement" time-profile bucket.
* `mysite/unmasque/src/pipeline/ExtractionPipeLine.py` - `_after_from_clause_extract` split into
  `_extract_spjgaol` (the old body) + a wrapper that calls `_refine_disjunctions` (gated on `config.detect_or`).
* `mysite/unmasque/src/pipeline/OuterJoinPipeLine.py` - runs `_extract_spjgaol` -> outer-join extraction ->
  `_refine_disjunctions` (so refinement sees the fully assembled query).
* `mysite/unmasque/src/pipeline/UnionPipeLine.py` - unchanged; per-arm refinement happens for free because
  it calls `_after_from_clause_extract` per arm.
* `mysite/unmasque/test/DisjunctionRefinementTest.py` - end-to-end tests (need a live TPC-H DB).

No new config flag - the existing `[feature] or` flag (`config.detect_or`) enables both the disjunction loop
and the refiner, exactly as in `main_cmd.py` where each query's `TestQuery(..., orf=...)` sets it.

**Deferred (v2):**

* over-loose `<=` / `>=` half-line atoms (only two-sided `range` atoms are refined today);
* `<>` mixed with a range is handled (it shows up as a width-one gap when the gap value is probed) but a
  standalone `<>` is still left to `aoa.py` / NEP;
* sharper text repair (today: `... NOT IN (leaking strings)`; later: a tighter `LIKE` or a split);
* over-loose filters that live inside an outer-join `ON` clause (the refiner currently can't edit those, so
  for outer-join queries it is effectively a no-op - it just verifies and, if it can't make the query exact,
  returns the query it was given);
* Part 1 (the cheap "gap-aware boundary search" inside `filter.py` / `aoa.py`) is not implemented - Part 2
  alone gives the correctness guarantee, Part 1 would just reduce how many refinement rounds are needed.

**Not yet verified:** the code compiles, but it has not been run end-to-end (no PostgreSQL available in the
environment it was written in). Needs a pass against the standard TPC-H test setup before relying on it.

# XPOSE_NEW — Extraction Coverage Implementation Checklist

> **Purpose.** A living, cross-chat tracker for extending what the extraction pipeline can
> recover from a black-box `Qh`. Each item carries the *theory first* (a mutation/Pop-grounded
> approach), then files, dependencies, observability limits, and a verification plan. We
> implement items one at a time (a single item may span several chats). When all feasible
> items are done we write the thesis (see the bottom section).
>
> **Provenance.** Derived from a code-verified multi-agent audit of the pipeline (34 agents,
> every verdict cross-checked against source). Full raw analysis: `/tmp/audit_result.json`
> and the per-construct detail dump referenced in the audit chat. Re-derive by re-reading the
> cited `file:line` anchors — never trust this doc over the code.

---

## 0. Ground rules (read before implementing anything)

1. **Black-box discipline — NO query parsing.** We never read the text of `Qh`. Everything is
   recovered by *mutating the database, running `Qh`, and observing the result* (the `Pop`
   oracle = "is the result non-empty and null-free", plus result-shape / row-count / read-back
   values). If a proposed approach needs to look at the query string, it is wrong — redesign
   it as a mutation experiment or mark the construct Infeasible.

2. **Two layers must both change for a construct to count as "Supported":**
   - **Detection** — a pipeline stage observes the construct via mutation (`core/*`).
   - **Emission** — [`QueryStringGenerator`](mysite/unmasque/src/util/QueryStringGenerator.py)
     (QSG) renders it into the output SQL.
   A construct detected but not emitted is still broken. Conversely, never emit something we
   cannot observe.

3. **The output ceiling.** [`assembleQuery`](mysite/unmasque/src/util/QueryStringGenerator.py#L79-L88)
   emits exactly six slots: `Select / From / Where / Group By / Order By / Limit`. Anything
   needing a new clause (DISTINCT, HAVING, OFFSET, NULLS, set-op wrapping) first needs a new
   slot here — see **Shared enabler S1**.

4. **The minimization tension.** The View Minimizer collapses `D` to a single witness row
   `D¹`. Many constructs (DISTINCT, UNION-dedup, cross-product scaling, HAVING, OFFSET,
   self-join degree) are *multiplicity* phenomena that are **invisible on a single row** — they
   must be probed on a controlled multi-row instance (reuse
   [`cardinality_probe`](mysite/unmasque/src/core/cardinality_probe.py) /
   [`multiplicity_probe`](mysite/unmasque/src/core/multiplicity_probe.py) duplication +
   row-count primitives). Flag this per item.

5. **The null-free tension.** `Pop` rejects any row with a null in a projected column
   ([nullfree_executable.py](mysite/unmasque/src/core/abstract/nullfree_executable.py)). This
   makes `IS NULL`, `COALESCE`, and `NULLS FIRST/LAST` hard-to-infeasible — the oracle fights us.

6. **Predicate-tuple contract.** Filter predicates are `(tab, attr, op, lb, ub)`; AOA tuples are
   `(node, op, node)` of length 3. Per [CLAUDE.md](CLAUDE.md), when you add a new op or tuple
   shape you MUST audit every consumer: [aoa.py](mysite/unmasque/src/core/aoa.py),
   [equi_join.py](mysite/unmasque/src/core/equi_join.py), and the QSG render path.

7. **Always add a feature flag** for new detection that costs probes or risks false positives:
   line in [config.ini](mysite/config.ini) `[feature]` → property in
   [configParser.py](mysite/unmasque/src/util/configParser.py) → read via
   `self.connectionHelper.config.<flag>`. Default new/risky stages OFF.

8. **Verification is part of "done".** For each item, pick or author a `Qh` exercising the
   construct, run extraction, and confirm `Qh` ≡ `Q_E` by **bag-equality** over full `D`
   (`Qh EXCEPT ALL Q_E` and `Q_E EXCEPT ALL Qh` both empty) via
   [Comparator](mysite/unmasque/src/core/abstract/Comparator.py). Capture a before/after example
   for the thesis. Add a case to the relevant `test/*Test.py`.

**Status legend:** ☐ not started · ◐ theory locked · ▣ implemented (unverified) · ✅ verified.

---

## Shared enablers (do early — many items depend on these)

### S1 — QSG clause-slot infrastructure  `Moderate`  ☐
Add the missing output slots so downstream items have somewhere to render to.
- **Now:** [`assembleQuery`](mysite/unmasque/src/util/QueryStringGenerator.py#L79-L88) hardcodes
  6 clauses; `QueryDetails` has no `distinct_op` / `having_op` / `offset_op` fields.
- **Do:** add fields + `append_clause` lines for `distinct` (a `Select distinct` prefix, not a
  trailing clause), `Having` (after Group By), `Offset` (after Limit / before? — Postgres allows
  `LIMIT n OFFSET m`, emit `Offset` after `Limit`). Keep each gated so absent features render
  nothing (the existing `append_clause` no-op-on-empty contract at
  [QSG:14-17](mysite/unmasque/src/util/QueryStringGenerator.py#L14)).
- **Unlocks:** WI-22 (DISTINCT), WI-23 (HAVING), WI-18 (OFFSET), WI-33 (NULLS).
- **Verify by:** unit-render a `QueryDetails` with each slot set; no behavior change when unset.

### S2 — Controlled multi-row / duplicate-row probe primitive  `Easy`  ✅ DONE (2026-06-02)
A reusable helper to duplicate the witness row(s) feeding the kept result and revert, so
multiplicity-dependent constructs are observable.
- **Now (before):** [`cardinality_probe._insert_duplicate`](mysite/unmasque/src/core/cardinality_probe.py)
  (`INSERT … SELECT … RETURNING ctid`) and
  [`multiplicity_probe._count_rows`](mysite/unmasque/src/core/multiplicity_probe.py)
  existed, copy-pasted, owned by their stages.
- **Do:** factor a small shared utility (duplicate-by-ctid + delete-by-ctid + count) usable from
  aggregation, union, distinct, having, count-distinct probes. No new oracle.
- **Unlocks:** WI-05, WI-06, WI-14, WI-22, WI-23.
- **Verify by:** duplicate→count→revert leaves `D` unchanged (ctid set restored).
- **DONE — what shipped:** new [`RowProbe`](mysite/unmasque/src/core/row_probe.py) class
  (`duplicate_rows(fqn, ctids=None)` — all rows or targeted-by-ctid; `delete_rows(fqn, ctids)`;
  `count_rows(query)` — header-stripped data-row count; `list_ctids(fqn)`). No new oracle: duplicate/
  delete go through `connectionHelper`, count reuses `app`; because `app` shares the stage's
  `connectionHelper` (one connection/transaction) an uncommitted INSERT is visible to the next
  `app.doJob` and the matching DELETE reverts it. Retrofitted the two owners to delegate
  ([`cardinality_probe`](mysite/unmasque/src/core/cardinality_probe.py) `_insert_duplicate`→`duplicate_rows`,
  `_delete_rows_at_ctids`→`delete_rows`; [`multiplicity_probe`](mysite/unmasque/src/core/multiplicity_probe.py)
  `_count_rows`→`count_rows`, exact-behaviour port). `_count_qh` in cardinality_probe left untouched to
  preserve the verified self-join detection.
- **Verified:** [`RowProbeTest.py`](mysite/unmasque/test/RowProbeTest.py) — 10 unit cases (SQL the
  primitive builds; header-stripping count) + 1 live-DB integration case proving the acceptance
  criterion: on a throwaway `public` table, `duplicate(1 ctid) → count(=+1) → delete → count` restores
  the **exact ctid set** (`before == after`). First live consumer is WI-05; the Group_By stage's
  duplicate-by-ctid + revert is visible in the e2e DEBUG trace.

---

## Tier 1 — Quick wins (Easy, high value-to-effort) — do first

### WI-01 — Fix `COUNT(col)` rendering bug  `Easy`  ✅ DONE (2026-06-01)
- **Now (BUG, verified):** `COUNT='Count'`, `COUNT_STAR='Count(*)'`
  ([constants.py:29-30](mysite/unmasque/src/util/constants.py#L29-L30)). At
  [QSG:611](mysite/unmasque/src/util/QueryStringGenerator.py#L611) the test is `if COUNT in label`,
  and `'Count'` is a substring of `'Count(*)'`, so **column-`COUNT` short-circuits to the bare
  token `'Count'` and drops its column** → emits invalid `Count as cnt`. Only `COUNT(*)` works
  (its label is already valid SQL). Detection is fine — aggregation stores `(attrib,'Count')`
  ([aggregation.py:318-319](mysite/unmasque/src/core/aggregation.py#L318)); `('',COUNT_STAR)` at
  [:243](mysite/unmasque/src/core/aggregation.py#L243).
- **Do:** import `COUNT_STAR` into QSG ([line 10](mysite/unmasque/src/util/QueryStringGenerator.py#L10))
  and change the gate to `if label == COUNT_STAR: elt = COUNT_STAR else: elt = label + '(' + elt + ')'`.
  Column-`COUNT` then renders `Count(c_custkey)`; `COUNT(*)` preserved.
- **Files:** [QueryStringGenerator.py:610-614](mysite/unmasque/src/util/QueryStringGenerator.py#L610-L614).
- **Risk:** none material; `'Count' in` substring use is local to this line.
- **Verify by:** `SELECT k, COUNT(orderkey) FROM … GROUP BY k`; AggregationTest case.
- **DONE — what shipped:** import `COUNT_STAR`; gate the short-circuit on `label == COUNT_STAR`
  ([QueryStringGenerator.py:611](mysite/unmasque/src/util/QueryStringGenerator.py#L611), with a
  comment). Added permanent regression test
  [CountRenderTest.py](mysite/unmasque/test/CountRenderTest.py) (4 cases, all pass) exercising the
  real `__generate_select_clause` — column-COUNT now renders `Count(o_orderkey) as order_count`,
  `COUNT(*)` and SUM/AVG unchanged. Ran on a populated `QueryDetails`, no DB mutation.
- **⚠️ IMPORTANT finding (the fix is currently LATENT — read before WI-05/WI-06):** an end-to-end
  extraction of `select o_orderpriority, count(o_orderkey) … group by o_orderpriority` against live
  TPC-H emitted `Count(*) as order_count` (result-equivalent, `pipeline.correct=True`) — i.e. the
  pipeline reconstructs `count(col)` as `count(*)` today, so the `(col,'Count')` label this fix
  renders is **never produced by the current detection flow**. Mechanism: the **projection** stage
  mutates column *values* (which never change a COUNT), finds no value-dependency for a count
  column, leaves its projected attribute `''`, and
  [aggregation.py:241-243](mysite/unmasque/src/core/aggregation.py#L241-L243) then overrides any
  empty-projected slot to `COUNT_STAR`. The audit's claim that detection already stores `(col,'Count')`
  missed this line-243 override. **Consequence:** WI-01 is correct + safe (no behavior change for
  `COUNT(*)`; verified) and is a *prerequisite render fix*, but the observable `Count(col)` output
  only appears once the **detection-side** distinction lands — see WI-05/WI-06. Capture the real
  before/after example for the thesis then.

### WI-02 — Lift LIMIT=1000 cap via exponential+binary search  `Easy`  ✅ DONE (2026-06-01)
- **Now (before):** [limit.py:21](mysite/unmasque/src/core/limit.py#L21) seeds `no_rows=limit_limit`
  (default 1000, [configParser.py:25](mysite/unmasque/src/util/configParser.py#L25)); linear
  insert-up-to-N scan ([limit.py:45-67](mysite/unmasque/src/core/limit.py#L45)); gives up →
  `limit=None` past the cap ([:70-71](mysite/unmasque/src/core/limit.py#L70)).
- **Do:** insert in geometric batches (1,2,4,8,…) until result cardinality plateaus, then binary-
  search the exact L between the last two sizes. O(log L) probes. Emission unchanged
  ([QSG:255](mysite/unmasque/src/util/QueryStringGenerator.py#L255)).
- **Risk:** plateau signal needs distinct/null-free inserted rows; refine step disambiguates a
  LIMIT sitting on a power-of-two boundary.
- **Verify by:** a `Qh` with `LIMIT 5000`; LimitTest.
- **DONE — what shipped:** rewrote `doLimitExtractJob` around two new helpers
  ([limit.py](mysite/unmasque/src/core/limit.py)): `__probe_limit_card` (one black-box probe —
  `do_init()` reset + insert exactly `m` matching rows per relation + `app.doJob` + the unchanged
  `len(result) − rmin_card + 2` normalization) and `__search_limit` (exponential doubling until the
  cardinality plateaus, then binary-search the bracket; factored to take the probe as a callable so
  it is unit-testable). The reported limit is **`plateau − 1`** (constant-independent), *not* the
  boundary insert count (which is `L − 1`, shifted by the D¹-witness duplicate — verified on real
  data). `bounded` flag splits the no-group case (budget `2·no_rows`, plateau-confirmed) from the
  group-bounded case (budget `no_rows`, single-shot edge read — old behaviour preserved). Emission
  untouched.
- **Key correction to the original framing:** the cap is now *cheap*, not removed. Detectable
  LIMIT at a given `no_rows` is still ~`no_rows`, but (i) the boundary `L = no_rows` is now
  recovered rather than dropped, and (ii) inserts scale with `~2L` not the budget, so
  `[options] limit` can be raised to ≥5000 without taxing small-LIMIT queries. That is the real lift.
- **Verified:** [LimitSearchTest.py](mysite/unmasque/test/LimitSearchTest.py) — 9 cases on the real
  `__search_limit` with a synthetic `min(m,L)+c` oracle (incl. L=5000 in <40 probes, no-limit→None,
  group-bounded edge, tiny-L floor, logarithmic probe count); all pass. End-to-end on live TPC-H:
  `… Order By o_orderkey Limit 1500` (past old cap) and `Limit 10` (control) both emit the correct
  LIMIT with `pipeline.correct=True`; DEBUG trace shows the geometric+binary probe sequence;
  `public.orders` 1,500,000 rows intact. Full writeup in
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md#wi-02--limit-detection-via-exponential--binary-search).

### WI-03 — Robust outer-join candidate equivalence (Comparator EXCEPT ALL)  `Easy`  ✅ DONE (2026-06-02)
- **Now (before):** [`__are_the_results_same`](mysite/unmasque/src/core/outer_join.py#L253) did
  ordered positional Python row-equality after a length check — order/duplicate fragile; the code
  comment itself said "maybe use the available result comparator techniques".
- **Do:** replace with the proven Re/Rh `EXCEPT ALL` both-directions-empty bag check
  (cf. [Comparator.run_diff_queries / is_match](mysite/unmasque/src/pipeline/abstract/Comparator.py#L63)).
- **Risk:** low (candidate set is tiny — one per BFS root).
- **Verify by:** O4/O6 multi-edge workload queries; OuterJoin path tests.
- **DONE — what shipped:** rewrote `__are_the_results_same` + new helper `__bag_diff_count`
  ([outer_join.py:253](mysite/unmasque/src/core/outer_join.py#L253)). Two results are bag-equal iff
  `(Qh EXCEPT ALL Q_E)` and `(Q_E EXCEPT ALL Qh)` are **both** empty (`Comparator.is_match` semantics).
  The diff is run **in-stage** via `app.doJob("select count(*) from ((<left>) except all (<right>)) as T;")`
  — the gap_witness inline pattern ([gap_witness.py:228](mysite/unmasque/src/core/gap_witness.py#L228)) —
  so it observes the join-breaking / NULL-injection mutation the caller just applied to D¹.
  `Comparator.match` itself is unusable here: it first restores tables to `user_schema`, erasing the
  mutation the test depends on. Soundness: the dangerous direction is a **false positive** (accepting a
  non-equivalent candidate → emitting wrong SQL), so on any diff failure (`None`) it **fails closed**
  (rejects). Added a `if not same: return False` short-circuit (the caller threads `same` across edges;
  once False it stays False) that also saves DB round-trips. No QSG change; no flag (gated by the existing
  `outer_join` flag on the whole stage).
- **Verified:** [`OuterJoinResultSameTest.py`](mysite/unmasque/test/OuterJoinResultSameTest.py) — 17 cases
  on the REAL methods, fake `app` faithfully implementing EXCEPT ALL over synthetic row bags: reordered /
  duplicate-reordered / NULL-extended-reordered rows that the old positional check mis-classifies →
  correctly **same**; differing multiplicity / disjoint / subset → **not same**; `same=False` short-circuit
  (zero DB calls); fail-closed on diff error; `__bag_diff_count` surplus count / semicolon-strip /
  parenthesised operands / empty-operand→None / degenerate-result→None. End-to-end on live TPC-H
  (`detect_oj` ON, each run own process): **(1)** FULL OUTER `nation/region` (`… r_name='AFRICA'`) →
  `… nation FULL OUTER JOIN region ON … and region.r_name = 'AFRICA'`, **correct=True**; **(2)** OQ6
  RIGHT OUTER multi-predicate `part/partsupp` (`p_size>4 and ps_availqty>3350`) →
  `… part RIGHT OUTER JOIN partsupp ON part.p_partkey=partsupp.ps_partkey and partsupp.ps_availqty>=3351
  and part.p_size>=5 Order By ps_suppkey Limit 10`, **correct=True** (139s). `public.*` intact
  (orders 1,500,000) before/after both.
- **Honest latency (cf. WI-01/05/06):** on the verified workloads the *old* positional check would also
  have succeeded — on the single-witness D¹ both Qh and the candidate share a deterministic ORDER BY, so
  positional ≡ bag there. The fix removes the **fragility** (row reordering, ORDER-BY ties, duplicates) and
  a NULL-handling sharp edge (EXCEPT ALL treats NULL=NULL natively), not a live mis-extraction. Its
  concrete payoff is being the **trustworthy equivalence check WI-11 requires** to wire outer joins ON by
  default. Full writeup:
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md#wi-03--robust-outer-join-candidate-equivalence-except-all-bag-diff).
- **Side-finding (out of scope, logged for WI-11/future):** with `gap_aware=yes`, the gap-witness Filter
  stage's `_build_qe` ([gap_witness.py:203](mysite/unmasque/src/core/gap_witness.py#L203)) comma-joins ALL
  FROM tables, so a numeric filter on a multi-table outer join builds an unbounded cartesian Re
  (`part × partsupp ≈ 1.6e11` rows) that never returns — and a `kill -9`'d client leaves the server-side
  backend running, locking the working schema. Verified OQ6 with `gap_aware` off (orthogonal to WI-03).

### WI-04 — Within-attribute equality OR → IN, cheap default-on path  `Easy`  ☐
- **Now:** `A=x OR A=y` works only when `or=yes` and re-runs the *whole* mutation pipeline per
  disjunct ([DisjunctionPipeLine.py:191](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py#L191));
  consolidated to IN at [genPipeline_context `__adjust_for_in_predicates`:302](mysite/unmasque/src/core/dataclass/genPipeline_context.py#L302),
  rendered at [QSG:479-493](mysite/unmasque/src/util/QueryStringGenerator.py#L479).
- **Do:** route same-`(tab,attr)` equality-union through the cheap gap-aware residual-domain
  search: after the first satisfying value via
  [`checkAttribValueEffect`](mysite/unmasque/src/core/filter.py#L154), keep binary-searching the
  residual domain (masking found values) for more disjoint singletons → accumulate as IN members.
  Keep the rerun loop as fallback for mixed equality+range disjuncts. No QSG change.
- **Risk/limit:** no natural termination signal for "# of disjuncts" under Pop — must bound
  iterations or sample distinct values from `user_schema` (can miss values absent from base data).
- **Verify by:** `l_shipmode='AIR' OR l_shipmode='RAIL'`.

### WI-05 — `const-1` vs `COUNT()==1` disambiguation  `Easy`  ✅ DONE (2026-06-02)
- **Now (before):** the const-1-vs-COUNT split at [groupby_clause.py:74-80](mysite/unmasque/src/core/groupby_clause.py#L74)
  is a *value* heuristic — it marks a column the literal `1` whenever every probed group showed `'1'`.
  Unsound in principle: a genuine `COUNT(*)`/`COUNT(col)` that reads 1 in every probed group is
  indistinguishable from a literal 1 by value alone. (Sound *in practice today* only because the probe
  synthesises the grouping column with the repeated delta `[0,1,1]` at
  [groupby_clause.py:39](mysite/unmasque/src/core/groupby_clause.py#L39), which incidentally always
  materialises a ≥2-row group for a real COUNT — an unrelated implementation detail; cf. WI-01's latent
  finding.)
- **Do:** resolve each candidate directly with a controlled duplicate-row probe (S2): on the single-group
  witness instance D¹, duplicate one contributing witness row. A COUNT tracks row multiplicity and rises
  1→2; the literal 1 is invariant and stays 1.
- **Depends on:** S2.
- **Risk:** group-key collisions could over-count — handled by probing on D¹ (exactly one group), so the
  duplicated tuple lands in that group and the signal (`max` over the lone result row) is undiluted.
- **Verify by:** `SELECT 1, … GROUP BY …` vs `SELECT COUNT(*) …` where count happens to be 1.
- **DONE — what shipped:** replaced the final const-1 application loop in
  [`groupby_clause.doExtractJob`](mysite/unmasque/src/core/groupby_clause.py) with
  `_confirm_const1_columns(query, const1_cols)` + the static helper `_max_int_in_col`. For each column the
  value-heuristic marked const-1, it `do_init()`s to D¹, reads the column's value, duplicates one witness
  row of `core_relations[0]` by ctid via [`RowProbe`](mysite/unmasque/src/core/row_probe.py) (S2), re-runs
  Qh, and reverts. Value grew → genuine COUNT (left empty-projected so
  [aggregation.py:241-243](mysite/unmasque/src/core/aggregation.py#L241) renders `COUNT(*)`); value
  unchanged → real literal 1 (set to `CONST_1_VALUE`). Degrades to the old verdict on any unreadable
  reading (no false positive on a real constant). No QSG change. No feature flag: the probe only fires
  when a const-1 candidate exists and cannot mis-flag a true literal 1 (its value can't grow).
- **Verified:** [`GroupByConst1Test.py`](mysite/unmasque/test/GroupByConst1Test.py) — 11 cases on the real
  `_confirm_const1_columns`/`_max_int_in_col` with a synthetic oracle modelling the exact case the old
  heuristic gets wrong (a COUNT reading 1 on the witness): COUNT-stuck-at-1 → reclassified to COUNT;
  literal 1 → kept; mixed; guards (no candidate, unreadable → const-1 default; targeted single-ctid dup).
  End-to-end on live TPC-H: `select o_orderpriority, 1 from orders group by o_orderpriority` →
  `Select o_orderpriority, 1 From orders Group By o_orderpriority Order By o_orderpriority desc;`,
  `pipeline.correct=True` — the DEBUG trace shows the Group_By stage's `INSERT … WHERE ctid IN ('(0,10)')
  … RETURNING ctid` then `DELETE … ctid='(0,11)'` (the S2 dup+revert) and **no** "reclassified" line
  (literal 1 correctly confirmed). `public.orders` 1,500,000 rows intact.

---

## Tier 2 — Moderate (high value, plan as focused tasks)

### WI-06 — `COUNT(DISTINCT col)` (+ companion `COUNT(col)` vs `COUNT(*)`)  `Moderate`  ✅ DONE (2026-06-02)
- **Now (before):** aggregation explicitly assumes no DISTINCT
  ([aggregation.py:133](mysite/unmasque/src/core/aggregation.py#L133)); no distinct flag anywhere.
  A COUNT has no value-dependency so projection leaves it empty-projected →
  [aggregation.py:241-243](mysite/unmasque/src/core/aggregation.py#L241) blanket-labels EVERY count
  `('', COUNT_STAR)` (so `count(distinct col)`, `count(col)`, `count(*)` were all emitted as `Count(*)`).
- **Theory (black-box):** refine each `COUNT_STAR`-labelled result index on the witness instance D¹.
  (1) **distinctness** — exact-duplicate one contributing witness row (S2): a non-distinct count rises
  1→2; `COUNT(DISTINCT)` sees a repeated value and stays 1 (the exact dup always survives Qh → reliable).
  (2) **if distinct, find the column** — insert a witness-copy whose ONLY change is a *fresh distinct*
  value in a candidate column; only the truly distinct-counted column lifts the count by one. (3) **if
  non-distinct (companion)** — null-inject a candidate column; `COUNT(*)` counts the row anyway,
  `COUNT(col)` skips the NULL (count unchanged), with a survival guard (a non-null fresh value in the
  same column MUST be counted) to rule out the dropped-row false positive.
- **Depends on:** WI-01 (COUNT branch carries an argument — shipped), S2 (RowProbe).
- **DONE — what shipped:** `COUNT_DISTINCT` sentinel + `count_distinct` feature flag (default OFF:
  [constants.py](mysite/unmasque/src/util/constants.py), [config.ini](mysite/config.ini),
  [configParser.py](mysite/unmasque/src/util/configParser.py)). New
  [`Aggregation._refine_counts`](mysite/unmasque/src/core/aggregation.py) (+ `_count_arg_candidates`,
  `_distinctness_probe`, `_identify_distinct_column`, `_identify_nonnull_count_column`,
  `_probe_count_with_col`, module-level `_max_int_in_result_col`) runs after the line-243 blanket label,
  gated by the flag. Candidates exclude GROUP BY keys and equi-join keys (perturbing them moves the row
  to another group / breaks the join → silent false negative). Stores `(col, COUNT_DISTINCT)` /
  `(col, COUNT)`; the agg-tuple **shape is unchanged** (still `(attrib, op)`, just new op values), so no
  predicate-consumer audit beyond the op. QSG renders `Count(distinct col)` via a dedicated branch
  (pulls col from the agg tuple, since the projected attribute is empty) and the column-COUNT `else`
  branch now falls back to the agg tuple's column when `elt` is empty
  ([QueryStringGenerator.py:608-640](mysite/unmasque/src/util/QueryStringGenerator.py#L608)); added
  `COUNT_DISTINCT` to QSG `AGGREGATES`. The order-by `COUNT in op` substring tests
  ([orderby_clause.py:117,127](mysite/unmasque/src/core/orderby_clause.py#L117)) treat the sentinel as a
  count (correct); `check_order_by_on_count` is dead (zero callers).
- **Verified:** [`CountDistinctAggTest.py`](mysite/unmasque/test/CountDistinctAggTest.py) — 17 cases on the
  REAL methods (synthetic oracle modelling COUNT semantics, no DB): COUNT(DISTINCT col) detected with the
  right column (incl. a decoy neighbour), COUNT(*) stays, COUNT(col) nullable → `(col, COUNT)`, DISTINCT
  over a join key / single-valued `=` filter → unidentified → stays COUNT(*), companion survival guard
  blocks the dropped-row false positive, no-count-cols no-op, candidate exclusions, distinctness-probe
  revert, `_max_int_in_result_col` + the QSG `Count(distinct col)` render. End-to-end on live TPC-H
  (`count_distinct=yes`, each run own process): `select o_orderstatus, count(distinct o_custkey) …` →
  `Select o_orderstatus, Count(distinct o_custkey) as count …` (**correct=True**); companion
  `count(o_orderkey)` → `Count(o_orderkey) as cnt` (**correct=True** — the WI-01 render fix is now
  OBSERVABLE, no longer collapsed to `Count(*)`); control `count(*)` → `Count(*) as cnt`
  (**correct=True**, probe found nothing). DEBUG trace shows the Aggregation S2 dup+revert
  (`INSERT … WHERE ctid IN ('(0,10)') RETURNING ctid::text` → `DELETE … ctid='(0,11)'`) and the
  null-inject sweep (one crafted row per candidate column with that column = NULL). `public.orders`
  1,500,000 rows intact before/after all three.
- **Limits (honest):** counted column must be perturbable — a column that is an equi-join key, the group
  key, or single-valued under an `=` filter can't be identified (left COUNT(*) with a logged note);
  `COUNT(DISTINCT)` over a unique key, and `COUNT(col)` on a non-null column, are *result-equivalent* to
  the simpler form so the companion is **latent** on TPC-H (like WI-01/05) — it improves faithfulness, not
  result-correctness, there. First cut scopes to a single base column (not an expression). Full writeup:
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md#wi-06--countdistinct-col--companion-countcol-vs-count).

### WI-07 — Boolean datatype in WHERE + graceful skip on unknown types  `Moderate`  ☐
- **Now:** [`get_datatype`](mysite/unmasque/src/core/abstract/un2_where_clause.py#L185-L194)
  recognizes only int/date/text/numeric and **raises `UnmasqueError`** otherwise (also
  [filter.py:169-176](mysite/unmasque/src/core/filter.py#L169)). One exotic column aborts the whole run.
- **Do:** (a) add a `bool` branch; two-point probe via
  [`checkAttribValueEffect`](mysite/unmasque/src/core/filter.py#L178-L205) — set True/observe Pop,
  set False/observe Pop; surviving value is the constant; emit `(tab,attr,'=',v,v)` (QSG `=` branch
  already renders it). (b) Replace the hard raise with **log-and-skip** for genuinely unhandleable
  types so extraction degrades gracefully instead of crashing.
- **Risk:** boolean is 3-state with NULL; a true-or-null column can't be cleanly separated under
  null-free Pop.
- **Verify by:** `WHERE is_active = true`.

### WI-08 — Timestamp / time datatype support  `Moderate`  ☐
- **Now:** `date` (whole-day) is first-class end-to-end; `timestamp`/`time`/`timestamptz`
  fall through to the `UnmasqueError` raise
  ([un2_where_clause.py:193-194](mysite/unmasque/src/core/abstract/un2_where_clause.py#L193)).
- **Do:** clone the proven DATE machinery into a parallel `timestamp` bucket — `get_datatype`
  branch (test before/disjoint from the `'date'` substring check), domain constants in
  [constants.py](mysite/unmasque/src/util/constants.py), `timedelta(seconds=…)` in the
  [utils.py](mysite/unmasque/src/util/utils.py) helpers + `get_constants_for`
  ([aoa_utils.py:473-482](mysite/unmasque/src/util/aoa_utils.py#L473)), a timestamp UPDATE
  template in [postgres_queries.py](mysite/unmasque/src/util/postgres_queries.py) routed in
  [filter.py:195-198](mysite/unmasque/src/core/filter.py#L195). Binary search is datatype-agnostic.
- **Observability limit:** sub-second/timezone precision is noisy at 1-s granularity;
  `+ interval '1' year` keyword form is **Infeasible** (we only see the resolved boundary, not
  the textual expression) — recover the literal only.
- **Verify by:** `WHERE ts BETWEEN '2024-01-01 09:00' AND '2024-01-01 17:00'`.

### WI-09 — uuid / bit / varbit / json equality-only point probe  `Moderate`  ☐
- **Do:** equality-only — read the d-min witness value from the base row and confirm Pop holds;
  emit `(tab,attr,'=',v,v)`. For json restrict to whole-column equality (no path extraction).
  Pairs with WI-07's graceful-skip fallback.
- **Risk:** marginal discriminating value (mostly confirms a constant already in d-min); json on a
  large column is brittle.

### WI-10 — Cross-attribute OR (`A=x OR B=y`, cross-table)  `Moderate`  ☐
- **Now:** detection essentially done — the falsify-and-rerun loop produces per-disjunct predicate
  tuples; the failure is a **hard raise** at
  [genPipeline_context.py:300](mysite/unmasque/src/core/dataclass/genPipeline_context.py#L300)
  (`ERROR_007`) for slots spanning >1 distinct `(tab,attr)`.
- **Do:** replace the raise with a structured `cross_attr_disjunctions` field (list of per-disjunct
  `(tab,attr,op,lb,ub)`); plumb to QSG exactly like `disjunctive_ranges`
  ([ExtractionPipeLine.py:231-232](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L231)); in
  [`__generate_arithmetic_pure_conjunctions`](mysite/unmasque/src/util/QueryStringGenerator.py#L542)
  emit one parenthesized `(t1.A=x OR t2.B=y)` group per slot reusing the gap-aware OR-join idiom at
  [QSG:570](mysite/unmasque/src/util/QueryStringGenerator.py#L570). Keep these OFF `arithmetic_filters`.
- **Depends on:** `or=yes` flag.
- **Observability limit:** if the single witness row satisfies both branches, the falsifier can
  mis-attribute which branch keeps Pop true (the trajectory-convergence issue) — moderate, not
  fundamental.
- **Verify by:** `WHERE o_orderstatus='F' OR o_totalprice=100`.

### WI-11 — Wire outer joins ON by default (route to JOIN…ON renderer)  `Moderate`  ✅ DONE (2026-06-02)
- **Now (before):** detection ([`__determine_join_edge_type`](mysite/unmasque/src/core/outer_join.py#L402)
  + `importance_dict` l/h markers) and renderer
  ([join_map](mysite/unmasque/src/util/QueryStringGenerator.py#L131) /
  [`generate_from_on_clause`](mysite/unmasque/src/util/QueryStringGenerator.py#L847)) both existed but
  only inside `OuterJoinPipeLine`, gated behind the off-by-default `outer_join` flag; the default
  factory chose `ExtractionPipeLine` → comma-FROM (= INNER) for every outer join. Routing was a fragile
  string heuristic (`q_candidate.count('OUTER')`).
- **Do:** after the join stage, if any edge has a non-symmetric `(l,h)/(h,l)/(h,h)` marker, build
  FROM via `generate_from_on_clause`; else keep comma-FROM. Guard so the two emitters never both
  fire. Enable + validate the nullability probe under the default pipeline.
- **Depends on:** WI-03 (trustworthy equivalence check).
- **Risk:** dangling row can be masked by aggregation/LIMIT → mis-mark outer as inner; FULL OUTER
  doubles probe count.
- **DONE — what shipped:** (1) **Principled routing.** Replaced the substring guard with a marker-based
  decision [`_seq_routes_to_join_on`](mysite/unmasque/src/core/outer_join.py#L358): route to JOIN…ON iff
  some edge marker ≠ `('l','l')`; all-`('l','l')` keeps comma-FROM (cf. WI-01: principled marker over a
  substring scan of the output). [`__formulateQueries`](mysite/unmasque/src/core/outer_join.py#L319)
  `continue`s on the inner case (never touches q_gen → comma-FROM baseline stands) and on the outer case
  `clear_from_where_ops()` BEFORE `generate_from_on_clause` (wipes comma-FROM → only JOIN…ON) — the
  **exactly-one-emitter guard**, by construction. (2) **Hardening.**
  [`__determine_join_edge_type`](mysite/unmasque/src/core/outer_join.py#L402) now defaults the marker
  pair to `('l','l')` (sound INNER→comma-FROM direction) for an edge absent from `importance_dict`,
  instead of the latent `UnboundLocalError` (harmless while opt-in, a crash now that it is default). (3)
  **Default-on:** `outer_join = yes` ([config.ini](mysite/config.ini)) + `self.detect_oj = True`
  ([configParser.py](mysite/unmasque/src/util/configParser.py)) → factory picks `OuterJoinPipeLine`,
  stage `enabled`, adaptive null-tolerant executable + NEP `re_E` path all key off the one flag. No QSG
  change, no new flag, no predicate-shape change. Safe because the WI-03 EXCEPT-ALL bag oracle re-checks
  every candidate under the join-break/NULL-inject mutations and fails closed — false *outer* is the
  dangerous direction and the marker probe + oracle must both agree to upgrade.
- **Verified:** [`OuterJoinRouteTest.py`](mysite/unmasque/test/OuterJoinRouteTest.py) — 12 cases on the
  REAL methods: routing verdict for every marker pair, mixed/all-inner multi-edge, reversed-edge lookup,
  missing-edge default (hardening), and the exactly-one-emitter guard on a real `QueryStringGenerator`
  (JOIN…ON replaces the comma list, not appends) — all pass; WI-03 `OuterJoinResultSameTest` (17) stays
  green. End-to-end on live TPC-H **under the DEFAULT config** (harness does NOT override `detect_oj` —
  it reads `yes` from config.ini, proving on-by-default; `gap_aware` off; each run own process): **(1)**
  FULL OUTER `nation/region` (`r_name='AFRICA'`) → `nation FULL OUTER JOIN region ON … and
  region.r_name='AFRICA'`, **correct=True** (173s); **(2)** INNER control `nation,region` → comma-FROM
  kept (`From nation, region Where …`), **correct=True** (110s, *no regression*); **(3)** RIGHT OUTER
  `nation/customer` (`c_acctbal<1000`) → commuted `customer LEFT OUTER JOIN nation ON … and
  customer.c_acctbal<=999.99` (A RIGHT JOIN B ≡ B LEFT JOIN A; WI-03 oracle accepted the equivalent
  survivor), **correct=True** (112s). `public.*` intact (orders 1,500,000) across all three; no orphaned
  backends.
- **Limits (honest):** real before/after (not latent) — corrects the *emission* unconditionally; the
  result-difference shows when data has dangling rows. Costs the nullability probe on every multi-table
  query (bails to comma-FROM on single-table / no-join-graph / projection-free relation). A relation that
  projects nothing can't be probed → falls back to comma-FROM (that's WI-12); dangling row masked by
  aggregation/LIMIT → false-inner (safe direction). **⚠ Shipped-default footgun:** `outer_join=yes` +
  `gap_aware=yes` (both now default) makes the gap-witness Filter stage cartesian-join all FROM tables on
  a numeric filter over a multi-table outer join (`_build_qe`, [gap_witness.py:203](mysite/unmasque/src/core/gap_witness.py#L203))
  → hangs; e2e ran with `gap_aware` off. Highest-priority follow-up (scope the gap-witness `Re` to the
  attribute's own table). Full writeup:
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md#wi-11--wire-outer-joins-on-by-default-route-to-the-joinon-renderer).

### WI-12 — Outer join where optional side has NO projected column  `Moderate`  ☐
- **Now:** [`__create_table_attrib_dict`](mysite/unmasque/src/core/outer_join.py#L179-L199) bails
  ("does not have any direct projection! … Bye.") if a relation projects nothing → falls back to
  INNER. Breaks anti-join / existence-style outer joins.
- **Do:** replace the projected-attribute NULL-witness with a **row-existence witness**: after
  breaking the edge, compare result row-count before/after and/or ctid-bisect the optional table
  (gap_witness style) to see whether a non-matching base row survives (→ side is `h`/preserved) or
  vanishes (→ `l`). Feeds the existing importance encoding; no QSG change.
- **Risk:** view-minimizer D¹ may lack a guaranteed dangling tuple — may need to materialize one
  (cs2-style), risking perturbation of other stages.

### WI-13 — Outer-join ON-vs-WHERE for TEXT/LIKE predicates  `Moderate`  ☐
- **Now:** [`__determine_on_and_where_filters`](mysite/unmasque/src/core/outer_join.py#L351-L360)
  only iterates `all_arithmetic_filters` (numeric/date); text-equality & LIKE never get ON/WHERE
  classification → emitted in WHERE, which turns LEFT JOIN into effectively INNER. The project's own
  O3 workload query (`… ON c_custkey=o_custkey AND o_orderstatus='F'`) hits this.
- **Do:** widen the candidate list to include filter-stage text/LIKE/`=` predicates and reuse the
  existing NULL-mutation discriminator
  [`__check_on_or_where`](mysite/unmasque/src/core/outer_join.py#L362-L370). Render path already
  handles it ([`generate_from_on_clause`](mysite/unmasque/src/util/QueryStringGenerator.py#L833) appends arbitrary fp).
- **Risk:** NOT-NULL text columns resist NULL-mutation; single-row D¹ makes the `len==1` test brittle.

### WI-14 — UNION vs UNION ALL (dedup discrimination)  `Moderate`  ✅ DONE (2026-06-02)
- **Now (before):** [UnionPipeLine.py:63](mysite/unmasque/src/pipeline/UnionPipeLine.py#L63) always joined
  branches with `UNION ALL`; no bag-vs-set probe.
- **Do:** engineer data so a branch emits a duplicate output row (S2 duplication), run `Qh`, count rows:
  grows → `UNION ALL`, unchanged → `UNION`. Conditional join token in `__post_process`. QSG untouched
  (set-op wrapping lives in UnionPipeLine).
- **Depends on:** `union=yes`, S2.
- **Observability limit:** if `D` has no cross-branch duplicate, UNION and UNION ALL are
  indistinguishable for that `D` — default to UNION ALL and log.
- **DONE — what shipped:** new probe [`SetOpProbe`](mysite/unmasque/src/core/set_op_probe.py) (a
  `GenerationPipeLineBase` subclass, so it inherits the real `do_init()` D¹-reset + the singleton `Qh`
  executable). The [`UnionPipeLine`](mysite/unmasque/src/pipeline/UnionPipeLine.py) branch loop, **while a
  branch is still isolated** (others nullified), resets it to D¹, counts `Qh` rows (`c0`), duplicates one
  contributing witness row via [`RowProbe`](mysite/unmasque/src/core/row_probe.py) (S2), recounts (`c1`),
  reverts: `c1>c0` → `UNION ALL` (growth is impossible under set semantics → unconditional proof);
  `c1==c0` → `UNION`; else undecided. `_resolve_set_op` combines per-branch verdicts (default `UNION ALL`;
  `UNION` only on positively-observed dedup; conflicts → safe default); `__post_process` takes the token,
  and its single-branch unwrap guard was changed from the fragile `"UNION ALL" not in u_Q` substring to
  `len(u_eq) <= 1` (the substring test would wrongly unwrap a multi-branch *bare-UNION* query).
- **Key correction to the original framing (the discovery):** for a true `UNION`, the group-by stage
  **spuriously infers a per-branch `GROUP BY` on all projected columns** (the union's dedup reads as
  grouping); the pre-WI-14 emission was therefore `DISTINCT(b1) ⊎ DISTINCT(b2)` joined by `UNION ALL` —
  result-correct only when branches are cross-branch-disjoint, wrong on overlaps. So the gate skips a
  branch only on a **genuine aggregate** (which absorbs the duplicate regardless of operator); a bare
  `GROUP BY`-no-aggregate is still probed. This is sound because **the probe runs `Qh` itself, not our
  extracted branch** — it sees the real operator through the spurious GROUP BY. No GROUP-BY stripping
  needed: `(distinct b1) UNION (distinct b2) ≡ b1 UNION b2 = Qh`.
- **Verified:** [`SetOpDedupTest.py`](mysite/unmasque/test/SetOpDedupTest.py) — 23 cases on the REAL
  methods (fake `app` scripting `Qh` counts + fake `RowProbe`): `1→2`⇒UNION ALL, `1→1`⇒UNION, self-join
  `1→4`⇒UNION ALL, empty/exec-fail/dup-fail⇒undecided (revert still happens), `_resolve_set_op` table,
  `__branch_is_probeable` (aggregate⇒skip, GROUP BY-no-agg⇒probe), `__post_process` token + unwrap. E2e on
  live TPC-H (`union=yes`, `oj=yes`, `gap_aware` off, each own process): the **same two OJ branches** run
  as `UNION ALL` → Q_E emits `UNION ALL` (correct=True, 218s) and as `UNION` → Q_E emits **bare `UNION`**
  (correct=True, 146s); DEBUG trace shows the witness dup **survives 1→2 under UNION ALL** and **collapses
  1→1 under UNION**. `public.*` intact (orders 1,500,000), 0 orphaned backends.
- **Limits (honest):** latent on the *original* OJ-branch `D` (branches cross-branch-disjoint → both
  tokens result-equal, so the win is faithful emission; the result-difference shows on overlapping data).
  The clean overlap demo (`n_regionkey from nation union r_regionkey from region`, 30 vs 5) was blocked by
  an **orthogonal** union-FROM-clause bug on bare single-table branches — now **FIXED** (see the
  **Union FROM-clause junk-relation fix** entry below: [`_as_relation_list`](mysite/unmasque/src/core/union_from_clause.py#L62)),
  so that demo now extracts and is a *non-latent* correctness flip (5 rows via bare `UNION` vs the
  pre-WI-14 `DISTINCT(nation) UNION ALL DISTINCT(region)` = 10). Blind spot:
  a genuine per-branch `SELECT DISTINCT` under `UNION ALL` reads as `UNION` (rare; WI-21 territory).
  Mixed operators unrepresentable (single global token) → safe default. Full writeup:
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md#wi-14--union-vs-union-all-set-operator-dedup-discrimination).

### WI-15 — Strict vs non-strict single-sided inequality (`<` vs `<=`)  `Moderate`  ☐
- **Now:** filter always appends inclusive tuples (`<=`/`>=`,
  [filter.py:790-799](mysite/unmasque/src/core/filter.py#L790)); QSG has no strict column-vs-const op.
- **Do:** after `get_filter_value` finds the boundary `val`, add a strictness probe via
  `checkAttribValueEffect`: Pop at exactly `val` → inclusive; Pop false at `val` but true at
  `val∓delta` → strict. Add `'lt'`/`'gt'` op tokens (audit all predicate consumers per rule 6) and
  render branches in
  [`formulate_predicate_from_filter`](mysite/unmasque/src/util/QueryStringGenerator.py#L495).
- **Observability limit:** on continuous numeric the constant is only localizable to `delta=0.01`
  (un_precision), so strictness is recoverable but the exact bound is not; on discrete int/date,
  `<c` ≡ `<= c-1` so strict is cosmetic. Guard against sibling-predicate interference (false strict).
- **Verify by:** TPC-H Q4/Q6 `o_orderdate < date '1995-03-15'`.

### WI-16 — ORDER BY on aggregate  `Moderate`  ☐
- **Now:** COUNT-ordering partly wired; [`check_order_by_on_count`](mysite/unmasque/src/core/orderby_clause.py#L91-L109)
  is **dead code** (zero callers); SUM/AVG/MIN/MAX ordering and aggregates absent from SELECT are
  undetected.
- **Do:** revive + generalize `check_order_by_on_count` to all of {COUNT,SUM,AVG,MIN,MAX}: synthesize
  per-group row multisets with monotone aggregate magnitudes + a reversed second DB, reuse the 2-DB
  swap + [`check_sort_order`](mysite/unmasque/src/core/orderby_clause.py#L54-L60). Borrow group-value
  enumeration from [limit.py:91-155](mysite/unmasque/src/core/limit.py#L91). No QSG change.
- **Observability limit:** an aggregate NOT in SELECT can't be read at `obj.index` → unobservable.
  Ordinal `ORDER BY 2` is observationally identical to by-name → no extraction value (skip).

### WI-17 — Type-aware / expandable numeric domain bounds (bigint, edge-vs-no-bound)  `Moderate`  ☐
- **Now:** [`get_min_and_max_val`](mysite/unmasque/src/util/utils.py#L217-L225) hardwires int32
  `±2147483648`; a real constant at the int32 edge is dropped as "no bound"
  ([QSG:513-520](mysite/unmasque/src/util/QueryStringGenerator.py#L513)); bigint constants
  undiscoverable.
- **Do:** per-type bounds (bigint `±2^63`); for numeric use an **outward-doubling pre-probe**
  (grow the bracket 2,4,8,… via `checkAttribValueEffect` until Pop fails) to bracket the real range
  before bisecting. Disambiguate edge-vs-open by probing `max+1`: Pop true → open-ended; flips →
  real bound, emit it.
- **Risk:** "true max" for unbounded numeric is conceptual — cap it; `max+1` must not overflow the type.

### WI-18 — OFFSET detection  `Moderate`  ☐
- **Now:** OFFSET exists only as internal ctid paging
  ([MinimizerBase.py:165-168](mysite/unmasque/src/core/abstract/MinimizerBase.py#L165)); no slot,
  no stage; a hidden OFFSET also corrupts the LIMIT card-count.
- **Do:** only when a deterministic ORDER BY was found — insert N>k+offset strictly-ordered
  distinguishable rows (limit.py enumeration), observe *which* rows appear: first `o` absent,
  `o+1..o+limit` present → OFFSET=`o`. Co-extract with LIMIT (LIMIT card-count assumes window starts
  at row 1). Emit via the S1 `offset_op` slot.
- **Depends on:** S1, a recovered ORDER BY (reorder so OFFSET sees `ob.orderby_list`).
- **Observability limit:** without a total order the skipped set is nondeterministic → **Infeasible**
  for no-ORDER-BY queries; ties must be broken by strictly-monotone synthesized data.

### WI-19 — Non-equi / theta join (cross-table inequality ON)  `Moderate→Hard`  ☐
- **Now:** only equality ops admitted as join edges
  ([equi_join.py:29-30](mysite/unmasque/src/core/equi_join.py#L29)); cross-table inequalities go to
  AOA only as ordering chains over already-equijoined groups.
- **Do:** AOA's [`__create_dashed_edges`](mysite/unmasque/src/core/aoa.py#L547) already forms edges
  across tables without a same-table restriction; add a classifier that (1) finds AOA tuples whose
  both endpoints are `(tab,attr)` on *different* tables, (2) confirms it's a real join edge by setting
  the two columns to VIOLATE the order on D¹ and requiring Pop to drop, (3) records `theta_join_edges`,
  (4) renders into JOIN…ON via `generate_from_on_clause` (treat as INNER) — `get_aoa_string` already
  emits `t1.a <= t2.b`.
- **Observability limit (important):** for *inner* joins, `a.x<b.y` in ON vs WHERE is
  Pop-indistinguishable → the inner case is **already result-equivalent** (cosmetic). The real win is
  *outer* theta joins. Single-row D¹ starves the order probe and yields high false-positive rate on
  incidental orderings — gate behind a flag, require the violation re-probe. Banded `BETWEEN`-joins
  stay Partial (need two coordinated edges). **Treat with caution; verify hard.**

### WI-20 — Cross-join / cartesian validation + labeling  `Moderate`  ☐
- **Now:** a cross product surfaces only as comma-FROM with no join predicate; never confirmed/labeled.
- **Do:** for a table pair with no equi/theta edge, run a row-scaling probe (S2 / CardinalityProbe):
  duplicate a row of `a`, recount `|Qh|`; multiplicative growth `~|a|×|b|` confirms cross product;
  cross-check with [`MultiplicityProbe.run`](mysite/unmasque/src/core/multiplicity_probe.py#L51).
  On confirm, keep the result-equivalent comma-FROM or emit explicit `CROSS JOIN`.
- **Observability limit:** **conflates with the k=2 self-join's 4× band**
  ([cardinality_probe.py:46-47](mysite/unmasque/src/core/cardinality_probe.py#L46)); a selective
  WHERE on top, or any aggregation/DISTINCT, destroys the multiplicative signal. Deliverable is a
  *confidence flag*, not new result-correctness. Inner cross join is already result-equivalent.

---

## Tier 3 — Hard (feasible but ambitious; each is its own project)

### WI-21 — SELECT DISTINCT detection  `Hard`  ☐
- **Now:** no detection; no token. Only a free-text warning at
  [multiplicity_probe.py:110](mysite/unmasque/src/core/multiplicity_probe.py#L110).
- **Do:** probe on a *multi-row* instance (NOT D¹): duplicate the base rows feeding the kept
  projection (S2) so the pre-DISTINCT result would have >1 identical projected tuple; if the
  projected-row count does NOT increase **and** there is no aggregation/GROUP-BY on those columns,
  set distinct. Emit `Select distinct` via S1.
- **Depends on:** S1, S2.
- **Observability limit:** strongly confounded — GROUP BY on all projected cols, an aggregate, a
  unique key in projection, or `LIMIT 1` all mimic the same collapse. Disambiguation is partial →
  false +/−. `DISTINCT ON` is harder still (per-subset probe + ordering oracle), likely Infeasible.

### WI-22 — HAVING predicate  `Hard`  ☐
- **Now:** no post-aggregation stage; no slot; pipeline goes group_by → aggregate → order_by.
- **Do:** new stage after aggregate. Build a controlled multi-group instance (generalize
  `insert_for_inner` to N groups with KNOWN distinct aggregate values); run `Qh`; observe which
  groups survive ([`get_all_nullfree_rows`](mysite/unmasque/src/core/groupby_clause.py#L86) signal).
  A HAVING threshold = groups below/above a cutoff disappear; binary-search the cutoff by steering a
  group's aggregate (insert more/larger rows); infer op from inclusive/exclusive boundary. Emit via S1.
- **Depends on:** S1, reliable aggregate labels.
- **Observability limit:** must attribute group-disappearance to HAVING (not WHERE / group-not-formed)
  via Pop-precheck; HAVING on an aggregate also in WHERE is confounded; AVG inversion is rounding-lossy;
  HAVING on an aggregate absent from SELECT → only group-survival observable, no magnitude.

### WI-23 — Column alias (AS) verification  `Hard`  ☐
- **Now:** alias only opportunistically harvested from the cursor-description column name; QSG emits
  `expr as name` only when name ≠ source ([QSG:621-625](mysite/unmasque/src/util/QueryStringGenerator.py#L621)).
- **Do:** add a verification probe — reuse the subquery-wrap primitive
  ([projection.py:142-153](mysite/unmasque/src/core/projection.py#L142)) `SELECT <name> FROM (<Qh>) sub`
  to confirm `<name>` is a real referenceable output label; stop suppressing aliases that equal the
  base column name when the probe proves the name is user-supplied.
- **Observability limit (fundamental):** `AS <samename>` is observationally identical to no alias →
  unrecoverable; unnamed expressions collapse to `?column?`/ORPHAN → no name. Improves the
  distinct-alias case only.

### WI-24 — Self-join degree k≥3  `Hard`  ☐
- **Now:** hard-capped at k=2 ([cardinality_probe.py:216-221](mysite/unmasque/src/core/cardinality_probe.py#L216),
  [equi_join.py:186-191](mysite/unmasque/src/core/equi_join.py#L186)); MultiplicityProbe only warns.
- **Do:** generalize `_promote_to_k2`→`_promote_to_k(m)` with a degree estimator (predict ratio `~m^k`,
  sweep m∈{2,3}, fit `k=round(log ratio/log m)` instead of the fixed 3.5–4.5 band); emit a1..ak and the
  cross-alias edge set (QSG render already generalizes); wire MultiplicityProbe to escalate not warn.
- **Observability limit:** degree from a cardinality ratio is data-dependent (skew/filters/aggregation
  corrupt it); k-way edge wiring may admit multiple topologies the oracle can't disambiguate.

### WI-25 — Affine arithmetic predicate: two-column offset on AOA edge (`a.x + k <= b.y`)  `Hard`  ☐
- **Now:** bare `col<col` handled; coefficient/offset arithmetic not. The AOA `coeff`
  ([aoa.py:202](mysite/unmasque/src/core/aoa.py#L202)) is only a ±1 step direction; magnitude discarded.
- **Do:** in [`__absorb_variable_LBs/UBs`](mysite/unmasque/src/core/aoa.py#L266) record the numeric gap
  (`new_lb - val`) as the recovered offset `k`, attach to the AOA tuple, extend `get_aoa_string` to
  render `tab.a + k <= tab.b`. Reuses the existing per-edge mutation loop.
- **Sub-cases:** single-column `col+k op const` is **Infeasible** (offset folds into the constant —
  unidentifiable from data). General `m·col`/multi-term needs multi-point boundary fitting (Hard, accuracy-limited).
- **Observability limit:** recovered gap conflates predicate offset with the mutation delta step —
  disentangle carefully to avoid off-by-delta.

### WI-26 — Algebraic inequalities among attributes (`a + b <= c`, `const < a+b`)  `Hard`  ☐
- **Now:** AOA edge model is strictly single-node; no multi-term term anywhere.
- **Do:** reuse projection's linear-solve idea but against the boolean oracle — fix a,b at concrete
  values (group-UPDATE), binary-search c to the Pop-boundary, collect n+1 affinely-independent
  boundary points, solve the hyperplane with numpy + `nsimplify`
  ([projection.py:314,38](mysite/unmasque/src/core/projection.py#L314)), validate by flipping Pop at
  interior/exterior points. New multi-term predicate shape + `get_aoa_string` extension. (Const-vs-
  expression is the same machinery with a literal RHS → the intercept is the constant.)
- **Observability limit:** combinatorial subset enumeration (needs datatype/domain pruning);
  cross-predicate interference on the single witness row; distinguishing `a+b<=c` from `a<=k1 AND b<=k2`
  needs off-axis points the constrained witness may not afford; linear-only; `<` vs `<=` to delta.

### WI-27 — GROUP BY on (linear) expression  `Hard`  ☐
- **Now:** GroupBy only iterates base-column names; QSG emits bare names.
- **Do:** detect via a collision probe — insert two rows that differ on base column `c` but the engine
  still collapses them into one group → grouping is on a function of `c`. For the LINEAR case reuse the
  projection `solution` for the co-projected column as the group-key expression; let
  `global_groupby_attributes` carry expression strings emitted verbatim
  ([QSG:664-669](mysite/unmasque/src/util/QueryStringGenerator.py#L664)).
- **Observability limit:** `GROUP BY a+b` vs `GROUP BY (a,b)` need injected collision pairs (blocked if
  filters forbid the values); a group key NOT also projected gives the collapse signal but no
  coefficients to render; non-linear/function keys need an enumerated candidate set (else misattributed).

### WI-28 — Restricted single-level DNF (OR of conjunctive terms)  `Hard`  ☐
- **Now:** flat conjunction only; OR confined to single-`(tab,attr)` groups.
- **Do:** generalize the falsify-and-rerun loop so each iteration extracts a full conjunctive *term*,
  then falsifies the ENTIRE term before the next; render as OR of parenthesized AND-groups. Needs a
  DNF-term IR distinct from the flat `arithmetic_filters`.
- **Observability limit:** mostly Infeasible in general — with a single witness row only ONE term is
  active; recovering all terms needs a distinct witness per term (count unknown, non-terminating);
  `(A∧B)∨(A∧C)` ≡ `A∧(B∨C)` is Pop-indistinguishable (only a normal form is recoverable). CNF + arbitrary
  nesting: **out of scope**.

### WI-29 — Scalar function in SELECT (fixed catalog)  `Hard`  ☐
- **Now:** projection models columns only as polynomial/multilinear arithmetic.
- **Do:** post-Projection sub-stage (flag-gated), only for columns the Ax=b solver could not fit.
  Hypothesis-test-by-mutation at several controlled input points against a CLOSED catalog:
  numeric {round(x,d) sweep d, floor, ceil, abs, x·const}, string {upper/lower via case toggle, length,
  substring via marker injection}. On a confirmed match store the rendered call (QSG emits the string verbatim).
- **Observability limit:** combinatorial for composed/multi-arg functions; many functions agree on a
  single D¹ row (needs multiple input points → re-inflating the column, fragile when it's also
  filtered/joined). Single-arg, closed-catalog only.

### WI-30 — String functions in predicates (UPPER/LOWER/TRIM/SUBSTRING-window)  `Hard`  ☐
- **Do:** reuse the per-character mutation loop already in
  [`__handle_for_wildcard_char_underscore`](mysite/unmasque/src/util/QueryStringGenerator.py#L746) /
  [`__try_with_temp`](mysite/unmasque/src/util/QueryStringGenerator.py#L733): case-only mutation keeps
  Pop → case-insensitive (`upper()=`/ILIKE); whitespace-only → TRIM; tail-vs-window mutation localizes
  SUBSTRING(col,1,k). Wrap the column in the detected function when rendering.
- **Observability limit:** each function is a bespoke probe; open-ended space; concatenation in
  projection is unobservable via the numeric solver; composed functions out of reach.

### WI-31 — Date functions EXTRACT/date_part  `Hard`  ☐
- **Do:** `EXTRACT(year FROM d)=k` shows as a **periodic/banded** acceptance region. Reuse gap-aware
  disjoint-interval discovery ([gap_witness.py](mysite/unmasque/src/core/gap_witness.py),
  `disjunctive_ranges`); add a calendar-alignment recognizer that maps interval edges to
  year/month/dow boundaries and collapses them to `EXTRACT(field)=k`.
- **Observability limit:** data-hungry (needs multiple periods in `D`); ambiguous against a hand-written
  OR-of-yearly-ranges. `now()`/`CURRENT_DATE` is **Infeasible** (environment-dependent, not a function of DB state).

### WI-32 — NULLS FIRST/LAST  `Hard`  ☐
- **Do:** after asc/desc is fixed, a dedicated NULLS probe inserts a row with a genuine NULL in the
  sort column (all OTHER output columns non-null so the row isn't discarded), read via a **null-tolerant
  raw result path** (bypass [`get_all_nullfree_rows`](mysite/unmasque/src/core/orderby_clause.py#L257));
  classify FIRST/LAST vs the Postgres default; append modifier (S1/verbatim).
- **Observability limit:** the deep null-free invariant fights this; only the non-default placement on a
  nullable column is detectable; NOT-NULL columns make it impossible. Low frequency — low priority.

### WI-33 — INTERSECT  `Hard`  ☐
- **Do:** UNION's nullify-and-observe FROM partition does NOT generalize (nulling any arm empties an
  intersection). Needs a bespoke arm-discovery probe; confirm with Comparator `EXCEPT ALL`:
  `(Qe_A INTERSECT Qe_B) EXCEPT ALL Qh` and reverse both empty. New two-arm rendering.
- **Observability limit:** weak partition signal; easily confused with an inner join over the union of tables. Fragile.

### WI-34 — EXCEPT  `Hard`  ☐
- **Do:** asymmetric signal (unlike INTERSECT) — nulling arm B grows the result toward A;
  nulling arm A empties it. Extend [algorithm1](mysite/unmasque/src/core/algorithm1.py) nullify loop to
  track row-count *direction*; classify additive (A) vs subtractive (B) arm; confirm with EXCEPT ALL diff.
- **Observability limit:** arm order matters (`A EXCEPT B ≠ B EXCEPT A`); if B subtracts nothing in `D`,
  EXCEPT ≡ plain A.

### WI-35 — Uncorrelated subquery-as-scalar-threshold (WHERE)  `Hard`  ☐
- **Do:** filter already recovers the effective threshold literal; to learn it's a *subquery*, mutate the
  candidate inner relation T and observe whether the recovered WHERE endpoint MOVES (a real
  mutation-sensitivity signal via `checkAttribValueEffect`+rerun). The inner aggregate must be
  SUM/AVG/MIN/MAX/COUNT. Needs a new parenthesized-SELECT node in QSG (absent).
- **Observability limit:** only the scalar-threshold-in-WHERE shape; derived tables and IN/ANY subqueries
  are indistinguishable from base scans / IN-lists under Pop.

### WI-36 — EXISTS / NOT EXISTS (uncorrelated)  `Hard`  ✅ DONE (2026-06-03)
- **Was (☐ plan, anchors STALE):** nullify candidate gate relation T (`__nullify_relations`); if Pop
  flips empty while T supplies no projected/joined columns → EXISTS gate (NOT EXISTS = inverse). The
  cited anchor was wrong (the actual classifier is `from_clause.get_core_relations_by_void/by_error`),
  and the "NOT EXISTS = inverse" framing was also wrong (corrected below).
- **What shipped — DETECTION.** New [`ExistsGateProbe`](mysite/unmasque/src/core/exists_gate_probe.py)
  (a `GenerationPipeLineBase` subclass, modelled on WI-14's `SetOpProbe`) owns the decisive
  **non-scaling** check. Reclassification is driven by
  [`ExtractionPipeLine._reclassify_exists_gates`](mysite/unmasque/src/pipeline/ExtractionPipeLine.py)
  (+ `__reclassify_exists_gates_impl`, `_gate_projected_tables`, `_gate_joined_tables`,
  `_strip_gate_relations`), run right after the Limit stage and before the q_generator is configured.
  A core relation `T` is an uncorrelated EXISTS gate iff **all four** hold:
  (1) **load-bearing** — guaranteed by core membership (from_clause kept it);
  (2) **non-projecting** — `T` is in no `Projection.dependencies` entry (read the per-relation
      attribution, since `projected_attribs` stores only the column name);
  (3) **non-joining** — `T` is in no equi-join edge (`aoa.algebraic_eq_predicates`) nor AOA/theta edge
      (`aoa.aoa_predicates` / `aoa.aoa_less_thans`);
  (4) **NON-SCALING** — *the decisive discriminator vs a CROSS JOIN* (which also empties the result when
      emptied): duplicate one contributing `T` row on D¹ (shared S2 [`RowProbe`](mysite/unmasque/src/core/row_probe.py))
      and recount Qh. **`|Qh|` unchanged ⇒ gate; grows ⇒ cross/inner join** (kept in FROM). Fail-closed:
      any inconclusive/`None` verdict, or any exception, leaves `T` in core_relations (status quo).
  On a gate verdict, `T` is pulled out of `core_relations` / `instances` / `alias_to_table` so it never
  reaches FROM.
- **What shipped — EMISSION.** New `exists_gates` field on
  [`QueryStringGenerator`](mysite/unmasque/src/util/QueryStringGenerator.py) (`QueryDetails.exists_gates`
  + property, plumbed like `disjunctive_ranges`). `__generate_where_clause` appends a
  `<kind> (SELECT 1 FROM T [WHERE <inner preds>])` conjunct via `__generate_exists_gate_clauses` /
  `__collect_gate_inner_predicates` (reusing the existing `formulate_predicate_from_filter` for the inner
  WHERE), and `__generate_arithmetic_pure_conjunctions` now SKIPS any predicate whose tab is a gate (its
  filter tuples belong inside the subquery, not the outer WHERE — `T` is no longer in FROM). No new
  top-level QSG slot needed — it's a WHERE conjunct.
- **Flag:** `exists` ([config.ini](mysite/config.ini) `[feature]`, default **OFF**; `DETECT_EXISTS` in
  [constants.py](mysite/unmasque/src/util/constants.py); `detect_exists` in
  [configParser.py](mysite/unmasque/src/util/configParser.py)). Off by default: extra probes + a
  correlated-subquery false-positive risk.
- **Verified:** [`ExistsGateTest.py`](mysite/unmasque/test/ExistsGateTest.py) — 26 cases on the REAL
  methods (probe non-scaling/scaling/undecided/revert; `_gate_projected_tables`/`_gate_joined_tables`;
  the fail-closed reclassification loop incl. a real-`PGAOcontext` regression guard for the write-only
  `aggregate` property; QSG EXISTS/NOT-EXISTS/no-inner-pred/no-gate rendering), all green; full
  regression sweep (CountRender, CountDistinctAgg, SetOpDedup, OuterJoinRoute/ResultSame, GroupByConst1,
  RowProbe, LimitSearch) green. **E2e on live TPC-H** (`exists=yes`, `detect_oj=off` so the plain
  ExtractionPipeLine runs, `gap_aware=off`, each own process), all **correct=True**:
  - **EXISTS (the deliverable):** `select n_name from nation where exists (select 1 from region where
    r_regionkey > 2)` → `Select n_name From nation Where EXISTS (SELECT 1 FROM region WHERE
    region.r_regionkey >= 3);` — DEBUG trace: dup region witness `(0,4)` → `|Qh|` **1→1** ⇒ gate, then revert.
  - **CROSS-JOIN control (must NOT be EXISTS):** `select n_name from nation, region` → `Select n_name
    From nation, region;` (comma-FROM kept) — probe: region dup `|Qh|` **1→2** ⇒ cross join. Same region
    relation, opposite verdict — condition (4) is the discriminator.
  - **NOT EXISTS:** `select n_name from nation where not exists (select 1 from region where r_regionkey >
    9)` → `Select n_name From nation Where EXISTS (SELECT 1 FROM region WHERE region.r_regionkey <= 9);`.
  `public.*` intact (orders 1,500,000; lineitem 6,001,215) before/after all three; 0 orphaned backends.
- **NOT EXISTS — honest finding (corrects the checklist's "= inverse" framing).** The default app_type is
  `SQL_ERR_FWD`, so from_clause uses the **error method** (rename the relation away → REL_ERROR ⇒ core),
  not the void method. So the NOT EXISTS gate relation is **kept core** (it is referenced in the subquery)
  — it is NOT invisible as the original plan assumed. The filter then recovers the **complement** of the
  inner predicate (the witness must FAIL the inner predicate for the gate to be open: `>9` ⇒ recovered
  `<=9`), and WI-36 emits a positive `EXISTS(complement)`. This is **bag-equivalent on the extraction D**
  for *every extractable* NOT EXISTS gate, because an uncorrelated NOT EXISTS(P) yields a non-empty outer
  result only when **no** T row satisfies P (∀¬P), under which EXISTS(¬P) is likewise true. WI-36 therefore
  reconstructs a **result-correct** query but does NOT recover the **polarity** (EXISTS vs NOT EXISTS is
  unobservable on the single witness D¹ — the witness satisfies the recovered predicate either way), and
  the emitted EXISTS(¬P) would diverge from NOT EXISTS(P) on a database where T's rows straddle P.
  **Specified future path** (clean, not yet shipped): a polarity probe — INSERT a T-row that violates the
  recovered predicate Q (satisfies ¬Q); under EXISTS(Q) the witness keeps the gate open (`|Qh|` unchanged),
  under NOT EXISTS(¬Q) the inserted row closes it (`|Qh|` → 0) — then emit `NOT EXISTS(¬Q)`.
- **Observability limits (honest):** (a) **correlated EXISTS** is Infeasible — on D¹ the correlation
  column has a fixed witness value, so the filter records it as a constant `=` and a correlated gate is
  indistinguishable from an uncorrelated one (default-OFF flag mitigates the false-positive risk).
  (b) A **semi-join to a UNIQUE key every outer row matches** is result-equivalent to an inner join on many
  D — but such a gate has a join edge ⇒ fails (3) ⇒ kept as a join (no spurious EXISTS). (c) A blanket
  global aggregate (COUNT/SUM, no GROUP BY) collapses `|Qh|` to one row, defeating the (4) row-count signal
  — but such queries typically also defeat from_clause's emptiness classifier, so they rarely reach here.
  (d) Verified under the plain ExtractionPipeLine; composing with the OuterJoin post-processor (which
  independently rebuilds FROM from `genPipelineCtx`, still holding the gate) is future integration — no
  shipped-default conflict since `exists` is OFF by default.

### WI-37 — Scalar subquery in SELECT  `Hard`  ☐
- **Do:** a (uncorrelated) scalar subquery is a CONSTANT output column; the projection solver fits it as a
  constant term. To recognize it as a subquery, mutate candidate inner relation T and see whether the
  projected constant tracks `max/sum/...` of a T column (read-back via
  [`get_nullfree_row`](mysite/unmasque/src/core/projection.py#L305)); reconstruct the inner aggregate.
  New parenthesized-SELECT projection node.
- **Observability limit:** constant-valued only; correlated is Infeasible.

### WI-38 — CASE expression (2-branch, single-column condition)  `Hard`  ☐
- **Do:** the TPC-H `sum(CASE WHEN col<thr THEN x ELSE y END)` shape. Inject values across a suspected
  breakpoint; a single linear fit that fails but two distinct fits on either side of a threshold signal a
  CASE branch; threshold via filter binary search. New CASE node in QSG.
- **Observability limit:** needs multi-row probe DB spanning the breakpoint; general/nested CASE explodes
  combinatorially — 2-branch single-column only.

### WI-39 — Composite-key / extra ON equality (outer joins)  `Hard`  ☐
- **Do:** treat each equi-edge AND each `attr=const` as an ON-candidate, run the
  [`__check_on_or_where`](mysite/unmasque/src/core/outer_join.py#L362) NULL-mutation discriminator per
  candidate; for composite keys emit multiple ANDed equalities (join graph already supports multi-vertex
  edges). Fix the AOA else-branch that assumes `<=` so `=` between two attrs isn't mislabeled
  ([outer_join.py:331](mysite/unmasque/src/core/outer_join.py#L331)). Validate with Comparator.
- **Observability limit:** ON-constant vs WHERE-constant on the preserved side is ambiguous under Pop on a
  single matching D¹ row.

### WI-40 — Outer/any join on a non-key column (upstream equi-join discovery)  `Hard`  ☐
- **Now:** outer join is driven entirely by `global_join_graph`; a join on columns the equi-join stage
  didn't register can't be made outer.
- **Do:** this is an **equi-join-stage** gap — strengthen edge discovery (value-coincidence probing across
  attribute pairs under mutation) so the edge appears in the graph; the existing outer-join pass then works.
- **Observability limit:** non-key-pair search explodes and risks false edges; weak black-box signal for
  spurious-vs-real on non-key columns. Lower priority.

---

## Ranking snapshot (impact × ease)

- **Do first (Easy):** WI-01 ⭐ ✅ (correctness bug) → WI-02 ✅ → WI-05 ✅ → WI-04 → **WI-03 ✅**. Plus
  enablers S1, **S2 ✅**.
- **High-value Moderate:** **WI-06 ✅** (COUNT DISTINCT + companion COUNT(col)), **WI-11 ✅** (wire outer
  joins ON by default), **WI-14 ✅** (UNION vs UNION ALL dedup), WI-10 (cross-attr OR), WI-07/08
  (datatypes), WI-18 (OFFSET).
- **Then the rest of Moderate**, then Hard items as standalone projects (WI-22 HAVING and WI-38 CASE are the
  highest-value Hard items).
- **Hard items landed:** **WI-36 ✅** (uncorrelated EXISTS gate detect+emit; NOT EXISTS reconstructed
  result-correct as EXISTS-complement, polarity recovery specified as future work).

---

## Explicitly EXCLUDED — fundamentally infeasible under black-box Pop (do NOT attempt)

These require query-text access or are unobservable/ambiguous under the single-bit, null-free Pop oracle.
Recording them so future chats don't re-litigate.

- **Correlated subqueries** — unobservable (single-row D¹; correlation needs per-outer-row evaluation).
- **Arbitrary nested boolean / CNF** — only logically-equivalent normal forms are recoverable; structure is lost.
- **Window / analytic functions (OVER, ROW_NUMBER, RANK, PARTITION BY)** — observability severely constrained.
- **`IS NULL` on a projected column** — null-free Pop classifies the whole query as empty; no signal.
  (`IS NOT NULL` on a NON-projected column is at best a Hard detect-and-warn; not scheduled above.)
- **`COALESCE`** — null-free filtering hides it; mostly cosmetic w.r.t. Pop.
- **`CAST` / type coercion** — usually semantics-preserving w.r.t. Pop; cosmetic.
- **`SELECT *`** — only the expanded column list is recoverable; the star is cosmetic.
- **`now()` / `CURRENT_DATE`** — environment/time-dependent, not a function of mutable DB state.
- **`+ interval '1' year` keyword fidelity** — only the resolved boundary literal is observable.
- **Single-column `col + k op const` offset** — offset folds into the constant; unidentifiable from data.
- **Ordinal `ORDER BY 2`** — observationally identical to by-name ordering; no extraction value.
- **CROSS JOIN as distinct from a missed join** — comma-FROM is already result-equivalent; only a
  confidence flag (WI-20) is meaningful, not a hard distinction.

---

## Thesis / final report plan (write after implementations land)

> **Accumulator file:** write each item up in thesis-ready form in
> [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md) *as we finish it* (problem → approach →
> implementation → proof → limits → takeaway). The final thesis is assembled from that file. WI-01 is
> already written up there as the first case study + template.

For each implemented item capture, while it's fresh (fill the **Log** lines above as we go):
1. **Before example** — a concrete `Qh` the framework previously mis-extracted, with the wrong `Q_E`
   (or the abort/exception) it produced.
2. **After example** — the same `Qh`, the correct `Q_E` we now emit, and the `Qh ≡ Q_E` bag-equality proof.
3. **The approach** — the mutation experiment and the Pop/result-shape signal that makes it observable
   (emphasize the black-box discipline: what we mutated, what we measured, why the signal is sound).
4. **Limits** — the observability boundary we hit (data-dependence, single-witness ambiguity, precision/delta),
   stated honestly.
5. **Where it lives** — files/functions touched, the flag that gates it, the test added.

Structure the document as: framework recap (mutation + Pop model, staged pipeline, QSG ceiling) →
per-construct before/after → a coverage table (this checklist, final state) → observability-limit
discussion (what is provably unrecoverable and why) → future work.

---

## Progress log (newest first)

- _2026-06-03_ — **WI-36 ✅ DONE (uncorrelated EXISTS gate detect+emit; NOT EXISTS reconstructed
  result-correct).** New [`ExistsGateProbe`](mysite/unmasque/src/core/exists_gate_probe.py) (modelled on
  WI-14 `SetOpProbe`) + [`ExtractionPipeLine._reclassify_exists_gates`](mysite/unmasque/src/pipeline/ExtractionPipeLine.py)
  run after the Limit stage: a core relation that is (1) load-bearing, (2) non-projecting
  (`Projection.dependencies`), (3) non-joining (`aoa` edges), and (4) **non-scaling** (dup one of its rows
  via S2 [`RowProbe`](mysite/unmasque/src/core/row_probe.py) → `|Qh|` unchanged ⇒ gate; grows ⇒ cross/inner
  join) is pulled out of `core_relations`/`instances`/`alias_to_table` and declared as an `exists_gates`
  entry; QSG appends `EXISTS (SELECT 1 FROM T WHERE <inner preds>)` to the WHERE and excludes T's own filter
  predicates from the outer WHERE (`__generate_exists_gate_clauses` + gate-tab skip). Condition (4) is the
  decisive discriminator vs a cross join (WI-20), which also empties the result when emptied but **scales**
  when a row is duplicated. New `exists` flag (default **OFF**: extra probes + correlated-subquery false-positive
  risk). Fail-closed throughout (any inconclusive/exception keeps T in FROM). **Discovery that corrects the
  plan:** the checklist's WI-36 anchor (`__nullify_relations`) was stale, and "NOT EXISTS = inverse" was wrong
  — the default `SQL_ERR_FWD` app_type means from_clause uses the **error method**, so a NOT EXISTS gate
  relation is **kept core** (referenced in the subquery), NOT invisible; the filter recovers the **complement**
  of the inner predicate and WI-36 emits a positive `EXISTS(¬P)` that is bag-equivalent on D for *every
  extractable* NOT EXISTS (an uncorrelated NOT EXISTS(P) is non-empty only when ∀¬P, under which EXISTS(¬P) is
  also true). So NOT EXISTS is **result-correct** but its **polarity is not recovered** (unobservable on the
  single witness D¹); a clean polarity probe (insert a ¬Q-row: gate stays open ⇒ EXISTS, closes ⇒ NOT EXISTS)
  is specified as future work. **Bug fixed mid-dev:** `_gate_projected_tables` read `PGAOcontext.aggregate`,
  a write-only property whose getter raises `NotImplementedError` (and `getattr` does NOT suppress it) — it
  aborted extraction; fixed to read `aggregated_attributes`, with a real-`PGAOcontext` regression test.
  Verified: [`ExistsGateTest.py`](mysite/unmasque/test/ExistsGateTest.py) (26 cases on the real methods) +
  full regression sweep green; e2e on live TPC-H all **correct=True** — `exists(region where r_regionkey>2)`
  → `EXISTS (SELECT 1 FROM region WHERE region.r_regionkey >= 3)` (probe `|Qh|` 1→1); cross-join control
  `nation, region` → comma-FROM kept (probe `|Qh|` 1→2); `not exists(region where r_regionkey>9)` →
  `EXISTS (SELECT 1 FROM region WHERE region.r_regionkey <= 9)`. `public.*` intact (orders 1,500,000;
  lineitem 6,001,215), 0 orphaned backends. Verified under the plain ExtractionPipeLine (`detect_oj=off`);
  OuterJoin-pipeline composition is future integration (OJ rebuilds FROM from genPipelineCtx). Full writeup in
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md#wi-36--uncorrelated-exists--not-exists-gate).
  Next suggested: the WI-36 **polarity probe** (faithful NOT EXISTS) or **WI-20** (cross-join confidence flag,
  shares the S2 scaling primitive), or **WI-10** (cross-attr OR).
- _2026-06-02_ — **BUGFIX ✅ Union FROM-clause junk-relation (status-string char-split) — unblocks
  single-table UNION branches + the non-latent WI-14 overlap demo.** Root cause: for a bare single-table
  single-column UNION branch the arms share **no** common relation, so
  [`UnionFromClause.get_comTabs`](mysite/unmasque/src/core/union_from_clause.py#L83) →
  `FromClause.doJob(QH, TYPE_RENAME)` finds zero core relations, `FromClause.doActualJob` raises
  `UnmasqueError(ERROR_006)`, and [`Base.doJob`](mysite/unmasque/src/core/abstract/ExtractorBase.py#L36)
  swallows it and returns the status string `OK` (`"OK "`). The old code stored that string verbatim, so
  `set("OK ")` → `{'O','K',' '}` and — the real damage — [`algorithm1.algo`](mysite/unmasque/src/core/algorithm1.py#L26)'s
  `for ct in comtabs` char-iterated it, injecting `'O'/'K'/' '` into **every** branch partition →
  `_after_from_clause_extract` errors on those junk relations → extraction aborts. **Fix:** static
  [`_as_relation_list`](mysite/unmasque/src/core/union_from_clause.py#L62) normalises any non-`list`
  `doJob` result (the `"OK "` string, `False`, or an exception string) to `[]`, applied in both
  `get_comTabs` and `get_fromTabs` — an empty common-table set is the **expected** outcome for disjoint
  single-table branches, not an error. One normalisation fixes all three consumers
  (`set([])`=∅, `for ct in []`, `nullify_except`'s `difference`). Black-box preserved (pure
  nullify-and-observe, no query parsing). Verified: [`UnionFromClauseTest.py`](mysite/unmasque/test/UnionFromClauseTest.py)
  — 10 cases on the REAL `get_comTabs`/`get_fromTabs`/`doActualJob`/`algorithm1.algo` path, all green;
  proven **non-vacuous** (stubbing the helper to identity reproduces the `{'O','K',' '}` junk in every
  partition); `SetOpDedupTest` (23) still green. E2e on live TPC-H (`union=yes`, `oj=yes`, `gap_aware`
  off, each own process): `n_regionkey from nation union all r_regionkey from region` → `UNION ALL`,
  **correct=True** (30 rows, 144.6s); `… union …` → **bare `UNION`**, **correct=True** (5 rows, 195.5s).
  Both **previously aborted** with ERROR_006. This is the **non-latent** WI-14 demo: regionkeys fully
  overlap, so bare `UNION`=5 rows but the pre-WI-14 emission `DISTINCT(nation) UNION ALL DISTINCT(region)`
  =10 rows — the token flip changes correctness. `public.*` intact (orders 1,500,000); 0 orphaned
  backends. Addendum appended to the WI-14 section of
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md#wi-14--union-vs-union-all-set-operator-dedup-discrimination).
  Next suggested: the **gap_aware × multi-table footgun** (scope `_build_qe` Re to the attribute's own
  table) or **WI-10** (cross-attr OR) / **WI-07** (boolean + graceful-skip).
- _2026-06-02_ — **WI-14 ✅ DONE (UNION vs UNION ALL dedup discrimination).** Replaced the hardcoded
  `"\n UNION ALL "` join token in [`UnionPipeLine.__post_process`](mysite/unmasque/src/pipeline/UnionPipeLine.py#L63)
  with a probed decision. New [`SetOpProbe`](mysite/unmasque/src/core/set_op_probe.py) (a
  `GenerationPipeLineBase` subclass for the real `do_init()` D¹-reset + singleton `Qh` executable): while a
  branch is isolated (others nullified), reset to D¹, count `Qh` rows, duplicate one contributing witness
  row (S2 [`RowProbe`](mysite/unmasque/src/core/row_probe.py)), recount, revert — `c1>c0` ⇒ `UNION ALL`
  (growth impossible under set semantics → unconditional), `c1==c0` ⇒ `UNION`, else undecided.
  `_resolve_set_op` combines verdicts (default `UNION ALL`; `UNION` only on positively-observed dedup;
  conflicts → safe default); `__post_process` single-branch unwrap changed `"UNION ALL" not in u_Q` →
  `len(u_eq) <= 1` (the substring test wrongly unwraps a multi-branch bare-UNION). **Discovery that shaped
  the design:** the group-by stage spuriously infers a per-branch `GROUP BY` all-cols for a `UNION` query
  (dedup reads as grouping), so the pre-WI-14 emission was `DISTINCT(b1) ⊎ DISTINCT(b2)` under `UNION ALL`
  — correct only on cross-branch-disjoint data. The gate (`__branch_is_probeable`) therefore skips only on
  a **genuine aggregate** (not on a bare GROUP BY); sound because the probe runs **`Qh` itself**, immune to
  our extraction's spurious GROUP BY, and flipping just the token suffices
  (`(distinct b1) UNION (distinct b2) ≡ Qh`). Verified:
  [`SetOpDedupTest.py`](mysite/unmasque/test/SetOpDedupTest.py) (23 cases on the real methods) all green;
  e2e on live TPC-H — the **same two OJ branches** as `UNION ALL` → Q_E `UNION ALL` (correct=True, 218s)
  and as `UNION` → Q_E **bare `UNION`** (correct=True, 146s); DEBUG trace shows the witness dup **surviving
  1→2 under UNION ALL** and **collapsing 1→1 under UNION**. `public.*` intact (orders 1,500,000), 0
  orphaned backends. Honest: latent on this `D` (branches disjoint → both tokens result-equal; faithful
  emission, result-fix on overlapping data); the clean overlap demo
  (`n_regionkey union r_regionkey`, 30 vs 5) is blocked by an **orthogonal** existing union-FROM-clause bug
  on bare single-table branches (`get_comTabs` status-string char-split into junk relations —
  [union_from_clause.py:73](mysite/unmasque/src/core/union_from_clause.py#L73)); blind spot = genuine
  per-branch `SELECT DISTINCT` under `UNION ALL` (WI-21). No QSG change; gated by `union` flag. Full
  writeup in [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md). Next suggested: the **union
  FROM-clause `get_comTabs` junk-relation fix** (unblocks single-table union branches + the overlap demo),
  then **WI-10** (cross-attr OR) or **WI-07/08** (datatypes).
- _2026-06-02_ — **WI-11 ✅ DONE (wire outer joins ON by default → JOIN…ON renderer).** The
  detection (nullability probe → `importance_dict` l/h markers) and the JOIN…ON renderer
  (`generate_from_on_clause`/`join_map`) already existed, but only inside `OuterJoinPipeLine`, gated
  behind the off-by-default `outer_join` flag — so the default factory picked `ExtractionPipeLine` and
  emitted comma-FROM (= INNER) for every outer join. WI-11 is the **emission/routing half** (cf. WI-01):
  (1) replaced the fragile `q_candidate.count('OUTER')` string heuristic in
  [`__formulateQueries`](mysite/unmasque/src/core/outer_join.py#L319) with a principled marker-based
  decision [`_seq_routes_to_join_on`](mysite/unmasque/src/core/outer_join.py#L358) (route to JOIN…ON iff
  some edge marker ≠ `('l','l')`; else keep comma-FROM); the inner route `continue`s untouched (comma-FROM
  baseline stands) and the outer route `clear_from_where_ops()` before building JOIN…ON — the
  **exactly-one-emitter guard**. (2) Hardened
  [`__determine_join_edge_type`](mysite/unmasque/src/core/outer_join.py#L402) to default to `('l','l')`
  on a missing edge (was a latent `UnboundLocalError`, a crash now that this is the default path). (3)
  Flipped `outer_join = yes` ([config.ini](mysite/config.ini)) + `self.detect_oj = True`
  ([configParser.py](mysite/unmasque/src/util/configParser.py)). Safe because WI-03's EXCEPT-ALL bag
  oracle re-checks every candidate and fails closed (false *outer* is the dangerous direction). Verified:
  [`OuterJoinRouteTest.py`](mysite/unmasque/test/OuterJoinRouteTest.py) (12 cases on the real routing
  methods incl. exactly-one-emitter on a real QSG) + WI-03's 17 green. E2e on live TPC-H **under the
  default config** (harness does NOT set `detect_oj` — reads `yes` from config.ini → on-by-default;
  `gap_aware` off): FULL OUTER `nation/region` → JOIN…ON `correct=True`; INNER control `nation,region` →
  comma-FROM kept `correct=True` (no regression); RIGHT OUTER `nation/customer` → commuted `customer LEFT
  OUTER JOIN nation` `correct=True`. `public.*` intact (orders 1,500,000). Honest: a real before/after
  (corrects the emission unconditionally; result-difference shows on dangling-row data), costs the
  nullability probe on every multi-table query; projection-free optional side → comma-FROM (WI-12);
  dangling masked by agg/LIMIT → false-inner (safe). ⚠ Shipped-default footgun now live: `outer_join=yes`
  + `gap_aware=yes` → gap-witness cartesian hang on numeric filter over multi-table OJ
  ([gap_witness.py:203](mysite/unmasque/src/core/gap_witness.py#L203)) — highest-priority follow-up. Full
  writeup in [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md). Next suggested: the **gap_aware
  ×multi-table** fix (now default-relevant), then **WI-14** (UNION dedup, reuses S2) or **WI-10**
  (cross-attr OR).
- _2026-06-02_ — **WI-03 ✅ DONE (robust outer-join candidate equivalence).** Replaced the
  order/duplicate-fragile positional row-equality in
  [`OuterJoin.__are_the_results_same`](mysite/unmasque/src/core/outer_join.py#L253) with the proven Re/Rh
  diff primitive (`Comparator.is_match` semantics): two results are equivalent iff `(Qh EXCEPT ALL Q_E)`
  and `(Q_E EXCEPT ALL Qh)` are both empty. New helper `__bag_diff_count` runs the diff **in-stage** via
  `app.doJob` (the gap_witness inline `select count(*) from ((left) except all (right)) as T` pattern) so
  it sees the join-breaking / NULL-injection mutation the caller applied to D¹ — `Comparator.match` can't
  be used because it restores tables to `user_schema` first. Fails **closed** (rejects) on any diff error
  (false-positive is the dangerous direction → never emit an unverified outer join); short-circuits when
  `same` is already False. No QSG change; gated by the existing `outer_join` flag. Verified:
  [`OuterJoinResultSameTest.py`](mysite/unmasque/test/OuterJoinResultSameTest.py) — 17 cases on the real
  methods (reorder/dup/NULL-extended → same; differing multiplicity/disjoint/subset → not-same;
  short-circuit; fail-closed; `__bag_diff_count` parse/strip/None paths) all green. E2e on live TPC-H
  (`detect_oj` ON, each own process): FULL OUTER `nation/region` → `correct=True`; OQ6 RIGHT OUTER
  multi-predicate `part/partsupp` → `correct=True` (139s); `public.*` intact (orders 1,500,000). Honest
  latency (like WI-01/05/06): on these single-witness workloads the old positional check would also pass
  (shared deterministic ORDER BY ⇒ positional ≡ bag); the win is removing reorder/tie/duplicate fragility
  + native NULL=NULL handling, and it is the trustworthy equivalence check **WI-11** (wire outer joins ON
  by default) depends on. Side-finding (out of scope): `gap_aware=yes` makes the gap-witness Filter stage
  cartesian-join all FROM tables on a multi-table outer join (`part × partsupp ≈ 1.6e11` rows) → hangs;
  OQ6 verified with `gap_aware` off (orthogonal to WI-03). Full writeup in
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md). Next suggested: **WI-04** (within-attr
  OR→IN) or **WI-11** (wire outer joins ON by default, now unblocked), or **WI-14** (UNION dedup).
- _2026-06-02_ — **WI-06 ✅ DONE (COUNT(DISTINCT col) + companion COUNT(col) vs COUNT(*)).** Added a
  flag-gated COUNT-refinement pass to the Aggregation stage
  ([`_refine_counts`](mysite/unmasque/src/core/aggregation.py) and helpers) that splits the blanket
  `('', COUNT_STAR)` label (which a count gets because it has no value-dependency for projection) into the
  real construct via S2 multiplicity probes on D¹: (1) exact-dup the witness — a non-distinct count rises,
  `COUNT(DISTINCT)` stays; (2) for distinct, insert a witness-copy with a fresh distinct value in a
  candidate column — only the counted column lifts the count; (3) for non-distinct, null-inject a
  candidate column with a survival guard to split `COUNT(col)` (nullable) from `COUNT(*)`. New
  `COUNT_DISTINCT` sentinel + `count_distinct` flag (default OFF); QSG renders `Count(distinct col)` and
  the column-COUNT path now falls back to the agg tuple's column. Candidates exclude group/join keys.
  Verified: [`CountDistinctAggTest.py`](mysite/unmasque/test/CountDistinctAggTest.py) (17 cases on the real
  methods, synthetic oracle) — all green; per-module regressions green in isolation. End-to-end on live
  TPC-H: `count(distinct o_custkey)` → `Count(distinct o_custkey)`, `count(o_orderkey)` →
  `Count(o_orderkey)` (WI-01 now OBSERVABLE), `count(*)` → `Count(*)` (control), all `correct=True`,
  `public.orders` 1,500,000 intact. DEBUG trace shows the S2 dup+revert and the per-column null-inject
  sweep. Honest limit (like WI-01/05): on TPC-H's non-null/unique columns the companion is *latent*
  (result-equivalent), and a counted column that is a join/group key or `=`-filtered single value can't be
  identified. Full writeup in [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md). Next suggested:
  **WI-04** (within-attr OR→IN) or **WI-03** (Comparator outer-join equivalence), then **WI-14** (UNION
  dedup) which reuses the same S2 multiplicity channel.
- _2026-06-02_ — **S2 ✅ + WI-05 ✅ DONE (one unit: enabler + first consumer).** Factored the
  duplicate→count→revert pieces that were copy-pasted inside CardinalityProbe / MultiplicityProbe into
  one shared [`RowProbe`](mysite/unmasque/src/core/row_probe.py) (S2; no new oracle — the count reuses
  `app`, which shares the stage's connection, so an uncommitted dup INSERT is seen by the next `app.doJob`
  and the DELETE reverts it). Retrofitted both owners to delegate; left CardinalityProbe `_count_qh`
  untouched. WI-05: replaced the *value* heuristic for const-1-vs-COUNT in
  [`groupby_clause`](mysite/unmasque/src/core/groupby_clause.py) with `_confirm_const1_columns`, an S2
  duplicate-row probe on the single-group D¹ — a COUNT rises 1→2 under a duplicated contributing row, a
  literal 1 stays 1 — making the classification sound instead of relying on the incidental `[0,1,1]`
  synthesis delta. Verified: [`RowProbeTest.py`](mysite/unmasque/test/RowProbeTest.py) (10 unit + 1
  live-DB ctid-set-restored) and [`GroupByConst1Test.py`](mysite/unmasque/test/GroupByConst1Test.py) (11
  cases on the real methods) all pass; e2e on live TPC-H — `select o_orderpriority, 1 from orders group
  by o_orderpriority` → `… , 1 … Order By o_orderpriority desc` (`correct=True`, DEBUG trace shows the
  Group_By dup-by-ctid + revert, **no** reclassification) and the control `… count(*) as order_count …`
  → `… Count(*) as order_count …` (`correct=True`, probe correctly never fires). `public.orders`
  1,500,000 rows intact. Full writeups in
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md). Note (like WI-01): the misclassification
  is **latent** under today's synthesis — the fix closes the unsoundness and is the reusable basis for
  WI-06/14/22/23. Next suggested: **WI-04** (within-attr OR→IN) or **WI-03** (Comparator outer-join
  equivalence).
- _2026-06-01_ — **WI-02 ✅ DONE.** Replaced the fixed `no_rows` linear insert-scan in
  [limit.py](mysite/unmasque/src/core/limit.py) with an exponential-then-binary search
  (`__probe_limit_card` + `__search_limit`), recovering the LIMIT in O(log L) probes with inserts
  bounded by ~2L instead of the budget. Limit is read from the **plateau value** (`L+1`,
  normalization-invariant), not the boundary count (`L−1`); proven on real data. Emission unchanged.
  Added permanent [LimitSearchTest.py](mysite/unmasque/test/LimitSearchTest.py) (9 cases, incl.
  L=5000, no-limit→None, group-bounded edge, logarithmic probe count). End-to-end on live TPC-H:
  `Limit 1500` (past the old 1000 cap) and `Limit 10` both extracted with `pipeline.correct=True`;
  DB (`public`) intact. Honest reframing: the cap is now *cheap to raise*, not removed (detectable
  range ≈ `no_rows`; the win is O(log L) cost + recovering the `L=no_rows` boundary). Writeup in
  [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md). Next suggested: enabler **S2**, then
  **WI-04** or **WI-03**.
- _2026-06-01_ — **WI-01 ✅ DONE.** Fixed the COUNT render gate in
  [QueryStringGenerator.py:610-619](mysite/unmasque/src/util/QueryStringGenerator.py#L610) (import
  `COUNT_STAR`; gate short-circuit on `== COUNT_STAR`). Added
  [CountRenderTest.py](mysite/unmasque/test/CountRenderTest.py) (4 passing cases). End-to-end run on
  live TPC-H confirmed no regression (`COUNT(*)`→`Count(*)`, `pipeline.correct=True`) and surfaced a
  key finding: the fix is **latent** — the pipeline reconstructs `count(col)` as `count(*)` today
  (projection finds no value-dependency for count cols → line-243 `COUNT_STAR` override). The
  observable `Count(col)` win is gated on the **nullable-column null-injection detection** noted under
  WI-06. DB (`public`) verified intact. Next suggested action: enabler **S2**, then **WI-04** or **WI-02**.
- _2026-06-01_ — Checklist created from the code-verified coverage audit.

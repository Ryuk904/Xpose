# XPOSE_NEW — Checklist Coverage Status Report

> **What this is.** A standalone, code-verified status read of the extraction-coverage
> work defined in [checklist.md](checklist.md) (*"XPOSE_NEW — Extraction Coverage
> Implementation Checklist"*). It tells you what the checklist sets out to do, what has
> actually shipped, what remains, and what is deemed fundamentally unrecoverable — without
> reading the raw checklist.
>
> **Distinct document.** This is **not** [COVERAGE_EXPANSION_REPORT.md](COVERAGE_EXPANSION_REPORT.md)
> (the thesis-source per-item writeups) nor [GAP_AWARE_AND_SELF_JOIN_THESIS.md](GAP_AWARE_AND_SELF_JOIN_THESIS.md).
> It cross-references both but stands alone.
>
> **Verification basis.** Every ✅/shipped claim below was re-checked against the actual
> source on **2026-06-05** by a 9-agent code audit that opened each cited file/function.
> **Result: all 9 shipped items are real and behave as claimed.** The checklist's *behavioral*
> descriptions hold; what failed audit is its *line-number anchors*, which have drifted as
> `aggregation.py` / `QueryStringGenerator.py` grew. Every drift is recorded in the
> **Discrepancies found** callout under each item. This report never contradicts the code —
> where the checklist and code disagree, the code wins and the disagreement is flagged.

---

## 1. Executive summary

The checklist is a cross-chat tracker for **extending what the extraction pipeline can recover
from a black-box hidden query `Qh`**. Detection is always a *mutation experiment*: we change the
database, run `Qh`, and read the result through the **Pop oracle** (is the result non-empty and
null-free?) plus row-count / result-shape / read-back-value signals — **never by parsing the query
text**. An item counts as **"Supported" only when two layers both change**: a `core/*` stage
**detects** the construct by mutation, *and* [QueryStringGenerator](mysite/unmasque/src/util/QueryStringGenerator.py)
(QSG) **emits** it into the reconstructed SQL. Detection without emission is broken; emitting what
we cannot observe is unsound.

Two structural ceilings shape every item. First, the **QSG six-slot output ceiling**:
[`assembleQuery`](mysite/unmasque/src/util/QueryStringGenerator.py#L87-L95) renders exactly
`Select / From / Where / Group By / Order By / Limit`. Anything needing a new clause (DISTINCT,
HAVING, OFFSET, NULLS, set-op wrapping) first needs a new slot — that is **shared enabler S1**, and
it is **not yet built**. Second, two oracle *tensions*: the **minimization tension** — the View
Minimizer collapses the database to a single witness row `D¹`, so every *multiplicity* phenomenon
(DISTINCT, UNION-dedup, HAVING, self-join degree, COUNT semantics) is invisible on one row and must
be probed on a controlled multi-row instance (**shared enabler S2**, which *is* built); and the
**null-free tension** — Pop rejects any row with a null in a projected column, which makes
`IS NULL`, `COALESCE`, and `NULLS FIRST/LAST` hard-to-infeasible because the oracle itself hides the
signal.

As of this report **8 of the 40 work-items (WI-01…WI-40) are verified shipped**, plus the **S2**
enabler and one orthogonal **Union FROM-clause bugfix**. The shipped work clusters in the Easy tier
(4/5) and the high-value Moderate/Hard joins-and-set-ops area (outer joins on-by-default, UNION vs
UNION ALL, uncorrelated EXISTS). The single biggest unblocker still open is **S1**, which gates four
distinct future items (DISTINCT, HAVING, OFFSET, NULLS).

---

## 2. Coverage dashboard

Tier = the item's own difficulty label. **Status** reflects the code audit, not just the
checklist marker. Sorted by ID within the checklist's structural grouping.

### Shared enablers

| ID | Title | Tier | Status | One-line summary |
|----|-------|------|--------|------------------|
| S1 | QSG clause-slot infrastructure | Moderate | ☐ Not started | Add `distinct` / `having` / `offset` / `nulls` output slots; blocks WI-18/22/23/33. |
| S2 | Controlled multi-row / duplicate-row probe primitive | Easy | ✅ Verified | [`RowProbe`](mysite/unmasque/src/core/row_probe.py) — duplicate/delete-by-ctid + count; the basis for every multiplicity probe. |

### Tier 1 — Quick wins (Easy)

| ID | Title | Tier | Status | One-line summary |
|----|-------|------|--------|------------------|
| WI-01 | Fix `COUNT(col)` rendering bug | Easy | ✅ Verified *(latent by default)* | Substring gate `COUNT in label` → exact `== COUNT_STAR`; correct but dormant unless `count_distinct` flag on. |
| WI-02 | Lift LIMIT=1000 cap via exponential+binary search | Easy | ✅ Verified | Geometric-then-binary row-insert search recovers LIMIT in O(log L); reads `plateau−1`. |
| WI-03 | Robust outer-join candidate equivalence (Comparator EXCEPT ALL) | Easy | ✅ Verified | Positional row-compare → both-directions `EXCEPT ALL` bag diff, fail-closed. |
| WI-04 | Within-attribute equality OR → IN, cheap default-on path | Easy | ☐ Not started | Route same-`(tab,attr)` equality-union through residual-domain search → IN list. |
| WI-05 | `const-1` vs `COUNT()==1` disambiguation | Easy | ✅ Verified | Duplicate-row probe: literal 1 stays 1, a COUNT rises 1→2. |

### Tier 2 — Moderate

| ID | Title | Tier | Status | One-line summary |
|----|-------|------|--------|------------------|
| WI-06 | `COUNT(DISTINCT col)` (+ companion `COUNT(col)` vs `COUNT(*)`) | Moderate | ✅ Verified | Distinctness / fresh-value / null-inject probes split the blanket `COUNT(*)` label. |
| WI-07 | Boolean datatype in WHERE + graceful skip on unknown types | Moderate | ☐ Not started | Two-point True/False probe; replace hard type-raise with log-and-skip. |
| WI-08 | Timestamp / time datatype support | Moderate | ☐ Not started | Clone DATE machinery into a `timestamp` bucket; literal-only (no interval keywords). |
| WI-09 | uuid / bit / varbit / json equality-only point probe | Moderate | ☐ Not started | Confirm the d-min witness value via Pop; emit `=` only. |
| WI-10 | Cross-attribute OR (`A=x OR B=y`, cross-table) | Moderate | ☐ Not started | Replace `ERROR_007` raise with a `cross_attr_disjunctions` field plumbed to QSG. |
| WI-11 | Wire outer joins ON by default (route to JOIN…ON renderer) | Moderate | ✅ Verified *(default-on)* | Marker-based routing replaces a substring scan; `outer_join=yes` by default. |
| WI-12 | Outer join where optional side has NO projected column | Moderate | ☐ Not started | Row-existence (ctid-bisect) witness instead of a NULL-projection witness. |
| WI-13 | Outer-join ON-vs-WHERE for TEXT/LIKE predicates | Moderate | ☐ Not started | Widen the ON/WHERE discriminator to text/`=`/LIKE filters. |
| WI-14 | UNION vs UNION ALL (dedup discrimination) | Moderate | ✅ Verified | Duplicate a witness row; output grows → UNION ALL, collapses → UNION. |
| WI-15 | Strict vs non-strict single-sided inequality (`<` vs `<=`) | Moderate | ☐ Not started | Strictness probe at the boundary value; add `lt`/`gt` op tokens. |
| WI-16 | ORDER BY on aggregate | Moderate | ☐ Not started | Revive dead `check_order_by_on_count`, generalize to SUM/AVG/MIN/MAX. |
| WI-17 | Type-aware / expandable numeric domain bounds | Moderate | ☐ Not started | Per-type bounds + outward-doubling pre-probe; disambiguate edge-vs-open. |
| WI-18 | OFFSET detection | Moderate | ☐ Not started | Needs a total order; insert ordered rows, see which window survives. **Blocked on S1.** |
| WI-19 | Non-equi / theta join (cross-table inequality ON) | Moderate→Hard | ☐ Not started | Classify cross-table AOA tuples as join edges; real win is outer theta. |
| WI-20 | Cross-join / cartesian validation + labeling | Moderate | ☐ Not started | Row-scaling probe confirms `~|a|×|b|`; a confidence flag, not new correctness. |

### Tier 3 — Hard (each its own project)

| ID | Title | Tier | Status | One-line summary |
|----|-------|------|--------|------------------|
| WI-21 | SELECT DISTINCT detection | Hard | ☐ Not started | Multi-row dedup probe; heavily confounded with GROUP BY / unique-key / LIMIT 1. **Blocked on S1.** |
| WI-22 | HAVING predicate | Hard | ☐ Not started | New post-aggregate stage; steer a group's aggregate to binary-search the cutoff. **Blocked on S1.** |
| WI-23 | Column alias (AS) verification | Hard | ☐ Not started | Subquery-wrap probe to confirm a referenceable output label. **Blocked on S1.** |
| WI-24 | Self-join degree k≥3 | Hard | ☐ Not started | Generalize the k=2 cap with a `k=round(log ratio/log m)` degree estimator. |
| WI-25 | Affine arithmetic predicate (`a.x + k <= b.y`) | Hard | ☐ Not started | Record the numeric gap as offset `k` on the AOA edge. |
| WI-26 | Algebraic inequalities among attributes (`a + b <= c`) | Hard | ☐ Not started | Collect affinely-independent boundary points; solve the hyperplane. |
| WI-27 | GROUP BY on (linear) expression | Hard | ☐ Not started | Collision probe: two rows differing on `c` collapse into one group. |
| WI-28 | Restricted single-level DNF (OR of conjunctive terms) | Hard | ☐ Not started | Mostly Infeasible — one witness row activates only one term. |
| WI-29 | Scalar function in SELECT (fixed catalog) | Hard | ☐ Not started | Hypothesis-test-by-mutation against a closed single-arg catalog. |
| WI-30 | String functions in predicates (UPPER/LOWER/TRIM/SUBSTRING) | Hard | ☐ Not started | Case-only / whitespace-only / window mutations localize the function. |
| WI-31 | Date functions EXTRACT/date_part | Hard | ☐ Not started | Periodic/banded acceptance region → calendar-alignment recognizer. |
| WI-32 | NULLS FIRST/LAST | Hard | ☐ Not started | Null-tolerant raw read of a single injected NULL sort row. **Blocked on S1.** |
| WI-33 | INTERSECT | Hard | ☐ Not started | Bespoke arm-discovery (nullify empties an intersection); confirm via EXCEPT ALL. |
| WI-34 | EXCEPT | Hard | ☐ Not started | Asymmetric nullify signal (additive A vs subtractive B arm). |
| WI-35 | Uncorrelated subquery-as-scalar-threshold (WHERE) | Hard | ☐ Not started | Mutate inner relation T; see if the recovered WHERE endpoint moves. |
| WI-36 | EXISTS / NOT EXISTS (uncorrelated) | Hard | ✅ Verified | Non-scaling probe distinguishes a gate from a cross join; NOT EXISTS recovered as `EXISTS(¬P)`. |
| WI-37 | Scalar subquery in SELECT | Hard | ☐ Not started | Constant projected column that tracks `max/sum` of a mutated T column. |
| WI-38 | CASE expression (2-branch, single-column) | Hard | ☐ Not started | Two distinct linear fits either side of a threshold signal a branch. |
| WI-39 | Composite-key / extra ON equality (outer joins) | Hard | ☐ Not started | Per-candidate NULL-mutation discriminator; ANDed equalities. |
| WI-40 | Outer/any join on a non-key column | Hard | ☐ Not started | Equi-join-stage gap: strengthen edge discovery on non-key pairs. |

### Headline stats

| Group | Total | Verified ✅ | % done | Items verified |
|-------|-------|------------|--------|----------------|
| Enablers | 2 | 1 | 50% | S2 (S1 outstanding) |
| Tier 1 — Easy (WI-01…05) | 5 | 4 | 80% | WI-01, WI-02, WI-03, WI-05 (WI-04 open) |
| Tier 2 — Moderate (WI-06…20) | 15 | 3 | 20% | WI-06, WI-11, WI-14 |
| Tier 3 — Hard (WI-21…40) | 20 | 1 | 5% | WI-36 |
| **Work-items overall (WI-01…40)** | **40** | **8** | **20%** | + S2 enabler + 1 Union FROM-clause bugfix |

---

## 3. Ground rules recap

The checklist's section 0, for a newcomer:

1. **Black-box discipline — no query parsing.** Never read `Qh`'s text. Recover everything by
   *mutating the DB, running `Qh`, and observing the result* — the Pop oracle (non-empty +
   null-free) plus result-shape / row-count / read-back values. If an approach needs the query
   string, it is wrong: redesign it as a mutation experiment or mark it Infeasible.
2. **Two layers = "Supported".** A `core/*` stage must **detect** the construct *and* QSG must
   **emit** it. Detected-but-not-emitted is still broken; never emit what you cannot observe.
3. **The output ceiling.** [`assembleQuery`](mysite/unmasque/src/util/QueryStringGenerator.py#L87-L95)
   emits six slots (`Select / From / Where / Group By / Order By / Limit`). New clauses need a new
   slot first → **S1**. The render helper [`append_clause`](mysite/unmasque/src/util/QueryStringGenerator.py#L14-L17)
   is no-op on an empty param, so unset slots render nothing.
4. **The minimization tension.** The View Minimizer collapses `D` to one witness row `D¹`.
   Multiplicity constructs (DISTINCT, UNION-dedup, cross-product scaling, HAVING, OFFSET, self-join
   degree, COUNT semantics) are invisible on one row → probe on a controlled multi-row instance via
   **S2**.
5. **The null-free tension.** Pop rejects any row with a null in a projected column
   ([nullfree_executable.py](mysite/unmasque/src/core/abstract/nullfree_executable.py)) → `IS NULL`,
   `COALESCE`, `NULLS FIRST/LAST` are hard-to-infeasible.
6. **Predicate-tuple contract.** Filter predicates are `(tab, attr, op, lb, ub)`; AOA tuples are
   `(node, op, node)` of length 3. Adding a new op/shape means auditing every consumer:
   [aoa.py](mysite/unmasque/src/core/aoa.py), [equi_join.py](mysite/unmasque/src/core/equi_join.py),
   and the QSG render path.
7. **Always add a feature flag** for new detection that costs probes or risks false positives:
   [config.ini](mysite/config.ini) `[feature]` → property in
   [configParser.py](mysite/unmasque/src/util/configParser.py) → read via
   `self.connectionHelper.config.<flag>`. Default new/risky stages OFF.
8. **Verification is part of "done".** Pick a `Qh` exercising the construct, run extraction, and
   confirm `Qh ≡ Q_E` by **bag-equality** over full `D` (both `EXCEPT ALL` directions empty) via
   [Comparator](mysite/unmasque/src/pipeline/abstract/Comparator.py). Add a `test/*Test.py` case.

**Status legend:** ☐ not started · ◐ theory locked · ▣ implemented (unverified) · ✅ verified.
*(No item is currently in the ◐ or ▣ states — every item is either ☐ or ✅.)*

> **Note on rule 8's anchor.** The checklist (section 0, rule 6 at [checklist.md](checklist.md))
> cites `mysite/unmasque/src/core/abstract/Comparator.py` and [CLAUDE.md](CLAUDE.md) repeats it —
> **that path does not exist.** The only `Comparator.py` is
> [pipeline/abstract/Comparator.py](mysite/unmasque/src/pipeline/abstract/Comparator.py). The WI-03
> entry itself uses the correct path.

---

## 4. Shipped work (verified)

Each subsection: **Before** · **After** · **Approach** · **Depends on** · **Risk** ·
**Verification** · **Where it lives** · **Discrepancies found**. Every item below was opened in
source and confirmed.

> **Cross-cutting verification caveat.** The audit could run the unit suites only inside the
> project virtualenv (`/home/ryuk/Xpose_new/.venv`); under bare system `python3` they fail at import
> because `pandas` is missing in the `ExecutableFactory` chain — an **environment gap, not a code
> defect**. Where a suite was actually executed it is noted ("ran OK"); the rest were confirmed by
> reading the real-method test bodies. This is called out honestly rather than asserted as green.

---

### S2 — Controlled multi-row / duplicate-row probe primitive  ✅ Verified (2026-06-02)

- **Before:** the duplicate-by-ctid (`INSERT … SELECT … RETURNING ctid`), delete-by-ctid, and
  header-stripped row-count logic were copy-pasted inside the two stages that first needed them
  ([cardinality_probe.py](mysite/unmasque/src/core/cardinality_probe.py),
  [multiplicity_probe.py](mysite/unmasque/src/core/multiplicity_probe.py)). Any new multiplicity
  detector had to re-implement the same SQL and the same None/header handling.
- **After:** one shared [`RowProbe`](mysite/unmasque/src/core/row_probe.py) class:
  [`duplicate_rows(fqn, ctids=None)`](mysite/unmasque/src/core/row_probe.py#L85) (all rows or
  targeted by ctid, `INSERT INTO {fqn} SELECT * FROM {fqn}{where} RETURNING ctid::text`),
  [`delete_rows(fqn, ctids)`](mysite/unmasque/src/core/row_probe.py#L112),
  [`count_rows(query)`](mysite/unmasque/src/core/row_probe.py#L55) (header-stripped data-row count;
  `-1` on exception, `0` on None), and
  [`list_ctids(fqn)`](mysite/unmasque/src/core/row_probe.py#L123).
- **Approach (black-box):** mutate `D¹` by INSERTing known duplicate tuples, run `Qh`, read the
  **row-count change**, then DELETE the duplicates by their RETURNING-captured ctids so `D` (and its
  ctid set) is byte-for-byte restored. **No new oracle:** duplicate/delete go through
  `connectionHelper`; count reuses `app`; because `app` shares the stage's connection/transaction,
  an uncommitted INSERT is visible to the next `app.doJob` and the DELETE reverts it.
- **Depends on:** existing `connectionHelper.queries.select_ctid_star_from`
  ([abstract_queries.py:139](mysite/unmasque/src/util/abstract_queries.py#L139)) and the `app`
  executable. S2 is itself the foundational enabler for WI-05/06/14/36.
- **Risk:** low — thin factoring of pre-existing SQL, no flag, no new oracle. Residual: `count_rows`
  could misclassify an all-string data row as a header; `delete_rows` only logs per-ctid failures,
  so a failed revert would silently leave `D` mutated (the integration test pins the restore
  invariant).
- **Verification:** [RowProbeTest.py](mysite/unmasque/test/RowProbeTest.py) — **9 unit cases + 1
  live-DB integration case** (`Ran 9 tests … OK` for the unit class). The integration case proves
  the acceptance criterion: duplicate 1 ctid → count `+1` → delete → the **exact ctid set is
  restored** (`before == after`).
- **Where it lives:** [row_probe.py](mysite/unmasque/src/core/row_probe.py) (class @
  [:36](mysite/unmasque/src/core/row_probe.py#L36)); owners delegate —
  [cardinality_probe.py `_insert_duplicate`@196 / `_delete_rows_at_ctids`@200](mysite/unmasque/src/core/cardinality_probe.py#L196),
  [multiplicity_probe.py `_count_rows`@36](mysite/unmasque/src/core/multiplicity_probe.py#L36).
  [`cardinality_probe._count_qh`@186](mysite/unmasque/src/core/cardinality_probe.py#L186) is
  deliberately left with its own variant (preserves verified self-join detection). **No feature
  flag** (always-available helper).

> **Discrepancies found.**
> 1. **Test count off by one.** The checklist says *"10 unit cases + 1 live-DB integration case"*
>    (implying 11 methods). The file actually has **9 unit + 1 integration = 10** `def test_`
>    methods total.
> 2. **Consumers undercounted.** The S2 entry frames `RowProbe` as a factoring-out for exactly two
>    owners. In the current tree it is *also* consumed by
>    [set_op_probe.py](mysite/unmasque/src/core/set_op_probe.py),
>    [groupby_clause.py](mysite/unmasque/src/core/groupby_clause.py),
>    [exists_gate_probe.py](mysite/unmasque/src/core/exists_gate_probe.py), and
>    [aggregation.py](mysite/unmasque/src/core/aggregation.py). Broader adoption — not a defect.

---

### WI-01 — Fix `COUNT(col)` rendering bug  ✅ Verified — *latent in the default config* (2026-06-01)

- **Before:** the QSG select-clause count branch short-circuited on the **substring** test
  `COUNT in label`. Since `COUNT_STAR` is the literal `'Count(*)'`, that test also matched
  `COUNT(*)`, and for a column-COUNT (label `'Count'`) it emitted the bare, invalid token
  `Count as order_count`, **dropping the counted column**.
- **After:** an exact-equality `if/elif/else` —
  [`label == COUNT_STAR`](mysite/unmasque/src/util/QueryStringGenerator.py#L677) emits `Count(*)`
  verbatim; the generic else wraps the column as `label(col)` with
  [`col = elt if elt else aggregate_tuple[0]`](mysite/unmasque/src/util/QueryStringGenerator.py#L701),
  so a column-COUNT renders `Count(o_orderkey)` and `COUNT(*)` is preserved.
- **Approach (black-box):** WI-01 itself adds **no probe** — it is a pure downstream *string-render*
  fix that turns an already-discovered count label into correct SQL. The fix is sound because it
  replaces substring containment with exact-string equality against the `COUNT_STAR` sentinel.
- **Depends on:** **WI-06**. The bare-`'Count'` label is only ever produced by WI-06's flag-gated
  `_refine_counts`; the synthetic [CountRenderTest.py](mysite/unmasque/test/CountRenderTest.py)
  feeds it directly.
- **Risk:** low — a localized equality test plus one import. The real risk is the **latency** (see
  below): in the default config the new column-COUNT branch is unreachable.
- **Verification:** [CountRenderTest.py](mysite/unmasque/test/CountRenderTest.py) — **4 cases**
  exercising the real name-mangled `__generate_select_clause`; asserts the exact mixed-select string
  `o_orderpriority, Count(o_orderkey) as order_count, Count(*) as total, Sum(o_totalprice) as rev`.
- **Where it lives:** import at [QSG:10](mysite/unmasque/src/util/QueryStringGenerator.py#L10); gate
  and branches in `__generate_select_clause` at
  [QSG:677-702](mysite/unmasque/src/util/QueryStringGenerator.py#L677-L702);
  [constants.py:29-30](mysite/unmasque/src/util/constants.py#L29-L30). **Effective flag:**
  `count_distinct` ([configParser.py:44](mysite/unmasque/src/util/configParser.py#L44),
  default `no`) — the QSG fix is unconditional, but a `(col,'Count')` label only reaches it when
  that flag is on.

> **Discrepancies found.** All are **stale line anchors** (behavior is correct):
> - QSG gate cited *"~611"* → actually [QSG:677](mysite/unmasque/src/util/QueryStringGenerator.py#L677)
>   (gate) + 688-702 (column-wrap else); off by ~66 lines.
> - `aggregation` store of `(attrib,'Count')` cited *":318-319"* → actually
>   [aggregation.py:340](mysite/unmasque/src/core/aggregation.py#L340); lines 318-319 are an
>   unrelated `if after is None: continue` guard.
> - `('',COUNT_STAR)` store cited *":243"* → actually
>   [aggregation.py:268](mysite/unmasque/src/core/aggregation.py#L268); line 243 is unrelated
>   aggregate-value arithmetic.
> - Empty-slot override cited *"241-243"* → actually
>   [aggregation.py:266-268](mysite/unmasque/src/core/aggregation.py#L266-L268).
> - **The "latent" finding is sound and confirmed:** with `count_distinct = no` (default),
>   `_refine_counts` ([aggregation.py:270-271](mysite/unmasque/src/core/aggregation.py#L270-L271))
>   never runs, so every empty-projected count is forced to `COUNT_STAR` and the new branch is dead
>   in the default pipeline. WI-01 is a correct, safe *prerequisite render fix* whose payoff
>   materializes only with WI-06 enabled.

---

### WI-02 — Lift LIMIT=1000 cap via exponential + binary search  ✅ Verified (2026-06-01)

- **Before:** [`doLimitExtractJob`](mysite/unmasque/src/core/limit.py#L29) inserted `no_rows`
  (default 1000) matching rows in one linear pass and gave up past the cap (`limit = None`). Any
  LIMIT ≥ 1000 was unrecoverable, and it always paid the full budget regardless of the actual LIMIT.
- **After:** two helpers —
  [`__probe_limit_card`@51-81](mysite/unmasque/src/core/limit.py#L51) (one black-box probe:
  `do_init()` reset + insert exactly `m` matching rows + `app.doJob` + the unchanged
  `len(result) − rmin_card + 2` normalization) and
  [`__search_limit`@83-155](mysite/unmasque/src/core/limit.py#L83) (exponential doubling until the
  cardinality plateaus, then binary-search the bracket; takes the probe as a callable, so it is
  unit-testable without a DB). Reported limit = [`plateau_card − 1`](mysite/unmasque/src/core/limit.py#L136).
- **Approach (black-box):** the pre-LIMIT result grows linearly with inserted rows; a LIMIT `L`
  clips it, so the **row-count plateau encodes `L`**. The `bounded` split
  ([limit.py:43](mysite/unmasque/src/core/limit.py#L43)) gives the no-group case budget `2·no_rows`
  (so the boundary `L = no_rows` is confirmable) and the group-bounded case budget `no_rows`. Pure
  row-count signal, no query-text parsing.
- **Depends on:** existing per-stage insert machinery (reused unchanged) and
  [`limit_limit`@configParser.py:25](mysite/unmasque/src/util/configParser.py#L25) (default 1000).
- **Risk:** low-to-moderate, handled conservatively: LIMIT 1/2 fold into the `D¹` baseline (returns
  None via the `≥3` floor); a true LIMIT beyond `2·no_rows` is reported None, never a wrong value.
- **Verification:** [LimitSearchTest.py](mysite/unmasque/test/LimitSearchTest.py) — **9 cases** on
  the real `__search_limit`, including **L=5000 recovered in <40 probes** (audit ran the search body
  standalone: 27 probes), no-limit→None, group-bounded edge, tiny-L floor, and a logarithmic
  probe-count sweep.
- **Where it lives:** [limit.py:29-155](mysite/unmasque/src/core/limit.py#L29-L155). **No feature
  flag** (runs unconditionally; bounded only by `limit_limit`). Emission **unchanged**.

> **Discrepancies found.**
> - *"Emission untouched (QSG:255)"* — the substantive claim is **true** (`git show bc5cd3d` shows
>   zero QSG limit-path changes), but the line is wrong: limit render is
>   [QSG:94](mysite/unmasque/src/util/QueryStringGenerator.py#L94) and the setter is QSG:272-274;
>   line 255 is the `algebraic_predicates` getter.
> - `config.ini` has **no** `[options] limit` entry, so the 1000 default comes solely from
>   [configParser.py:25](mysite/unmasque/src/util/configParser.py#L25) at runtime. (Checklist
>   phrasing is right; just noting the `.ini` does not override it.)

---

### WI-03 — Robust outer-join candidate equivalence (Comparator EXCEPT ALL)  ✅ Verified (2026-06-02)

- **Before:** [`__are_the_results_same`](mysite/unmasque/src/core/outer_join.py#L253) did an
  **ordered, positional** row-by-row equality after a length check. SQL results are bags, so row
  reordering (ORDER BY ties) or duplicate rows could make two equivalent candidates compare unequal
  — or two different candidates compare equal under coincidental alignment. It also had no early
  exit and never failed closed on a diff error.
- **After:** the method now computes `fwd = |Qh EXCEPT ALL Q_E|` and `rev = |Q_E EXCEPT ALL Qh|` via
  the new helper [`__bag_diff_count`@279](mysite/unmasque/src/core/outer_join.py#L279) and returns
  `fwd == 0 and rev == 0` (Comparator `is_match` bag semantics, order/duplicate insensitive).
- **Approach (black-box):** the diff is run **in-stage** via
  `app.doJob("select count(*) from ((<left>) except all (<right>)) as T;")`
  ([outer_join.py:289-291](mysite/unmasque/src/core/outer_join.py#L289)) so it observes the
  join-break / NULL-injection mutation the caller just applied to `D¹`.
  [`Comparator.match`](mysite/unmasque/src/pipeline/abstract/Comparator.py#L97) is unusable here
  because it first restores tables to `user_schema`, erasing the mutation. **Fails closed** (rejects)
  on any diff error — false-positive is the dangerous direction
  ([outer_join.py:273-276](mysite/unmasque/src/core/outer_join.py#L273)) — and **short-circuits**
  `if not same: return False` ([outer_join.py:268-269](mysite/unmasque/src/core/outer_join.py#L268))
  to save DB round-trips.
- **Depends on:** nothing upstream. WI-03 is the trustworthy equivalence check that **WI-11**
  requires.
- **Risk:** low for the equivalence logic itself (strictly more correct, fail-closed, well-tested).
- **Verification:** [OuterJoinResultSameTest.py](mysite/unmasque/test/OuterJoinResultSameTest.py) —
  **17 cases** on the real methods: reorder/duplicate/NULL-extended → same; differing
  multiplicity/disjoint/subset → not-same; short-circuit makes 0 DB calls; fail-closed on diff
  error; `__bag_diff_count` parse/strip/None paths.
- **Where it lives:** [outer_join.py:253-301](mysite/unmasque/src/core/outer_join.py#L253). **Flag:**
  the existing `outer_join` (no new flag).

> **Discrepancies found.** All stale anchors (behavior correct):
> - Comparator `is_match` cited at *pipeline/abstract/Comparator.py#L63* → L63 is
>   `run_diff_query_match_and_dropViews`; the actual
>   [`is_match`@71](mysite/unmasque/src/pipeline/abstract/Comparator.py#L71) /
>   [`run_diff_queries`@77](mysite/unmasque/src/pipeline/abstract/Comparator.py#L77).
> - The gap-witness sibling pattern is cited at *gap_witness.py:228* — that's the
>   [`_diff_nonempty`@228](mysite/unmasque/src/core/gap_witness.py#L228) `def` line; the
>   `count(*) … EXCEPT ALL` SQL is at gap_witness.py:245-247. (Minor implementation difference:
>   the sibling uses `execute_sql_fetchone_0`, WI-03 uses `app.doJob` — intentional, to observe the
>   in-stage mutation.)
> - **Checklist-wide path bug:** [checklist.md](checklist.md) section 0 cites
>   `src/core/abstract/Comparator.py`, which does not exist (only `pipeline/abstract/`). The WI-03
>   body itself uses the correct path.

---

### WI-05 — `const-1` vs `COUNT()==1` disambiguation  ✅ Verified (2026-06-02)

- **Before:** the const-1-vs-COUNT split was a *value* heuristic
  ([groupby_clause.py:78-84](mysite/unmasque/src/core/groupby_clause.py#L78-L84)) — it marked a
  column the literal `1` whenever every probed group read `'1'`. Unsound in principle: a genuine
  `COUNT(*)`/`COUNT(col)` that reads 1 in every probed group is indistinguishable from a literal 1
  by value alone. (Sound *in practice today* only because the grouping-column synthesis delta
  `[0,1,1]` at [groupby_clause.py:43](mysite/unmasque/src/core/groupby_clause.py#L43) incidentally
  materializes a ≥2-row group for a real COUNT.)
- **After:** [`_confirm_const1_columns`@121-177](mysite/unmasque/src/core/groupby_clause.py#L121)
  (+ static [`_max_int_in_col`@179-194](mysite/unmasque/src/core/groupby_clause.py#L179)) confirms
  each candidate directly. Value grew → genuine COUNT, left empty-projected; value unchanged → real
  literal 1, set to `CONST_1_VALUE`.
- **Approach (black-box):** on the single-group witness `D¹`, `do_init()`, read the column's value,
  **duplicate one contributing witness row of `core_relations[0]` by ctid** via `RowProbe` (S2),
  re-run `Qh`, revert. A COUNT tracks row multiplicity and rises `1→2`; the literal 1 is invariant.
  Probing on `D¹` (exactly one group) keeps the signal undiluted; degrades to the old verdict on any
  unreadable reading, so a true literal 1 is never mis-flagged.
- **Depends on:** **S2** ([RowProbe wired @groupby_clause.py:30](mysite/unmasque/src/core/groupby_clause.py#L30)).
- **Risk:** low — no false positive on a real constant (its value can't grow).
- **Verification:** [GroupByConst1Test.py](mysite/unmasque/test/GroupByConst1Test.py) — **11 cases**
  (`Ran 11 tests … OK` in the project venv) modelling the exact case the old heuristic gets wrong
  (a COUNT reading 1 on the witness).
- **Where it lives:** [groupby_clause.py:115-194](mysite/unmasque/src/core/groupby_clause.py#L115).
  **No QSG change, no feature flag.**

> **Discrepancies found.** Stale anchors only:
> - Empty-projection → `COUNT(*)` cited at *aggregation.py:241-243* → actually
>   [aggregation.py:267-268](mysite/unmasque/src/core/aggregation.py#L267) +
>   [QSG:677-682](mysite/unmasque/src/util/QueryStringGenerator.py#L677); lines 241-243 are
>   unrelated AVG/equation debug code.
> - Old heuristic cited *groupby_clause.py:74-80* → actually
>   [78-84](mysite/unmasque/src/core/groupby_clause.py#L78) (the same stale anchor is repeated in
>   [checklist.md](checklist.md)).
> - Synthesis delta `[0,1,1]` cited *:39* → actually
>   [:43](mysite/unmasque/src/core/groupby_clause.py#L43); the stale `:39` anchor is **also hard-coded
>   into the source comment** at groupby_clause.py:108.

---

### WI-06 — `COUNT(DISTINCT col)` (+ companion `COUNT(col)` vs `COUNT(*)`)  ✅ Verified (2026-06-02)

- **Before:** aggregation blanket-labeled **every** count `('', COUNT_STAR)` (a count has no
  value-dependency for projection to discover), so `count(distinct col)`, `count(col)`, and
  `count(*)` were all emitted as `Count(*)`.
- **After:** a flag-gated [`_refine_counts`@276](mysite/unmasque/src/core/aggregation.py#L276) runs
  after the blanket label and re-classifies each `COUNT_STAR` slot, storing
  `(col, COUNT_DISTINCT)` or `(col, COUNT)` (agg-tuple shape unchanged — still `(attrib, op)`). QSG
  renders `Count(distinct col)` via a
  [dedicated branch @682-687](mysite/unmasque/src/util/QueryStringGenerator.py#L682) and the
  column-COUNT [fallback @701-702](mysite/unmasque/src/util/QueryStringGenerator.py#L701).
- **Approach (black-box, three probes on `D¹`):** (1) **distinctness** — exact-duplicate one
  surviving witness row (S2): a non-distinct count rises `1→2`, `COUNT(DISTINCT)` stays 1; (2)
  **distinct-column id** — insert a witness-copy whose only change is a *fresh distinct* value in a
  candidate column; only the truly distinct-counted column lifts the count; (3) **non-distinct
  companion** — null-inject a candidate column (`COUNT(*)` counts it, `COUNT(col)` skips the NULL)
  with a survival guard to rule out the dropped-row false positive. Candidates **exclude GROUP BY
  keys and equi-join keys** ([aggregation.py:346-363](mysite/unmasque/src/core/aggregation.py#L346)).
- **Depends on:** **WI-01** (column-COUNT render) and **S2** (multi-row witness).
- **Risk:** O(candidates) probes per count column (bounded, OFF by default); a count over a
  non-driving relation could be misattributed → conservative leave-`COUNT(*)` fallback bounds the
  blast radius.
- **Verification:** [CountDistinctAggTest.py](mysite/unmasque/test/CountDistinctAggTest.py) —
  **17 cases** on the real methods with a synthetic count oracle (including a decoy-neighbour column
  and the survival-guard false-positive block).
- **Where it lives:** detection in [aggregation.py](mysite/unmasque/src/core/aggregation.py)
  (`_refine_counts` + 5 helpers + module-level `_max_int_in_result_col`); sentinel
  [constants.py:37](mysite/unmasque/src/util/constants.py#L37); render
  [QSG:142/682-687/701-702](mysite/unmasque/src/util/QueryStringGenerator.py#L142). **Flag:**
  `count_distinct` ([config.ini:23](mysite/config.ini#L23), default **no**).

> **Discrepancies found.** Stale anchors only:
> - `_refine_counts` "after the line-243 blanket label" → blanket is at
>   [aggregation.py:266-268](mysite/unmasque/src/core/aggregation.py#L266), flag-gated call at
>   [270-271](mysite/unmasque/src/core/aggregation.py#L270); line 243 is unrelated SUM/AVG arithmetic.
> - Render range cited *QSG:608-640* → actually
>   [QSG:682-687 + 701-702](mysite/unmasque/src/util/QueryStringGenerator.py#L682) (off ~70-90 lines).
> - Minor phrasing: the order-by substring tests
>   ([orderby_clause.py:117,127](mysite/unmasque/src/core/orderby_clause.py#L117)) read
>   `elt.aggregation`, not a variable literally named `op`. `check_order_by_on_count`
>   ([orderby_clause.py:91](mysite/unmasque/src/core/orderby_clause.py#L91)) is confirmed dead.

---

### WI-11 — Wire outer joins ON by default (route to JOIN…ON renderer)  ✅ Verified — *default-on* (2026-06-02)

- **Before:** detection (nullability probe → `importance_dict` l/h markers) and the JOIN…ON renderer
  both existed, but only inside `OuterJoinPipeLine`, gated behind the off-by-default `outer_join`
  flag; the default factory chose `ExtractionPipeLine` → comma-FROM (= INNER) for every outer join.
  Routing was a fragile string scan of the rendered SQL (`q_candidate.count('OUTER')`).
- **After:** marker-based
  [`_seq_routes_to_join_on`@358](mysite/unmasque/src/core/outer_join.py#L358) — route to JOIN…ON iff
  some edge marker `≠ ('l','l')`; all-`('l','l')` keeps comma-FROM.
  [`__formulateQueries`@319](mysite/unmasque/src/core/outer_join.py#L319) `continue`s on the inner
  case (comma-FROM baseline stands) and on the outer case calls `clear_from_where_ops()` **before**
  `generate_from_on_clause` — the **exactly-one-emitter guard, by construction**. Default-on:
  [`outer_join = yes`@config.ini:21](mysite/config.ini#L21) +
  [`self.detect_oj = True`@configParser.py:42](mysite/unmasque/src/util/configParser.py#L42).
- **Approach (black-box):** WI-11 is the **emission/routing half** — turn the `(l,h)` nullability
  markers (themselves discovered by a join-break + Pop probe) into the actual FROM shape, off the old
  output-text scan. Candidates are only *proposed* here; the downstream WI-03 `EXCEPT ALL` bag oracle
  finalizes which outer variant is sound and fails closed.
- **Depends on:** **WI-03** (trustworthy equivalence check).
- **Risk:** runs the nullability probe on every multi-table query; a dangling row masked by
  aggregation/LIMIT → false-inner (the *safe* direction). **⚠ Shipped-default footgun:** with both
  `outer_join=yes` and `gap_aware=yes` (both live defaults — see
  [config.ini](mysite/config.ini#L16-L24)), the gap-witness Filter stage cartesian-joins all FROM
  tables on a numeric filter over a multi-table outer join
  ([`_build_qe`@gap_witness.py:203](mysite/unmasque/src/core/gap_witness.py#L203)) and **hangs**.
- **Verification:** [OuterJoinRouteTest.py](mysite/unmasque/test/OuterJoinRouteTest.py) — **12 cases**
  on the real routing methods, including the exactly-one-emitter assertion on a real QSG (JOIN…ON
  *replaces*, not appends, the comma list).
- **Where it lives:** routing/guard in
  [outer_join.py](mysite/unmasque/src/core/outer_join.py) (`_seq_routes_to_join_on@358`,
  `__formulateQueries@319`, `__determine_join_edge_type@436`); renderer
  [`generate_from_on_clause`@QSG:913](mysite/unmasque/src/util/QueryStringGenerator.py#L913) +
  [`join_map`@QSG:139](mysite/unmasque/src/util/QueryStringGenerator.py#L139). **Flag:** `outer_join`
  (now default **yes**).

> **Discrepancies found.** The two **principal** anchors (`_seq_routes_to_join_on@358`,
> `__formulateQueries@319`) are **exact**. Three are stale:
> - `generate_from_on_clause` cited *QSG:847* → actually
>   [QSG:913](mysite/unmasque/src/util/QueryStringGenerator.py#L913).
> - `join_map` cited *QSG:131* → actually
>   [QSG:139](mysite/unmasque/src/util/QueryStringGenerator.py#L139).
> - `__determine_join_edge_type` cited *outer_join.py:402* → actually
>   [:436](mysite/unmasque/src/core/outer_join.py#L436) (the `('l','l')` default-on-missing-edge fix,
>   which replaced a latent `UnboundLocalError`, is at line 443).
> - **Config confirmed live:** `outer_join = yes` and `detect_oj = True` are the current values
>   (not toggled back) — the default-on claim holds.

---

### WI-14 — UNION vs UNION ALL (dedup discrimination)  ✅ Verified (2026-06-02)

*(Bundled with the orthogonal **Union FROM-clause junk-relation bugfix**, also verified.)*

- **Before:** [`UnionPipeLine.__post_process`](mysite/unmasque/src/pipeline/UnionPipeLine.py#L144)
  hard-coded the `"\n UNION ALL "` join token (verified against parent commit `46a4428`); no
  bag-vs-set probe — a true `UNION` was always mis-emitted as `UNION ALL`.
- **After:** [`SetOpProbe`](mysite/unmasque/src/core/set_op_probe.py) (a `GenerationPipeLineBase`
  subclass for the real `do_init()` D¹-reset + singleton `Qh`). While a branch is isolated (others
  nullified), it counts `Qh` rows (`c0`), duplicates one contributing witness row via `RowProbe`
  (S2), recounts (`c1`), reverts: **`c1>c0` → UNION ALL** (bag growth is impossible under set
  semantics — unconditional proof), **`c1==c0` → UNION**, else undecided
  ([set_op_probe.py:62-108](mysite/unmasque/src/core/set_op_probe.py#L62)).
  [`_resolve_set_op`@128-142](mysite/unmasque/src/pipeline/UnionPipeLine.py#L128) defaults to
  `UNION ALL` and emits `UNION` only on positively-observed dedup.
- **Approach (black-box):** the probe runs **`Qh` itself** (not our extracted branch), so it sees
  the real operator through the spurious per-branch `GROUP BY` that the group-by stage infers for a
  `UNION`. The gate [`__branch_is_probeable`@114-126](mysite/unmasque/src/pipeline/UnionPipeLine.py#L114)
  skips only on a **genuine aggregate** (which absorbs the duplicate regardless of operator), not on
  a bare GROUP BY.
- **Depends on:** `union=yes` (surfaces as `config.detect_union`), **S2**.
- **Risk:** OFF by default. Blind spot: a genuine per-branch `SELECT DISTINCT` under `UNION ALL`
  reads as `UNION` (WI-21 territory). Mixed operators are unrepresentable → safe `UNION ALL` default.
- **Verification:** [SetOpDedupTest.py](mysite/unmasque/test/SetOpDedupTest.py) — **23 cases**;
  [UnionFromClauseTest.py](mysite/unmasque/test/UnionFromClauseTest.py) — **10 cases** (the latter
  proven *non-vacuous*: stubbing the fix reproduces the `{'O','K',' '}` junk).
- **Where it lives:** [set_op_probe.py](mysite/unmasque/src/core/set_op_probe.py) +
  [UnionPipeLine.py:39-156](mysite/unmasque/src/pipeline/UnionPipeLine.py#L39); the bugfix at
  [`_as_relation_list`@union_from_clause.py:61-75](mysite/unmasque/src/core/union_from_clause.py#L61).
  **Flag:** `union` (default **no**).

> **The Union FROM-clause bugfix** (confirmed): for a bare single-table single-column UNION branch
> the arms share no common relation, so `FromClause.doJob` raises `ERROR_006`,
> [`ExtractorBase.doJob`@36-39](mysite/unmasque/src/core/abstract/ExtractorBase.py#L36) swallows it
> and returns the status string `"OK "`, and
> [`algorithm1.algo`@26-27](mysite/unmasque/src/core/algorithm1.py#L26) char-iterated it into junk
> relations `{'O','K',' '}`, aborting extraction.
> [`_as_relation_list`](mysite/unmasque/src/core/union_from_clause.py#L61) normalizes any non-`list`
> result to `[]`. This **unblocks** the non-latent WI-14 overlap demo (fully-overlapping regionkeys:
> bare `UNION`=5 rows vs the pre-WI-14 `DISTINCT(nation) UNION ALL DISTINCT(region)`=10).

> **Discrepancies found.** Anchors are essentially **exact** here (this is the most accurately-cited
> shipped item). Minor notes:
> - The `_as_relation_list` `@staticmethod` decorator is on line 61, `def` on line 62 (checklist
>   cites :62 — accurate).
> - Terminology: the config key `union` surfaces internally as `config.detect_union`
>   (`DETECT_UNION = "union"`); not a defect, just a name mismatch worth knowing.

---

### WI-36 — EXISTS / NOT EXISTS (uncorrelated)  ✅ Verified (2026-06-03)

- **Before:** an uncorrelated EXISTS gate relation `T` (e.g. `region` in
  `SELECT n_name FROM nation WHERE EXISTS (SELECT 1 FROM region WHERE r_regionkey > 2)`) was swept
  into `core_relations` (emptying `T` empties `Qh` → kept) and comma-joined into FROM as a **wrong
  cross join** `Select n_name From nation, region`. A genuine cross join and an EXISTS gate are
  indistinguishable to the void/error core classifier — both empty `Qh` when `T` is emptied.
- **After:** [`ExtractionPipeLine._reclassify_exists_gates`@284](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L284)
  runs right after the Limit stage. A core relation `T` is an uncorrelated EXISTS gate iff **all
  four** hold: (1) **load-bearing** (core membership); (2) **non-projecting** (`T` in no
  [`Projection.dependencies`](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L347) entry);
  (3) **non-joining** (`T` in no
  [aoa equi/AOA/theta edge](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L371)); (4)
  **NON-SCALING** — the decisive discriminator.
- **Approach (black-box):** condition (4) is the
  [`ExistsGateProbe`](mysite/unmasque/src/core/exists_gate_probe.py#L66) — duplicate one contributing
  `T` row on `D¹` via `RowProbe` (S2) and recount `Qh`: **`|Qh|` unchanged ⇒ gate; grows ⇒ cross/
  inner join** (kept in FROM). This mirrors WI-14's `SetOpProbe`, inverted (there scaling
  distinguishes `UNION ALL` vs dedup; here scaling distinguishes cross-join vs gate).
  **Fail-closed** throughout: any inconclusive/None/exception keeps `T` in core (status quo). On a
  gate verdict `T` is pulled from `core_relations`/`instances`/`alias_to_table` and rendered as a
  `<kind> (SELECT 1 FROM T [WHERE …])` WHERE conjunct
  ([QSG:460-469](mysite/unmasque/src/util/QueryStringGenerator.py#L460)), with `T`'s own filter
  predicates moved inside the subquery and skipped in the outer WHERE
  ([QSG:618-622](mysite/unmasque/src/util/QueryStringGenerator.py#L618)).
- **Depends on:** **S2**; modelled on WI-14's `SetOpProbe`.
- **Risk / limits:** **correlated EXISTS** is Infeasible (on `D¹` the correlation column has a fixed
  witness value); **NOT EXISTS polarity is not recovered** (see below); a global aggregate collapses
  the `(4)` row-count signal. OFF by default → zero blast radius on the default config.
- **Verification:** [ExistsGateTest.py](mysite/unmasque/test/ExistsGateTest.py) — **26 cases**
  (`Ran 26 tests … OK` in the venv) on the real probe, the real reclassification helpers (incl. a
  real-`PGAOcontext` regression guard), and the real QSG render.
- **Where it lives:** [exists_gate_probe.py](mysite/unmasque/src/core/exists_gate_probe.py) +
  [ExtractionPipeLine.py:208-406](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L208) + QSG
  emission ([`exists_gates`@QSG:54](mysite/unmasque/src/util/QueryStringGenerator.py#L54)). **Flag:**
  `exists` ([config.ini:24](mysite/config.ini#L24), default **no**).

> **Discrepancies found.** The WI-36 entry cites **only file paths + method names (no line
> numbers)**, so there are **no stale line anchors** — and every cited method exists at the claimed
> file. Notes:
> - **Anchor correction the checklist *itself* makes:** the original plan's anchor
>   (`__nullify_relations`) and its "NOT EXISTS = inverse" framing were both wrong; the shipped entry
>   documents the correction (the default `SQL_ERR_FWD` app-type means a NOT EXISTS gate is **kept
>   core**, the filter recovers the **complement**, and WI-36 emits a positive `EXISTS(¬P)` that is
>   **bag-equivalent on the extraction `D`** but does **not recover polarity**). A polarity probe is
>   specified as future work.
> - `NOT_EXISTS_GATE` ([constants.py:83](mysite/unmasque/src/util/constants.py#L83)) is **defined but
>   never produced by the pipeline** — the impl hard-codes `{'kind': 'EXISTS'}`
>   ([ExtractionPipeLine.py:334](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L334)). Only the
>   QSG renderer would honor a `'NOT EXISTS'` kind, and only a unit test supplies it. Consistent with
>   the polarity-not-recovered finding, but a reader skimming the EMISSION claim should not expect
>   `NOT EXISTS` to ever be emitted by the pipeline.
> - Minor framing: "T pulled out of core_relations / instances / alias_to_table" is **split across
>   two methods** (`__reclassify_exists_gates_impl` removes from core_relations;
>   [`_strip_gate_relations`@397](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L397) handles
>   instances + alias_map) — functionally correct, just distributed.
> - The mid-dev bugfix is real: `_gate_projected_tables` originally read the **write-only**
>   `PGAOcontext.aggregate` getter (raises `NotImplementedError`); it now reads
>   [`aggregated_attributes`@ExtractionPipeLine.py:365](mysite/unmasque/src/pipeline/ExtractionPipeLine.py#L365),
>   with a real-`PGAOcontext` regression test.

---

## 5. Planned / not-started work

All items below are **☐ not started** (none are at ◐ or ▣). Framed as plans: **Now** (current gap),
**Do** (proposed mutation experiment + emission), **Depends on**, **Risk**, **Verify by**. Where the
checklist's detail is thin, it is said so rather than invented.

### Tier 1 — Easy

#### WI-04 — Within-attribute equality OR → IN, cheap default-on path
- **Now:** `A=x OR A=y` works only when `or=yes` and re-runs the *whole* mutation pipeline per
  disjunct ([DisjunctionPipeLine.py:191](mysite/unmasque/src/pipeline/fragments/DisjunctionPipeLine.py#L191)),
  consolidated to IN at [genPipeline_context `__adjust_for_in_predicates`:302](mysite/unmasque/src/core/dataclass/genPipeline_context.py#L302).
- **Do:** after the first satisfying value via
  [`checkAttribValueEffect`](mysite/unmasque/src/core/filter.py#L154), keep binary-searching the
  *residual domain* (masking found values) for more disjoint singletons → accumulate as IN members.
  Keep the rerun loop as fallback for mixed equality+range disjuncts. No QSG change.
- **Depends on:** none (reuses the existing IN render).
- **Risk:** no natural termination signal for "# of disjuncts" under Pop — must bound iterations or
  sample distinct values from `user_schema` (can miss values absent from base data).
- **Verify by:** `l_shipmode='AIR' OR l_shipmode='RAIL'`.

### Tier 2 — Moderate

#### WI-07 — Boolean datatype in WHERE + graceful skip on unknown types
- **Now:** [`get_datatype`](mysite/unmasque/src/core/abstract/un2_where_clause.py#L185) recognizes
  only int/date/text/numeric and **raises `UnmasqueError`** otherwise — one exotic column aborts the
  whole run.
- **Do:** add a `bool` branch (two-point True/False probe via `checkAttribValueEffect`; emit
  `(tab,attr,'=',v,v)`), and replace the hard raise with **log-and-skip** so extraction degrades
  gracefully.
- **Risk:** boolean is 3-state with NULL; a true-or-null column can't be cleanly separated under
  null-free Pop.
- **Verify by:** `WHERE is_active = true`.

#### WI-08 — Timestamp / time datatype support
- **Now:** `date` (whole-day) is first-class; `timestamp`/`time`/`timestamptz` fall through to the
  `UnmasqueError` raise ([un2_where_clause.py:193-194](mysite/unmasque/src/core/abstract/un2_where_clause.py#L193)).
- **Do:** clone the DATE machinery into a parallel `timestamp` bucket (`get_datatype` branch, domain
  constants, `timedelta(seconds=…)` helpers, an UPDATE template). Binary search is datatype-agnostic.
- **Risk / limit:** sub-second/timezone precision is noisy at 1-s granularity; `+ interval '1' year`
  keyword form is **Infeasible** (only the resolved boundary is observable). Recover the literal only.
- **Verify by:** `WHERE ts BETWEEN '2024-01-01 09:00' AND '2024-01-01 17:00'`.

#### WI-09 — uuid / bit / varbit / json equality-only point probe
- **Now:** these types are unhandled (same hard raise).
- **Do:** equality-only — read the d-min witness value from the base row, confirm Pop holds, emit
  `(tab,attr,'=',v,v)`. For json restrict to whole-column equality. Pairs with WI-07's graceful skip.
- **Risk:** marginal discriminating value (mostly confirms a constant already in d-min); json on a
  large column is brittle.
- **Verify by:** *(thin in the checklist — no concrete `Qh` given.)*

#### WI-10 — Cross-attribute OR (`A=x OR B=y`, cross-table)
- **Now:** detection is essentially done (the falsify-and-rerun loop produces per-disjunct tuples);
  the failure is a **hard raise** at
  [genPipeline_context.py:300](mysite/unmasque/src/core/dataclass/genPipeline_context.py#L300)
  (`ERROR_007`) for slots spanning >1 distinct `(tab,attr)`.
- **Do:** replace the raise with a structured `cross_attr_disjunctions` field; plumb to QSG like
  `disjunctive_ranges`; emit one parenthesized `(t1.A=x OR t2.B=y)` group per slot reusing the
  gap-aware OR-join idiom.
- **Depends on:** `or=yes`.
- **Risk:** if the single witness row satisfies both branches, the falsifier can mis-attribute which
  branch keeps Pop true (trajectory-convergence) — moderate, not fundamental.
- **Verify by:** `WHERE o_orderstatus='F' OR o_totalprice=100`.

#### WI-12 — Outer join where optional side has NO projected column
- **Now:** [`__create_table_attrib_dict`](mysite/unmasque/src/core/outer_join.py#L179) bails to INNER
  if a relation projects nothing → breaks anti-join / existence-style outer joins.
- **Do:** replace the projected-attribute NULL-witness with a **row-existence witness** — after
  breaking the edge, compare row-count before/after and/or ctid-bisect the optional table to see
  whether a non-matching base row survives (`h`/preserved) or vanishes (`l`).
- **Risk:** `D¹` may lack a guaranteed dangling tuple — may need to materialize one (cs2-style),
  risking perturbation of other stages.
- **Verify by:** *(thin — no concrete `Qh` given.)*

#### WI-13 — Outer-join ON-vs-WHERE for TEXT/LIKE predicates
- **Now:** [`__determine_on_and_where_filters`](mysite/unmasque/src/core/outer_join.py#L351) only
  iterates numeric/date filters; text-equality & LIKE never get ON/WHERE classification → emitted in
  WHERE, turning LEFT JOIN into effectively INNER (the project's own O3 workload hits this).
- **Do:** widen the candidate list to filter-stage text/LIKE/`=` predicates and reuse the existing
  NULL-mutation discriminator [`__check_on_or_where`](mysite/unmasque/src/core/outer_join.py#L362).
  Render path already handles it.
- **Risk:** NOT-NULL text columns resist NULL-mutation; single-row `D¹` makes the `len==1` test
  brittle.
- **Verify by:** O3-style `… ON c_custkey=o_custkey AND o_orderstatus='F'`.

#### WI-15 — Strict vs non-strict single-sided inequality (`<` vs `<=`)
- **Now:** filter always appends inclusive tuples
  ([filter.py:790-799](mysite/unmasque/src/core/filter.py#L790)); QSG has no strict column-vs-const
  op.
- **Do:** after `get_filter_value` finds the boundary, add a strictness probe; add `lt`/`gt` op
  tokens (**audit all predicate consumers** per rule 6) and render branches.
- **Risk / limit:** on continuous numeric the constant is localizable only to `delta=0.01`, so
  strictness is recoverable but the exact bound is not; on discrete int/date, `<c ≡ <= c-1` so strict
  is cosmetic. Guard against sibling-predicate interference.
- **Verify by:** TPC-H Q4/Q6 `o_orderdate < date '1995-03-15'`.

#### WI-16 — ORDER BY on aggregate
- **Now:** [`check_order_by_on_count`](mysite/unmasque/src/core/orderby_clause.py#L91) is **dead
  code** (zero callers); SUM/AVG/MIN/MAX ordering and aggregates absent from SELECT are undetected.
- **Do:** revive + generalize to {COUNT,SUM,AVG,MIN,MAX}: synthesize per-group monotone aggregate
  magnitudes + a reversed second DB, reuse the 2-DB swap. No QSG change.
- **Risk / limit:** an aggregate **not** in SELECT can't be read at `obj.index` → unobservable.
  Ordinal `ORDER BY 2` is observationally identical to by-name → skip.
- **Verify by:** *(thin — no concrete `Qh` given.)*

#### WI-17 — Type-aware / expandable numeric domain bounds (bigint, edge-vs-no-bound)
- **Now:** [`get_min_and_max_val`](mysite/unmasque/src/util/utils.py#L217) hardwires int32
  `±2147483648`; a real constant at the int32 edge is dropped as "no bound"; bigint constants
  undiscoverable.
- **Do:** per-type bounds (bigint `±2^63`); for numeric an **outward-doubling pre-probe** to bracket
  the real range before bisecting; disambiguate edge-vs-open by probing `max+1`.
- **Risk:** "true max" for unbounded numeric is conceptual — cap it; `max+1` must not overflow.
- **Verify by:** *(thin — no concrete `Qh` given.)*

#### WI-18 — OFFSET detection  *(blocked on S1)*
- **Now:** OFFSET exists only as internal ctid paging
  ([MinimizerBase.py:165-168](mysite/unmasque/src/core/abstract/MinimizerBase.py#L165)); no slot, no
  stage; a hidden OFFSET also corrupts the LIMIT card-count.
- **Do:** only when a deterministic ORDER BY was found — insert `N>k+offset` strictly-ordered rows,
  observe which window survives (first `o` absent, `o+1..o+limit` present → OFFSET=`o`). Co-extract
  with LIMIT. Emit via the **S1** `offset_op` slot.
- **Depends on:** **S1**, a recovered ORDER BY.
- **Risk / limit:** without a total order the skipped set is nondeterministic → **Infeasible** for
  no-ORDER-BY queries.
- **Verify by:** *(thin — no concrete `Qh` given.)*

#### WI-19 — Non-equi / theta join (cross-table inequality ON)  *(Moderate→Hard)*
- **Now:** only equality ops are admitted as join edges
  ([equi_join.py:29-30](mysite/unmasque/src/core/equi_join.py#L29)).
- **Do:** classify AOA tuples whose endpoints are on *different* tables, confirm with an
  order-violation re-probe (set the columns to violate the order on `D¹`, require Pop to drop), record
  `theta_join_edges`, render INNER JOIN…ON.
- **Risk / limit (important):** for *inner* joins, `a.x<b.y` in ON vs WHERE is Pop-indistinguishable
  → cosmetic; the real win is *outer* theta joins. Single-row `D¹` gives a high false-positive rate
  → gate behind a flag, require the violation re-probe. **Treat with caution; verify hard.**
- **Verify by:** *(thin — no concrete `Qh` given.)*

#### WI-20 — Cross-join / cartesian validation + labeling
- **Now:** a cross product surfaces only as comma-FROM with no join predicate; never confirmed.
- **Do:** for a table pair with no equi/theta edge, run a row-scaling probe (S2 / CardinalityProbe):
  duplicate a row of `a`, recount `|Qh|`; multiplicative growth `~|a|×|b|` confirms cross product.
  Keep the result-equivalent comma-FROM or emit explicit `CROSS JOIN`.
- **Risk / limit:** **conflates with the k=2 self-join's 4× band**
  ([cardinality_probe.py:46-47](mysite/unmasque/src/core/cardinality_probe.py#L46)); deliverable is a
  *confidence flag*, not new result-correctness (inner cross join is already result-equivalent).
- **Verify by:** *(thin — no concrete `Qh` given.)*

### Tier 3 — Hard (each its own project)

These are sketched in the checklist as standalone projects; details below are the checklist's plan,
distilled. Several are **blocked on S1**.

| ID | Now (gap) | Do (probe + emission) | Key observability limit |
|----|-----------|-----------------------|-------------------------|
| **WI-21** SELECT DISTINCT *(S1)* | No detection, no token | Duplicate base rows on a *multi-row* instance; projected-row count not increasing ⇒ distinct. Emit `Select distinct` via S1. | Strongly confounded (GROUP BY all cols, unique key, `LIMIT 1` all mimic the collapse). `DISTINCT ON` likely Infeasible. |
| **WI-22** HAVING *(S1)* | No post-aggregate stage, no slot | New stage: build a multi-group instance with known aggregate values; steer a group's aggregate to binary-search the cutoff; infer op from boundary. Emit via S1. | Must attribute group-disappearance to HAVING (not WHERE / not-formed); AVG inversion is rounding-lossy. |
| **WI-23** Column alias (AS) *(S1)* | Alias only opportunistically harvested | Subquery-wrap probe `SELECT <name> FROM (<Qh>) sub` to confirm a referenceable label; stop suppressing aliases equal to the base column. | `AS <samename>` is observationally identical to no alias → unrecoverable; unnamed exprs collapse to `?column?`. |
| **WI-24** Self-join degree k≥3 | Hard-capped at k=2 ([cardinality_probe.py:216](mysite/unmasque/src/core/cardinality_probe.py#L216)) | Generalize `_promote_to_k2`→`_promote_to_k(m)` with a degree estimator `k=round(log ratio/log m)`; emit a1..ak. | Degree from a cardinality ratio is data-dependent; k-way wiring may admit multiple topologies. |
| **WI-25** Affine `a.x + k <= b.y` | Coefficient/offset discarded ([aoa.py:202](mysite/unmasque/src/core/aoa.py#L202)) | Record the numeric gap `new_lb − val` as offset `k` on the AOA tuple; render `tab.a + k <= tab.b`. | Single-column `col+k op const` is **Infeasible** (offset folds into the constant). |
| **WI-26** `a + b <= c` | AOA model is single-node only | Fix a,b; binary-search c to the Pop-boundary; collect n+1 affinely-independent points; solve the hyperplane. | Combinatorial subset enumeration; distinguishing `a+b<=c` from `a<=k1 AND b<=k2` needs off-axis points. |
| **WI-27** GROUP BY on (linear) expression | GroupBy iterates base-column names only | Collision probe: two rows differing on `c` collapse into one group ⇒ grouping on a function of `c`; reuse projection's linear solution. | `GROUP BY a+b` vs `(a,b)` needs injected collision pairs; non-linear keys need an enumerated catalog. |
| **WI-28** Restricted single-level DNF | Flat conjunction only | Generalize falsify-and-rerun: extract a full conjunctive *term*, falsify the whole term, repeat. | Mostly **Infeasible** — one witness row activates one term; `(A∧B)∨(A∧C) ≡ A∧(B∨C)` is indistinguishable. |
| **WI-29** Scalar function in SELECT | Projection models polynomial arithmetic only | Post-projection sub-stage, flag-gated: hypothesis-test-by-mutation against a **closed catalog** (round/floor/ceil/abs/upper/lower/length/substring). | Many functions agree on a single `D¹` row → needs multiple input points (fragile when filtered/joined). |
| **WI-30** String functions in predicates | — | Reuse the per-char mutation loop: case-only ⇒ `upper()`/ILIKE; whitespace-only ⇒ TRIM; tail-vs-window ⇒ SUBSTRING. | Each function is a bespoke probe; concatenation in projection is unobservable. |
| **WI-31** Date functions EXTRACT/date_part | — | `EXTRACT(year FROM d)=k` is a periodic/banded region; reuse gap-aware disjoint-interval discovery + a calendar-alignment recognizer. | Data-hungry (needs multiple periods); `now()`/`CURRENT_DATE` is **Infeasible**. |
| **WI-32** NULLS FIRST/LAST *(S1)* | — | After asc/desc, insert a row with a genuine NULL in the sort column; read via a null-tolerant raw path; classify FIRST/LAST. | The null-free invariant fights this; only non-default placement on a nullable column is detectable. |
| **WI-33** INTERSECT | — | UNION's nullify-partition does not generalize (nulling any arm empties the intersection); bespoke arm-discovery; confirm via `EXCEPT ALL`. | Weak partition signal; easily confused with an inner join over the union of tables. |
| **WI-34** EXCEPT | — | Asymmetric: nulling arm B grows the result toward A, nulling arm A empties it; track row-count direction; confirm via `EXCEPT ALL`. | Arm order matters; if B subtracts nothing in `D`, `EXCEPT ≡ plain A`. |
| **WI-35** Uncorrelated scalar-threshold subquery (WHERE) | Filter recovers only the literal | Mutate the candidate inner relation T; see if the recovered WHERE endpoint **moves**. New parenthesized-SELECT node in QSG. | Only the scalar-threshold-in-WHERE shape; IN/ANY subqueries are indistinguishable from IN-lists. |
| **WI-37** Scalar subquery in SELECT | Constant column fits as a constant term | Mutate inner relation T; see whether the projected constant tracks `max/sum/...` of a T column; reconstruct the inner aggregate. | Constant-valued only; correlated is **Infeasible**. |
| **WI-38** CASE (2-branch, single-column) | — | Inject values across a suspected breakpoint; one failed linear fit but two distinct fits either side ⇒ a CASE branch; threshold via filter binary search. New CASE node. | Needs a multi-row probe DB spanning the breakpoint; general/nested CASE explodes. |
| **WI-39** Composite-key / extra ON equality | AOA else-branch assumes `<=` ([outer_join.py:331](mysite/unmasque/src/core/outer_join.py#L331)) | Treat each equi-edge and each `attr=const` as an ON-candidate; per-candidate NULL-mutation discriminator; emit ANDed equalities. | ON-constant vs WHERE-constant on the preserved side is ambiguous on a single `D¹` row. |
| **WI-40** Outer/any join on a non-key column | Driven entirely by `global_join_graph` | An **equi-join-stage** gap: strengthen edge discovery (value-coincidence probing) so the edge appears; the existing outer-join pass then works. | Non-key-pair search explodes and risks false edges. Lower priority. |

---

## 6. Shared enablers

| Enabler | Status | What it unlocks |
|---------|--------|-----------------|
| **S1 — QSG clause-slot infrastructure** | ☐ **Not started** | The missing output slots. Today [`assembleQuery`](mysite/unmasque/src/util/QueryStringGenerator.py#L87-L95) hardcodes six clauses and `QueryDetails` has no `distinct_op`/`having_op`/`offset_op`/`nulls` fields. Add the fields + gated `append_clause` lines (DISTINCT as a `Select distinct` prefix; `Having` after Group By; `Offset` after Limit). **Blocks WI-18 (OFFSET), WI-21 (DISTINCT), WI-22 (HAVING), WI-23 (alias), WI-32 (NULLS).** This is the single highest-leverage piece of unbuilt infrastructure. |
| **S2 — Controlled multi-row / duplicate-row probe** | ✅ **Verified** | [`RowProbe`](mysite/unmasque/src/core/row_probe.py) — duplicate/delete-by-ctid + header-stripped count + list-ctids, with a ctid-set-restore invariant. **Already consumed by WI-05, WI-06, WI-14, WI-36** (and, beyond the checklist's stated scope, retrofitted into `cardinality_probe`/`multiplicity_probe`). It is the basis for every *multiplicity* probe (the minimization tension), and would also underpin WI-20/21/22/24. |

The contrast is the headline planning fact: **S2 is done and already paying off across four shipped
items; S1 is not started and gates four future items.** Anything in the "needs a new clause" family is
stuck behind S1.

---

## 7. Explicitly excluded — fundamentally infeasible under black-box Pop

Recorded so future chats don't re-litigate them. Each is unobservable or ambiguous under the
single-bit, null-free Pop oracle, or would require query-text access.

| Construct | Why it's out |
|-----------|--------------|
| **Correlated subqueries** | Unobservable — single-row `D¹`; correlation needs per-outer-row evaluation. |
| **Arbitrary nested boolean / CNF** | Only logically-equivalent normal forms are recoverable; structure is lost. |
| **Window / analytic functions** (OVER, ROW_NUMBER, RANK, PARTITION BY) | Observability severely constrained. |
| **`IS NULL` on a projected column** | Null-free Pop classifies the whole query as empty → no signal. (`IS NOT NULL` on a non-projected column is at best a Hard detect-and-warn.) |
| **`COALESCE`** | Null-free filtering hides it; mostly cosmetic w.r.t. Pop. |
| **`CAST` / type coercion** | Usually semantics-preserving w.r.t. Pop; cosmetic. |
| **`SELECT *`** | Only the expanded column list is recoverable; the star is cosmetic. |
| **`now()` / `CURRENT_DATE`** | Environment/time-dependent, not a function of mutable DB state. |
| **`+ interval '1' year` keyword fidelity** | Only the resolved boundary literal is observable. |
| **Single-column `col + k op const` offset** | Offset folds into the constant; unidentifiable from data. |
| **Ordinal `ORDER BY 2`** | Observationally identical to by-name ordering; no extraction value. |
| **CROSS JOIN vs a missed join** | Comma-FROM is already result-equivalent; only a confidence flag (WI-20) is meaningful. |

> Two **partial-infeasibility** sub-cases live *inside* otherwise-feasible items and are worth
> remembering: **WI-36 NOT EXISTS polarity** (the result is reconstructed correctly as `EXISTS(¬P)`
> but EXISTS-vs-NOT-EXISTS is unobservable on the single witness `D¹`), and **WI-28 general DNF**
> (only a normal form is recoverable; CNF + arbitrary nesting is out of scope).

---

## 8. Progress timeline

Rendered **oldest → newest** (chronological build order) so it reads as the narrative of how the
work landed. Dates are from the checklist's progress log.

| Date | Milestone |
|------|-----------|
| 2026-06-01 | **Checklist created** from the code-verified coverage audit (34-agent audit; `/tmp/audit_result.json`). |
| 2026-06-01 | **WI-01 ✅** — COUNT render gate fixed; surfaced the *latent* finding (the pipeline reconstructs `count(col)` as `count(*)` until detection-side work lands). |
| 2026-06-01 | **WI-02 ✅** — LIMIT exponential+binary search; the cap is now cheap to raise (recovers `L=no_rows`, inserts scale `~2L`). |
| 2026-06-02 | **S2 ✅ + WI-05 ✅** (shipped as one unit: enabler + first consumer). `RowProbe` factored out; const-1-vs-COUNT made sound via a duplicate-row probe. |
| 2026-06-02 | **WI-06 ✅** — COUNT(DISTINCT) + companion COUNT(col); first observable payoff of WI-01. |
| 2026-06-02 | **WI-03 ✅** — outer-join equivalence moved to both-directions `EXCEPT ALL` bag diff (the check WI-11 needs). |
| 2026-06-02 | **WI-11 ✅** — outer joins wired ON by default (marker-based routing; `outer_join=yes`). Footgun noted: `outer_join=yes` + `gap_aware=yes` → gap-witness cartesian hang. |
| 2026-06-02 | **WI-14 ✅** — UNION vs UNION ALL dedup discrimination via `SetOpProbe`. |
| 2026-06-02 | **Union FROM-clause bugfix ✅** — `_as_relation_list` normalizes the swallowed `"OK "` status string; unblocks single-table UNION branches and the non-latent WI-14 overlap demo. |
| 2026-06-03 | **WI-36 ✅** — uncorrelated EXISTS gate detect+emit; NOT EXISTS reconstructed result-correct as `EXISTS(¬P)` (polarity recovery specified as future work). |

---

## 9. Recommended next work

Ordered by impact × ease, starting from the checklist's ranking snapshot and applying this report's
judgment. **Bold = highest value.**

1. **S1 — QSG clause-slot infrastructure (Moderate, enabler).** Build it next. It is the single
   biggest unblocker: four distinct items (**WI-18 OFFSET, WI-21 DISTINCT, WI-22 HAVING, WI-23
   alias**) — including the two highest-value Hard items — cannot ship without it. Pure-emission,
   no oracle risk, unit-testable by rendering a `QueryDetails` with each slot set. Doing S1 turns a
   cluster of "blocked" items into "available."
2. **The `gap_aware × multi-table` footgun (bugfix, now default-relevant).** Because `outer_join=yes`
   and `gap_aware=yes` are *both* live defaults ([config.ini](mysite/config.ini#L16-L24)), a numeric
   filter over a multi-table outer join makes the gap-witness Filter stage cartesian-join all FROM
   tables and hang ([`_build_qe`@gap_witness.py:203](mysite/unmasque/src/core/gap_witness.py#L203)).
   Scope the gap-witness `Re` to the attribute's own table. This is a **shipped-default correctness/
   liveness** issue, so it outranks new features.
3. **WI-04 — within-attribute OR→IN (Easy).** The last open Easy item; cheap, default-on, reuses the
   existing IN render — high value-to-effort.
4. **WI-10 — cross-attribute OR (Moderate).** Detection is *essentially already done* (the
   falsify-and-rerun loop produces the tuples); the work is replacing a hard `ERROR_007` raise with a
   plumbed `cross_attr_disjunctions` field. Disproportionate payoff for the effort.
5. **WI-07 / WI-08 (Moderate, datatypes).** WI-07's **log-and-skip** half is broadly valuable on its
   own — it stops one exotic column from aborting an entire run — independent of the boolean probe.
   WI-08 is a mechanical clone of the proven DATE machinery.
6. **WI-36 polarity probe (Hard, faithful NOT EXISTS).** A clean, well-specified follow-up to a
   shipped item: insert a `¬Q`-row — gate stays open ⇒ EXISTS, closes ⇒ NOT EXISTS. Would let the
   already-defined-but-unused `NOT_EXISTS_GATE` constant finally be emitted.
7. **Then the rest of Moderate**, then the Hard items as standalone projects — with **WI-22 HAVING**
   and **WI-38 CASE** the highest-value Hard items (both ride on S1 / multi-row probing).

**Blocked on S1 (do S1 first):** WI-18, WI-21, WI-22, WI-23, WI-32.
**Approach with caution / verify hard:** WI-19 (theta join — single-row `D¹` gives a high
false-positive rate; require the order-violation re-probe behind a flag).

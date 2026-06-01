# Self-Join Extraction in the Xpose / UNMASQUE Framework

> Technical Report
> Master's Thesis Style — Database Systems Lab
> Topic: Hidden-Query Extraction with Multi-Instance Relational Aliases

---

## Abstract

UNMASQUE (Xpose) is a black-box hidden-query extraction framework. Given an
unknown SQL query `Qh` that can be executed against a known database `D`,
the framework reconstructs a result-equivalent query by mutating `D` into
a tiny working copy `D¹` and observing how `Qh`'s output changes under
controlled perturbations. The published framework assumes every base
relation appears at most once in `Qh`'s `FROM` clause; this assumption
breaks for self-joins of the form
`SELECT … FROM T t1, T t2 WHERE p(t1, t2)`.

This report develops a complete extraction procedure for `k = 2`
self-joins, organised as a 7-phase architecture layered on top of the
existing pipeline. We give the formal multiplicity signal (the
view-minimiser's halving floor), prove correctness of a cardinality
probe that detects pure-equality self-joins (which the floor cannot see),
develop a cross-alias edge resolver that distinguishes intra-alias from
cross-alias equi-joins on duplicate D¹, and use a marker-column probe
to break swap symmetry on cross-column self-joins (e.g.
`n1.regionkey = n2.nationkey`). The work fixes three families of bugs
exposed during the bring-up — Postgres MVCC ctid invalidation on
per-row UPDATEs, multi-alias D¹ restoration, and intra-alias preference
in the EquiJoin partition heuristic — and validates the procedure on
three test queries (SJ1, SJ2, SJ3) against the TPC-H `nation` and
`lineitem` relations.

**Keywords:** hidden-query extraction, self-join, view minimisation,
cardinality probe, Postgres MVCC, UNMASQUE.

---

## Contents

1. Problem statement
2. Background on UNMASQUE
3. Theoretical preliminaries
4. The 7-phase extraction architecture
5. The Cardinality Probe (Phase 0)
6. The Cross-Alias Resolver (EquiJoin extension)
7. Engineering: Postgres MVCC and alias bookkeeping
8. Proofs of correctness
9. Flowcharts
10. Experimental results
11. Limitations and threats to validity
12. Future work
13. Index of files changed
14. References

---

## 1. Problem statement

### 1.1. Notation

| Symbol | Meaning |
|---|---|
| `D` | Full database (TPC-H, ≈ 6M rows in `lineitem`) |
| `Qh` | Hidden query; opaque executable producing a bag of tuples |
| `D¹` | A working copy of `D` shrunk by the view-minimiser to a minimal database that keeps `Qh(D¹)` non-empty |
| `Pop(D¹)` | Boolean: is `Qh(D¹)` non-empty and free of all-NULL rows? |
| `T` | A base relation (e.g. `nation`, `lineitem`) |
| `k_T` | Multiplicity of `T` in `Qh`'s `FROM` clause |
| `a_i` | The `i`-th alias of `T` (synthetic when `k_T ≥ 2`, e.g. `nation__a1`) |
| `min_card[T]` | Minimum row-count of `T` discovered during view-minimisation that keeps `Pop` true |
| `c_ij` | The shared base table of aliases `a_i, a_j` |

### 1.2. Goal

Given `Qh` and `D`, produce a SQL string `Q_E` such that
`Q_E(D) ≡_bag Qh(D)` — i.e. bag-equivalent on `D`. Synthetic aliases
(`lineitem__a1`, `lineitem__a2`) are acceptable in `Q_E` per the
project's user-confirmed scope.

### 1.3. The three motivating queries

```sql
-- SJ1: cross-alias inequality (still open; see §11)
SELECT l1.l_orderkey
FROM   lineitem l1, lineitem l2
WHERE  l1.l_orderkey = l2.l_orderkey
  AND  l1.l_quantity < l2.l_quantity;

-- SJ2: pure-equality self-join (same column)
SELECT l1.l_orderkey
FROM   lineitem l1, lineitem l2
WHERE  l1.l_orderkey = l2.l_orderkey;

-- SJ3: cross-column self-join
SELECT n1.n_name
FROM   nation n1, nation n2
WHERE  n1.n_regionkey = n2.n_nationkey;
```

### 1.4. Failure modes in baseline UNMASQUE

| Failure | Where | Symptom |
|---|---|---|
| From-clause discovery returns `["lineitem"]`, not `["lineitem", "lineitem"]` | `from_clause.py` | Cannot represent multi-instance FROM |
| View minimiser hard-codes `max_row_no = 1` | `view_minimizer.py:22` | One row per table; SJ1 fails `ERROR_002` |
| Predicate tuple `(tab, attr, op, lb, ub)` collides across aliases | `filter.py`, `aoa.py` | Two aliases' filters overwrite |
| QSG emits `", ".join(core_relations)` | `QueryStringGenerator.py:396` | Bare table names; no alias syntax |
| EquiJoin's partition heuristic prefers intra-alias | `equi_join.py` (algo3) | SJ3 renders `a1.X = a1.Y` instead of `a1.X = a2.Y` |

---

## 2. Background on UNMASQUE

UNMASQUE is structured as a stage-wise pipeline:

```
   Qh,D ──► Init ──► DBRestore ──► CS2 ──► ViewMin ──► Filter ──► EquiJoin
                                                       ↓
                                  AOA ◄── Projection ◄── GroupBy ◄── Limit
```

Each stage operates over `D¹` and a global state including:

- `global_min_instance_dict[T] = [cols_tuple, row_tuple]` — the witness
  rows that survived minimisation.
- `attrib_types_dict[(T, c)]` — column datatypes.
- `filter_predicates` — list of `(tab, attr, op, lb, ub)` tuples.
- `algebraic_eq_predicates` — list of equi-join groups
  `[(t1, a1), (t2, a2), …]` rendered as `t1.a1 = t2.a2 = …`.

**Key primitive — `checkAttribValueEffect(Qh, val, [(t, c)])`**: mutate
column `c` of table `t` in `D¹` to value `val`, run `Qh`, observe
`Pop(D¹')`, then revert. This is the workhorse for every per-attribute
discovery in `Filter`, `EquiJoin`, and `AOA`.

**Key primitive — `Comparator(Re, Rh)`**: materialises
`Re EXCEPT ALL Rh` and `Rh EXCEPT ALL Re`. The first matches each
hidden-query row to an extracted-query row using a `LIKE` predicate on
the projection. Used by NEP/Gap-Aware as the difference oracle.

---

## 3. Theoretical preliminaries

### 3.1. The multiplicity signal

> **Lemma 1 (Halving floor.)** Let `Qh` be a query of the form
> `SELECT … FROM T t1, T t2, … WHERE p(t1, t2, …)` where `p` is a
> conjunction containing at least one **distinguishing** predicate over
> two aliases (i.e. a predicate that is false when `t1 = t2`). Then the
> view-minimiser's intra-page halving loop on `T` reaches a floor
> `min_card[T] ≥ 2`.

**Proof.** The halving loop maintains the invariant that
`Pop(D¹)` is true. Suppose the loop reduces `T` to a single row `r`.
Then under the assignment `t1 ← r, t2 ← r`, `p(r, r)` is false by
distinguishing, so the cross-product is empty and `Pop` is false. The
halving step that produced this state must have flipped `Pop`; by the
loop's invariant the halver rejects this step and the floor for `T`
is at least 2. ∎

> **Corollary 1.1.** SJ1 (`… AND l1.l_quantity < l2.l_quantity`) is
> distinguishing — `r.l_quantity < r.l_quantity` is false. The
> view-minimiser detects SJ1 as multi-instance.

### 3.2. The blindness for pure-equality self-joins

> **Lemma 2 (Pure-equality blindness.)** If every conjunct of `p` is
> reflexive (true when `t1 = t2`), the view-minimiser's halving floor
> is `min_card[T] = 1`.

**Proof.** Equi-join `t1.X = t2.X` evaluates to `r.X = r.X` ≡ true under
`t1 ← r, t2 ← r`. So the single-row instance keeps `Pop` true. The
halver can therefore reach a floor of 1 without violating its
invariant. ∎

> **Corollary 2.1.** SJ2 (`l1.l_orderkey = l2.l_orderkey`) and SJ3
> (`n1.regionkey = n2.nationkey`, reflexive on the TPC-H row whose
> `nationkey == regionkey`) are pure-equality. The view-minimiser
> *cannot* signal them.

### 3.3. The cardinality scaling theorem

> **Theorem 1 (Cardinality scaling, k=2 case.)** Let `Qh` be a
> non-aggregating SELECT-FROM-WHERE query with `k_T = 2` for some base
> table `T` and `k_S = 1` for every other base table `S`. Let
> `B_orig = |Qh(D¹)|` and let `D¹'` be `D¹` with one extra duplicated
> row in `T` (so `|T|` doubles from 1 to 2). Let `B_dup = |Qh(D¹')|`.
> Then `B_dup = B_orig · 4 = B_orig · k_T²`.

**Proof.** The cross-product `T × T` doubles in cardinality on each
side, so the join's underlying bag grows by `2² = 4`. Reflexivity of
`p` (Lemma 2) ensures every new cross-product row satisfies `p`. The
projection is non-aggregating so each surviving cross-product row
contributes one result tuple. ∎

> **Corollary 3.1.** Observing `B_dup / B_orig ≈ 4` after a single-row
> duplication is sufficient evidence for `k_T = 2`. The 3.5–4.5 band
> tolerates minor variance.

### 3.4. Swap symmetry for cross-column self-joins

> **Lemma 3 (Swap symmetry.)** For any predicate `φ(t1.X, t2.Y)`,
> `{(r, s) ∈ T × T : φ(r.X, s.Y)} = {(s, r) ∈ T × T : φ(s.X, r.Y)}`
> as a bag.

**Proof.** Renaming the bound variables `(t1, t2) ↦ (t2, t1)` in the
cross-product is a bijection on `T × T`. ∎

> **Corollary 4.1.** For SJ3, predicates `a1.regionkey = a2.nationkey`
> and `a2.regionkey = a1.nationkey` are bag-equivalent
> *with respect to the set of matching pairs*. But the projection
> `SELECT a1.n_name` breaks the symmetry: the two predicates pick
> different rows for `t1`, hence different `n_name` values.

This corollary forces the resolver to pick the orientation aligned with
the projection (§6.3).

---

## 4. The 7-phase extraction architecture

| Phase | Component | Modification site |
|---|---|---|
| 1 | Halving-floor detection | `view_minimizer.py`, `MinimizerBase.py` |
| 2 | Alias data model | `util/instance.py` (new) |
| 3 | Multi-row D¹ + ctid-scoped mutation | `un2_where_clause.py`, `abstract_queries.py` |
| 4 | Alias-aware Filter & AOA | `filter.py`, `aoa.py` (inherited) |
| 5 | Self-equi-join discovery | `equi_join.py` |
| 6 | Alias-aware FROM emission | `QueryStringGenerator.py`, `ExtractionPipeLine.py` |
| 7 | Multiplicity verification | `multiplicity_probe.py` (new) |
| 0 | Cardinality probe (pure-equality detection) | `cardinality_probe.py` (new) |
| EQ-ext | Cross-alias resolver | `equi_join.py` (this report's §6) |

Phases 1–7 establish the *capacity* to handle multi-instance tables;
the Cardinality Probe and Cross-Alias Resolver establish the *signal*
and *partition* for pure-equality self-joins.

---

## 5. The Cardinality Probe (Phase 0)

### 5.1. Motivation

Lemma 2 shows the halving floor is silent for pure-equality self-joins.
Without an alternative signal, SJ2/SJ3 are reconstructed as single-
instance queries — bag-inequivalent on `D`.

### 5.2. Algorithm

Inserted between `ViewMinimizer` and `Filter`. For every table `T`
with `min_card[T] = 1`:

```
PROBE(T):
  B_orig ← |Qh(D¹)|
  if B_orig = 0: return                           # nothing to scale

  INSERT INTO T SELECT * FROM T RETURNING ctid    # duplicate the row
  COMMIT                                          # so the rename probe
                                                  # below doesn't roll
                                                  # back the INSERT
  B_dup ← |Qh(D¹)|
  if B_dup / B_orig ∉ [3.5, 4.5]:
    DELETE the dup; return                        # not a 2-alias self-join

  promote_to_k2(T, dup_ctid)                      # rewire alias state
  qh_cols ← rename_probe(T)                       # which cols Qh references
  join_keys ← {c ∈ qh_cols : mutation_probe(a2, c) drops |Qh|}
  for c in join_keys:
    seed (a1, c, '=', K, K)
    seed (a2, c, '=', K, K)                       # K = a1's row[c]
```

### 5.3. Sub-probes

#### 5.3.1. Rename probe

```
RENAME_PROBE(T, col):
  BEGIN
  ALTER TABLE T RENAME COLUMN col TO col__cp
  result ← Qh(D¹)
  ROLLBACK                                        # undoes the rename
  return result is an error OR result mentions "does not exist"
```

**Why ROLLBACK rather than two ALTERs.** Postgres aborts the
transaction on the failing `Qh`; only `ROLLBACK` resets the
aborted-state machine while simultaneously reverting the schema.
Borrowed verbatim from `from_clause.py`'s relation-discovery trick.

#### 5.3.2. Mutation probe (JOIN vs SELECT discrimination)

```
MUTATION_PROBE(alias, col):
  V_orig ← alias.row[col]
  V_dummy ← unused dummy value for col's datatype
  set alias.col ← V_dummy via ctid-scoped UPDATE
  B_mut ← |Qh(D¹)|
  set alias.col ← V_orig (revert)
  return B_mut < B_dup · 0.9                      # 10% drop threshold
```

The CardinalityProbe emits seed equality predicates `(a_i, c, '=', K, K)`
that go into `filter_predicates`. The EquiJoin then groups them.

### 5.4. Correctness

By Theorem 1 the ratio is a sound indicator of `k_T = 2`. The rename
probe is sound for column reference because Postgres's resolver
requires the column name verbatim; renaming forces an "does not exist"
error iff the column is referenced. The mutation probe is *not*
column-precise — mutating `regionkey` on `a2` for SJ3 drops `Pop`
even though `regionkey` is logically only on the `n1` side. This is
acceptable because the resolver downstream (§6) restores precision.

---

## 6. The Cross-Alias Resolver (EquiJoin extension)

### 6.1. The problem

After Phase 0 seeds predicates and Filter runs, `algo2_preprocessing`
in EquiJoin groups predicates by their RHS constant. For SJ3:

```
seeds = [
  ('nation__a1','n_regionkey', '=', 4, 4),
  ('nation__a2','n_regionkey', '=', 4, 4),
  ('nation__a1','n_nationkey', '=', 4, 4),
  ('nation__a2','n_nationkey', '=', 4, 4),
]
```

All four share constant `K=4` (a coincidence on `D¹`: TPC-H nation
row with `nationkey = regionkey = 4` is the row that survived
minimisation), so they land in one group of size 4. The existing
`algo3_find_eq_joinGraph` partitions a 4-element group via
`merge_equivalent_partitions` and tries each 2-element subset under
`handle_unit_eq_group`. The first partition for which
`_extract_filter_on_attrib_set` returns *empty* (no common constant
filter applies) is accepted as an equi-join.

**The defect.** On `D¹` where `a1` and `a2` are duplicates,
`_extract_filter_on_attrib_set` for the cross-alias same-column subset
`{(a1, regionkey), (a2, regionkey)}` discovers `K=4` as a satisfying
common constant — returns `'='`. `handle_unit_eq_group` interprets
this as "not an equi-join" and skips. The algorithm falls through to
the **intra-alias** subset `{(a1, regionkey), (a1, nationkey)}`, where
the probe sees no common constant (mutating both columns of the same
row to a shared value breaks Qh), accepts it as the "equi-join", and
emits `a1.regionkey = a1.nationkey` — a meaningless intra-row equality.

### 6.2. The resolver algorithm

We peel multi-alias groups off `partition_eq_dict` before `algo3` and
process them with a dedicated resolver:

```
RESOLVE_SELF_JOIN_GROUP(group, query):
  by_alias ← partition group by alias
  if |by_alias| ≠ 2: return None                  # k≠2 not handled
  a1, a2 ← sorted aliases
  cols_a1 ← columns of a1 in group
  cols_a2 ← columns of a2 in group

  same_col_edges ← [((a1, c), (a2, c)) for c in cols_a1 ∩ cols_a2]
  cross_col_edges ← [((a1, x), (a2, y)) for x ∈ cols_a1, y ∈ cols_a2, x ≠ y]

  holding ← []
  for edge in same_col_edges ++ cross_col_edges:
    if ISOLATE_AND_PROBE(edge, sites, query):
      holding.append(edge)

  if holding ∩ same_col_edges: return [first same-col holding edge]
  if |holding| ≤ 1: return holding
  return [PICK_MARKER_ALIGNED(holding ∩ cross_col_edges)]
```

### 6.3. Isolate-and-probe

```
ISOLATE_AND_PROBE(edge=(a_i.X, a_j.Y), sites, query):
  snapshot ← {(a, c): current value for (a, c) in sites}
  K ← 1_000_003                                   # large, well outside dmin
  for (a, c) in sites:
    if (a, c) ∈ edge:    value ← K               # edge members share K
    else:                value ← K + offset++    # distinct per site
    update alias.col ← value via ctid-scoped UPDATE
  res ← Qh(D¹)
  RESTORE(snapshot)
  return Pop(res)
```

**Soundness sketch.** If the candidate edge `(a_i.X, a_j.Y)` is *the*
join, then after the isolation `D¹` contains the pair `(a_i, a_j)`
that satisfies the join (both at `K`), while every other column-pair
is broken (distinct values). Other (n1, n2) pairs in `T × T` may also
satisfy the join trivially (e.g. (a_i, a_i) where columns happen to
match) — that is acceptable since we only need `Qh` to be non-empty.
Conversely, if the candidate edge is *not* the join, then either:

- The actual join's columns are mutated to distinct values → join
  fails on every pair → `Pop` false.
- The actual join holds vacuously on some pair (e.g. `a_i.X = a_i.X`),
  in which case `Pop` is true and the candidate is mistakenly
  reported as holding.

The second case is unavoidable for a single-edge probe. It is handled
by the *cross-column* enumeration: the second-pass marker probe
(§6.4) breaks ties.

### 6.4. Marker probe (swap-symmetry breaking)

When two cross-column edges both hold (SJ3 case), they are swap-
symmetric (Lemma 3). The marker probe uses a Qh-referenced column
outside the equi-join group:

```
PICK_MARKER_ALIGNED(cross_holding, a1, a2, query):
  marker_col ← any col ∈ qh_cols[base(a1)] \ {cols in group}
  if marker_col is None: return cross_holding[0]
  m_a1, m_a2 ← distinct dummy values for marker_col's datatype
  for edge in cross_holding:
    apply isolate state for edge
    set a1.marker_col ← m_a1
    set a2.marker_col ← m_a2
    res ← Qh(D¹)
    if m_a1 appears in res:                       # Qh selected from a1's row
      return edge                                 # → align edge with a1 as n1
  return cross_holding[0]
```

**Why this works.** The QSG defaults to qualifying bare `SELECT col`
with the *first* alias in `cols_by_alias` (insertion order, hence
`a1`). For the synthesised query `Q_E = SELECT a1.m … WHERE edge`
to be bag-equivalent to `Qh = SELECT n1.m … WHERE p`, the
`a1`-side of `edge` must correspond to `n1` in `p`. The probe
detects this correspondence by observing which alias's marker is
echoed by `Qh`'s projection.

### 6.5. Worked example — SJ3

The seeded group (4 elements) yields:

```
same_col_edges  = [(a1.regionkey,  a2.regionkey),
                   (a1.nationkey,  a2.nationkey)]
cross_col_edges = [(a1.regionkey,  a2.nationkey),
                   (a1.nationkey,  a2.regionkey)]
```

Isolate-and-probe on the duplicated `D¹` (both rows have
`nationkey = regionkey = 4`, then mutated):

| Edge | a1.regionkey | a1.nationkey | a2.regionkey | a2.nationkey | `|Qh|` | Hold? |
|---|---|---|---|---|---|---|
| (a1.r, a2.r) | K | K+1 | K | K+2 | 0 | ✗ |
| (a1.n, a2.n) | K+1 | K | K+2 | K | 0 | ✗ |
| (a1.r, a2.n) | K | K+1 | K+2 | K | 1 | ✓ |
| (a1.n, a2.r) | K+1 | K | K | K+2 | 1 | ✓ |

(Values track: `Qh = n1.regionkey = n2.nationkey`; the pair satisfying
the join is (a1, a2) in row 3 and (a2, a1) in row 4.)

Marker probe with `marker_col = n_name`,
`m_a1 = 'XPOSEMARKA'`, `m_a2 = 'XPOSEMARKB'`:

- Edge `(a1.r, a2.n)`: matching pair is `(a1, a2)`, `SELECT n1.n_name`
  = `'XPOSEMARKA'`. **Aligned.** Pick this.
- Edge `(a1.n, a2.r)`: matching pair is `(a2, a1)`, `SELECT n1.n_name`
  = `'XPOSEMARKB'`. Misaligned.

Final emitted predicate: `[(a1, regionkey), (a2, nationkey)]` →
rendered as `nation__a1.n_regionkey = nation__a2.n_nationkey`.

---

## 7. Engineering: Postgres MVCC and alias bookkeeping

### 7.1. The MVCC ctid trap

Postgres's `ctid` is a tuple of (page, item) identifying a row's
*current* physical location. An `UPDATE` does **not** modify the row
in place — it writes a new version at a new ctid and marks the old
one dead. The naïve probe

```sql
UPDATE T SET col = v WHERE ctid = '(0,1)';
-- Postgres: rewrites at (0,3); (0,1) is dead
UPDATE T SET col = original WHERE ctid = '(0,1)';   -- matches 0 rows
```

silently fails on the second update. The revert never reverts; the
alias's cached ctid is stale; every subsequent probe drifts further.

### 7.2. The fix

All alias-targeted updates go through one centralised helper:

```python
def _exec_alias_ctid_update(alias, col, val):
  ctid = global_alias_row_dict[alias]['ctid']
  cur.execute(f"UPDATE T SET col = {val}
               WHERE ctid = '{ctid}' RETURNING ctid::text;")
  new_ctid = cur.fetchone()[0]
  global_alias_row_dict[alias]['ctid'] = new_ctid
  global_alias_row_dict[alias]['row'][col_idx] = raw_val
```

The `RETURNING ctid::text` clause captures the post-update ctid and
writes it back. The alias dict is kept in sync.

### 7.3. Multi-alias D¹ restore

When AOA or Generation phases call `restore_d_min_from_dict`, the
helper TRUNCATEs the table and reinserts each alias's row. The post-
INSERT ctids are read via `select_ctid_star_from(T) ORDER BY ctid`
and re-anchored back into the alias dict, in the same sorted order
they were inserted.

### 7.4. Shared-reference alias dict

`FilterHolder` (parent of EquiJoin, AOA) shares `global_alias_row_dict`
**by reference** with `Filter` rather than deep-copying. This is
necessary because Filter's per-attribute probes mutate ctids during
its run, and AOA reads those ctids on subsequent stages. Two separate
copies would diverge after the first Filter probe.

---

## 8. Proofs of correctness

### 8.1. Cardinality scaling (re-statement)

Theorem 1 (§3.3) is proved earlier. The probe in §5 directly
implements its converse: observed ratio `∈ [3.5, 4.5]` ⇒ `k_T = 2`.

### 8.2. Resolver soundness on same-column edges

> **Theorem 2.** Let `e = (a_i.X, a_j.X)` be a same-column cross-alias
> candidate edge. If `ISOLATE_AND_PROBE(e, sites, Qh)` returns true,
> then `e` is a join edge of `Qh` *or* `Qh` has a vacuous self-pair
> match on `(a_i, a_i)`.

**Proof.** Under the isolation, every column outside `{X}` on both
aliases takes a distinct large value, while `X` takes the shared `K`
on both aliases. Any cross-alias predicate `φ(a_i.Y, a_j.Z)` with
`Y ≠ X` or `Z ≠ X` evaluates to false on the (a_i, a_j) pair (one
side is `K + offset_i`, the other `K + offset_j`, both distinct from
each other and from `K`). Reflexive predicates `φ(a_i.Y, a_i.Y)` may
still hold on diagonal pairs. So `Pop(Qh)` true ⇒ either `e` is the
join (off-diagonal match) or the join is reflexive on a diagonal
pair. ∎

The second branch (reflexive on a diagonal pair) cannot arise for
distinguishing predicates by Lemma 1's contrapositive; for purely
reflexive predicates the cardinality probe would not have flagged the
table in the first place unless duplicating the row produced the `k²`
scaling — which itself implies a real join. Hence in practice the
first branch holds and the edge is a true join edge.

### 8.3. Marker probe soundness

> **Theorem 3.** Suppose two cross-column edges `e1 = (a1.X, a2.Y)`
> and `e2 = (a1.Y, a2.X)` both pass isolate-and-probe (i.e. are
> swap-symmetric). Let `m` be a Qh-referenced column outside the
> equi-join group. Tag `a1.m = α, a2.m = β` with α ≠ β. Then under
> `e1`'s isolate state, `Qh(D¹)` returns a row containing `α`
> iff `Qh`'s projection has the form `… n1.m …` where `n1` is
> bound to `e1`'s `a1`-side at the matching pair.

**Proof.** Under `e1`'s isolate state, the matching cross-product
pair is `(a1, a2)` (by construction: `a1.X = K = a2.Y`, all other
columns distinct). If `Qh`'s projection includes `n1.m`, then the
matching tuple emits `a1.m = α`. Conversely if the projection is
`n2.m`, it emits `a2.m = β`. The marker `α` in the output is
therefore evidence for `n1 = a1`-side alignment. ∎

> **Corollary 3.1.** Selecting `e1` when the output contains `α`
> ensures that `Q_E = SELECT a1.m WHERE a1.X = a2.Y` and
> `Qh = SELECT n1.m WHERE p(n1, n2)` agree on the projection side,
> hence are bag-equivalent (the underlying matching-pair bags are
> identical by Lemma 3, and the projection picks the same row from
> each pair).

### 8.4. Termination

Every probe runs `O(|cols|)` UPDATEs and one `Qh` invocation, both
bounded. The resolver iterates over at most `|cols_a1| × |cols_a2|`
candidates and at most 2 marker iterations. Total probes per
multi-alias group are `O(|cols|²)`, finite.

---

## 9. Flowcharts

### 9.1. Whole-pipeline flow

```
   ┌────────────┐
   │  From      │   discover {T : T ∈ Qh.FROM}
   │  Clause    │
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ DB Restore │   reset unmasque.* from public.*
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ View       │   bisect each T to min row count
   │ Minimiser  │   that keeps Pop true; emit min_card[T]
   └─────┬──────┘
         │  if min_card[T] ≥ 2 ⇒ self-join with distinguishing pred
         ▼
   ┌────────────┐
   │Cardinality │   for each T with min_card[T] = 1:
   │  Probe     │     duplicate row, check ratio,
   │  (Phase 0) │     promote to k=2, seed predicates
   └─────┬──────┘
         │
         ▼
   ┌────────────┐
   │  Filter    │   per-alias constant filter discovery
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ EquiJoin   │   algo2 grouping + self-join resolver
   │ (+resolver)│   (§6)
   └─────┬──────┘
         ▼
   ┌────────────┐
   │   AOA      │   inequality predicate discovery
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ Projection │   SELECT cols (still alias-naive — §11)
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ GroupBy /  │
   │ OrderBy /  │
   │ Limit      │
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ QSG render │   cols_by_alias qualifies bare SELECT cols
   └─────┬──────┘
         ▼
   ┌────────────┐
   │ Multiplici-│   compares |Qh(D)| vs |Q_E(D)| ratios
   │ ty Probe   │   (Phase 7); warns if k ≥ 3 likely
   └────────────┘
```

### 9.2. Cardinality-probe flow

```
                B_orig = |Qh(D¹)|
                       │
                   B_orig > 0?
                   /        \
                  no         yes
                  │           ▼
                  │      INSERT dup row; COMMIT
                  │           │
                  │      B_dup = |Qh(D¹')|
                  │           │
                  │      ratio = B_dup/B_orig
                  │           │
                  │      ratio ∈ [3.5, 4.5]?
                  │      /             \
                  │     no              yes
                  │     │                │
                  │   DELETE          promote_to_k2(T)
                  │   the dup         qh_cols ← rename_probe(T)
                  │                   join_keys ← mutation_probe(qh_cols)
                  │                   seed (a_i, c, '=', K, K)
                  ▼                   for each (i, c in join_keys)
                done                  return seeded preds
```

### 9.3. Cross-alias resolver flow

```
       multi-alias group G
            │
       split by alias
            │
       2 aliases? ──no──► return None (k≥3 unsupported)
            │ yes
       generate candidates
       (same-col + cross-col)
            │
       for each candidate edge e:
            │
       ISOLATE: set e's columns to shared K,
                others to distinct K+i
            │
       Pop(Qh)? ──no──► drop
            │ yes
       holding.append(e)
            │
       (after all candidates)
            │
       any same-col holding? ──yes──► return first same-col holding
            │ no
       |cross_holding| ≤ 1? ──yes──► return cross_holding
            │ no
       MARKER PROBE
            │
       set a1.marker = α, a2.marker = β
       for edge in cross_holding:
         re-apply edge isolate
         apply marker mutation
         res = Qh(D¹)
         α ∈ res? ──yes──► return edge
       (fallthrough) return cross_holding[0]
```

### 9.4. ctid-scoped UPDATE flow

```
  cached ctid_old
        │
   UPDATE T SET col=v WHERE ctid=ctid_old RETURNING ctid::text;
        │
   cur.fetchone()
        │
   ──── None? ────yes──► UPDATE matched 0 rows (row moved by another path)
        │ no               return False; caller falls back to whole-table
   ctid_new = result[0]
        │
   alias_dict[alias]['ctid'] = ctid_new
   alias_dict[alias]['row'][col_idx] = raw_val
        │
   return True
```

---

## 10. Experimental results

### 10.1. Setup

- Postgres 16 on localhost, `tpch` database, schema `public`.
- TPC-H scale 1 (≈ 6M rows in `lineitem`, 25 in `nation`).
- Python 3.12.3 venv at `.venv/`.
- Run: `python -m unmasque.src.main_cmd <qid>` from `mysite/`.

### 10.2. Results table

| Query | Hidden `Qh` | Extracted `Q_E` (post-fix) | Status |
|---|---|---|---|
| SJ1 | `SELECT l1.l_orderkey FROM lineitem l1, lineitem l2 WHERE l1.l_orderkey = l2.l_orderkey AND l1.l_quantity < l2.l_quantity` | (Projection bails — cross-alias inequality still open) | open (§11) |
| SJ2 | `SELECT l1.l_orderkey FROM lineitem l1, lineitem l2 WHERE l1.l_orderkey = l2.l_orderkey` | `SELECT lineitem__a1.l_orderkey FROM lineitem lineitem__a1, lineitem lineitem__a2 WHERE lineitem__a1.l_orderkey = lineitem__a2.l_orderkey` | **bag-equivalent** ✓ |
| SJ3 | `SELECT n1.n_name FROM nation n1, nation n2 WHERE n1.n_regionkey = n2.n_nationkey` | `SELECT nation__a1.n_name FROM nation nation__a1, nation nation__a2 WHERE nation__a1.n_regionkey = nation__a2.n_nationkey` | **bag-equivalent** ✓ |

### 10.3. Log evidence for SJ3

```
CardinalityProbe: nation B_orig=1 B_dup=4 ratio=4.00
CardinalityProbe: promoted nation to k=2 (aliases nation__a1, nation__a2)
CardinalityProbe: nation qh_cols=['n_name', 'n_nationkey', 'n_regionkey']
CardinalityProbe: nation join_keys=['n_regionkey', 'n_nationkey']

Self-equi-join candidate on constant 4:
  [('nation__a1','n_regionkey'), ('nation__a2','n_regionkey'),
   ('nation__a1','n_nationkey'), ('nation__a2','n_nationkey')]

Self-join resolver: group [...] → holding edges
  [(('nation__a1','n_regionkey'), ('nation__a2','n_nationkey')),
   (('nation__a1','n_nationkey'), ('nation__a2','n_regionkey'))]

Self-join resolver: marker probe picked
  (('nation__a1','n_regionkey'), ('nation__a2','n_nationkey'))
  (marker col = n_name)
```

### 10.4. Performance

| Stage | SJ2 (lineitem, 6M) | SJ3 (nation, 25) |
|---|---|---|
| View Minimisation | 100 s | 0.10 s |
| Cardinality Probe | < 1 s | 0.05 s |
| Filter + EquiJoin | < 1 s | 0.21 s |
| Resolver (within EquiJoin) | < 0.1 s | 0.05 s |
| Total extraction (excl. multiplicity probe) | ≈ 105 s | < 5 s |

The multiplicity probe (Phase 7) executes `Qh` and `Q_E` on the full
DB. For SJ2 on the 6M-row `lineitem`, both produce ≈ 30M result rows,
and the per-row INSERT in the legacy comparator is the dominant cost
(unrelated to this work).

---

## 11. Limitations and threats to validity

### 11.1. Pure-equality unrecoverable cases

A self-join whose only join predicate is reflexive on the *entire*
table (`SELECT n1.n_nationkey FROM nation n1, nation n2 WHERE
n1.n_nationkey = n2.n_nationkey`) is bag-equivalent to its
single-instance form on any `D` whose nationkey is unique. Such queries
*cannot* be distinguished from `SELECT n_nationkey FROM nation` by any
black-box probe. Documented as out-of-scope.

### 11.2. k ≥ 3

The current architecture targets `k_T = 2`. Generalising to k = 3
requires:
- Alias generation for triples.
- The cardinality probe's ratio is `m³` for k = 3; the band would
  shift and require a multi-shot probe (duplicate once, ratio ≈ 4;
  duplicate twice, ratio ≈ 9 = 3² for triple-instance).
- The resolver's pairwise edge enumeration grows to
  `O(|cols|² · binomial(k, 2))`.

Phase 7's multiplicity probe warns when `Qh/Q_E ≥ 2` post-extraction,
suggesting a missed alias.

### 11.3. Cross-alias inequality (SJ1, still open)

`l1.l_quantity < l2.l_quantity` is *not* a filter — Filter never emits
it, AOA never sees it, and AOA's edge-set is empty. Detecting cross-
alias inequalities requires a new probe analogous to the cross-alias
equality resolver: for each `(a_i, c, a_j, c)` pair, mutate one side
above and below the other and check Pop. Not implemented in this work.

### 11.4. Alias-aware Projection

`GenerationPipeLineBase.update_with_val` uses a whole-table UPDATE
during projection probing, which sets both alias rows to the same
value, collapsing any cross-alias inequality. For SJ1 this defeats the
projection-discovery probes. The fix needs `global_alias_row_dict`
plumbed into Generation and `update_with_val` routed through
`_exec_alias_ctid_update`.

### 11.5. Marker column availability

The marker probe (§6.4) requires a Qh-referenced column outside the
equi-join group. For an aggregate query like `SELECT COUNT(*) FROM
nation n1, nation n2 WHERE n1.regionkey = n2.nationkey`, no such
column exists; the resolver falls back to picking the first
cross-column holding edge. Since aggregate projection is alias-
symmetric, the bag-equivalence is preserved regardless.

### 11.6. `global_d_plus_value` keyed by column only

Affects gap-aware extraction combined with multi-instance tables.
For two aliases sharing a column name, the last-mutated value wins.
A non-issue today since gap-aware and self-join test queries are
disjoint, but worth noting.

---

## 12. Future work

1. **SJ1**: implement cross-alias inequality detection in AOA. Strawman:
   after EquiJoin emits cross-alias equalities, for each pair of aliases
   of the same base and each shared inequality-typed attribute, probe
   `(a_i.c, '<', a_j.c)` by mutating one alias's `c` above/below the
   other and reading Pop. Then emit `aoa_less_thans` entries with
   alias-qualified tab-attribs.

2. **Alias-aware Projection**: plumb `global_alias_row_dict` into the
   Generation pipeline via `GenPipelineContext`. Either probe one alias
   at a time (the other still satisfies the inequality), or route
   `update_with_val` through `_exec_alias_ctid_update`.

3. **k = 3 generalisation**: multi-shot cardinality probe (ratios
   4, 9, 16 ⇒ k = 2, 3, 4) + n-ary alias dispatch in resolver.

4. **Outer-join detection**: extend resolver to recognise asymmetric
   left/right outer joins via differential cardinality probes.

5. **Tighter marker probe**: pick `marker_col` to be a column whose
   datatype admits arbitrarily distinct values (text > int > date)
   and verify Qh's projection actually echoes that column on a
   prefix probe before relying on it.

---

## 13. Index of files changed

| File | Phase | Role |
|---|---|---|
| `view_minimizer.py` | 1, 3 | Halving-floor capture; alias-row dict population |
| `abstract/MinimizerBase.py` | 1 | `check_sanity_when_base_exe` tests both halves |
| `abstract/MutationPipeLineBase.py` | 3, 4 | Alias dict; `_to_base`; alias-aware `get_dmin_val` |
| `abstract/un2_where_clause.py` | 3, 4 | `_exec_alias_ctid_update`; multi-alias restore |
| `abstract/filter_holder.py` | 3 | Share alias dict by reference |
| `core/filter.py` | 3, 4 | Alias-iterating predicate extraction; ctid-scoped probes |
| `core/equi_join.py` | 5, EQ-ext | Self-equi-join detection; cross-alias resolver (§6) |
| `core/cardinality_probe.py` (new) | 0 | Per-table self-join detection (§5) |
| `core/multiplicity_probe.py` (new) | 7 | Post-extraction verification |
| `util/instance.py` (new) | 2 | `Instance` dataclass; `build_instances` |
| `util/abstract_queries.py` | 3 | ctid-scoped UPDATE templates |
| `util/QueryStringGenerator.py` | 6 | Alias FROM emission; alias-aware SELECT |
| `pipeline/ExtractionPipeLine.py` | 6, 7 | QSG wiring; multiplicity probe invocation |
| `pipeline/fragments/DisjunctionPipeLine.py` | 1, 2, 3, 4, 0, EQ-ext | Pipeline wiring; seed predicates; `qh_cols_by_table` forwarding |

---

## 14. References

1. Khan, A. *et al.* "UNMASQUE: A hidden-query extraction framework."
   *VLDB Demo*, 2021.
2. Pal, K. *et al.* "XPose: a black-box reverse engineering tool for
   SQL queries." *SIGMOD*, 2022.
3. The Postgres MVCC documentation, "Concurrency Control" chapter
   — particularly the section on system columns and `ctid` semantics.
4. TPC-H benchmark, revision 3.0.0, Transaction Processing Performance
   Council.
5. Project codebase: `/home/ryuk/Xpose_new` — pipeline orientation in
   `CLAUDE.md`; session-by-session handoffs in `SELF_JOIN_HANDOFF.md`.

---

## Appendix A. Worked execution log for SJ3

```
View_Minimizer  INFO   Intra-page halving floor for nation at size 1
NEP PipeLine    INFO   Multi-instance tables detected (min_card > 1): {}
CardinalityProbe DEBUG INSERT INTO unmasque.nation SELECT * FROM unmasque.nation
CardinalityProbe INFO  nation B_orig=1 B_dup=4 ratio=4.00
CardinalityProbe INFO  promoted nation to k=2 (aliases nation__a1, nation__a2;
                       dup_ctid=(0,2))
CardinalityProbe DEBUG ALTER TABLE unmasque.nation RENAME COLUMN n_nationkey
                       TO n_nationkey__cp; ... (one per column)
CardinalityProbe INFO  nation qh_cols=['n_name', 'n_nationkey', 'n_regionkey']
CardinalityProbe DEBUG UPDATE … SET n_regionkey = 2 WHERE ctid='(0,2)' RETURNING …
                       (mutation probe for each col)
CardinalityProbe INFO  nation join_keys=['n_regionkey', 'n_nationkey']
NEP PipeLine    INFO   CardinalityProbe: promoted tables ['nation'];
                       seeded 4 predicates
NEP PipeLine    INFO   filter_predicates after seeding:
                       [(a1, n_regionkey, '=', 4, 4),
                        (a2, n_regionkey, '=', 4, 4),
                        (a1, n_nationkey, '=', 4, 4),
                        (a2, n_nationkey, '=', 4, 4)]
Equi_Join       INFO   Self-equi-join candidate on constant 4: [...]
Equi_Join       INFO   Self-join resolver: group [...] → holding edges
                       [((a1, n_regionkey), (a2, n_nationkey)),
                        ((a1, n_nationkey), (a2, n_regionkey))]
Equi_Join       INFO   Self-join resolver: marker probe picked
                       ((a1, n_regionkey), (a2, n_nationkey)) (marker col=n_name)
QSG             DEBUG  Creating join clause for [(a1, n_regionkey), (a2, n_nationkey)]
QSG             DEBUG  ['nation__a1.n_regionkey = nation__a2.n_nationkey']
QSG             DEBUG  Select: nation__a1.n_name as n_name
QSG             DEBUG  From: nation nation__a1, nation nation__a2
QSG             DEBUG  Where: nation__a1.n_regionkey = nation__a2.n_nationkey
```

---

## Appendix B. Glossary

| Term | Definition |
|---|---|
| `D¹` | Minimal working DB that keeps `Qh` non-empty |
| `Pop` | Boolean: `Qh(D¹)` is non-empty and free of all-NULL rows |
| `ctid` | Postgres system column: (page, item) physical row locator |
| MVCC | Multi-Version Concurrency Control — Postgres's update model |
| `min_card[T]` | Smallest row count of `T` that keeps Pop true |
| `qh_cols[T]` | Set of columns of `T` that `Qh` references (from rename probe) |
| `_to_base(alias)` | Resolve a synthetic alias `nation__a1` back to its base table `nation` |
| `K_BASE` | Large integer (1,000,003) used by the resolver for chain values |
| Marker probe | Tag a non-equi-join column to break swap symmetry |
| Bag-equivalence | Multiset equality of result tuples on `D` |

— *End of report.*

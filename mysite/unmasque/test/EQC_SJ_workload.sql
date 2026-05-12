-- ============================================================================
-- EQC+SJ benchmark workload -- self-join / multi-instance hidden queries.
--
-- These are candidate Q_H inputs for exercising the multi-instance extension
-- (Algorithms 1-4, the per-(alias,attribute) probe, the alias-aware assembler;
-- see docs/multi_instance.md).  Run with [feature] multi_instance = yes.
--
-- Naming: each query has a tag.  TPC-H rewrites keep the TPC-H number; the
-- "Sx-*" tags are the synthetic stress queries (mixed multiplicity, strict vs.
-- non-strict chains, per-alias filters, intra-alias equalities, k>=3).
--
-- Status: this file lists the *intended* benchmark; nothing here has been run
-- end-to-end against a live PostgreSQL + TPC-H instance yet (the dev env had no
-- DB).  MultiInstancePipelineTest.py drives a subset of these against a live DB.
-- For each query the comment records what the multi-instance pipeline *should*
-- recover (mult, cross-alias predicates, per-alias filters, the alias-aware
-- query) so the integration test has something to assert against.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- A. Two-way self-join, equi-join + NON-STRICT inequality.
--    Exact reconstructible class (the aliases are symmetric except for the
--    ordering predicate; relabeling t1<->t2 changes nothing observable).
-- ---------------------------------------------------------------------------

-- A1 (Q2-rewrite): the "min ps_supplycost over a partkey" idea, decorrelated to
-- a self-join.  mult(partsupp)=2; inter-alias ps1.ps_supplycost <= ps2.ps_supplycost
-- on the slots; ps_partkey is a coupled column (equal across both aliases).
-- Expected alias-aware query:
--   Select partsupp_a1.ps_partkey
--   From partsupp AS partsupp_a1, partsupp AS partsupp_a2
--   Where partsupp_a1.ps_partkey = partsupp_a2.ps_partkey
--     and partsupp_a1.ps_supplycost <= partsupp_a2.ps_supplycost;
SELECT ps1.ps_partkey
FROM   partsupp ps1, partsupp ps2
WHERE  ps1.ps_partkey = ps2.ps_partkey
  AND  ps1.ps_supplycost <= ps2.ps_supplycost;

-- A2 (Q17-rewrite, simplified): "lines whose quantity is at most some other line
-- on the same part".  mult(lineitem)=2; coupled column l_partkey; inter-alias
-- l1.l_quantity <= l2.l_quantity.
SELECT l1.l_partkey, l1.l_quantity
FROM   lineitem l1, lineitem l2
WHERE  l1.l_partkey = l2.l_partkey
  AND  l1.l_quantity <= l2.l_quantity;


-- ---------------------------------------------------------------------------
-- B. Two-way self-join, equi-join + STRICT inequality.
--    The witness D_min necessarily needs >= 2 *distinct* rows -- exercises the
--    no-crash minimizer fix (MinimizerBase.check_sanity_when_base_exe) and the
--    mult(R)-floored minimization, then Algorithm 2's k-coloured halving.
-- ---------------------------------------------------------------------------

-- B1: project one column from each alias -> exercises the projection alias-lift.
-- Expected: mult(lineitem)=2, coupled column l_orderkey, inter-alias
-- l1.l_shipdate < l2.l_shipdate, output_attribution {0:(lineitem,1,l_partkey),
-- 1:(lineitem,2,l_quantity)} (so SELECT is reconstructed as
-- lineitem_a1.l_partkey, lineitem_a2.l_quantity, not collapsed onto a1).
SELECT l1.l_partkey, l2.l_quantity
FROM   lineitem l1, lineitem l2
WHERE  l1.l_orderkey = l2.l_orderkey
  AND  l1.l_shipdate < l2.l_shipdate;

-- B2: same idea on orders.  mult(orders)=2, coupled o_custkey, inter-alias
-- o1.o_orderdate < o2.o_orderdate.
SELECT o1.o_orderkey, o2.o_orderkey
FROM   orders o1, orders o2
WHERE  o1.o_custkey = o2.o_custkey
  AND  o1.o_orderdate < o2.o_orderdate;


-- ---------------------------------------------------------------------------
-- C. Per-alias filters (Algorithm 4): each alias carries its OWN bound on the
--    same column.  The legacy filter extractor only recovers the tightest; the
--    multi-instance pipeline must recover the bound *multiset* and (when an
--    inter-alias chain pins the aliases) attribute each bound to its alias.
-- ---------------------------------------------------------------------------

-- C1: distinct upper bounds, no chain -> per_alias_filters[partsupp][ps_supplycost]
-- has upper_multiset ~ [500, 800] (values approximate after bisection); aliases
-- are free w.r.t. ps_supplycost so the assignment a1<->a2 is observationally
-- irrelevant (the assembler emits tightest->a1, looser->a2).
SELECT ps1.ps_partkey
FROM   partsupp ps1, partsupp ps2
WHERE  ps1.ps_partkey = ps2.ps_partkey
  AND  ps1.ps_supplycost <= 500
  AND  ps2.ps_supplycost <= 800;

-- C2: a per-alias filter on each side of l_quantity, plus a strict chain on a
-- *different* column (l_shipdate) that is PROJECTED from both aliases -- so
-- Algorithm 3 recovers the inter-alias chain on l_shipdate, and the
-- per-(alias,attribute) probe (per_alias_pinned_filter) discriminates l_shipdate,
-- pins a1 = the earlier-shipdate row, and binary-searches each alias's l_quantity
-- bound: a1 owns l_quantity >= 10, a2 owns l_quantity <= 40.
SELECT l1.l_shipdate AS early, l2.l_shipdate AS late
FROM   lineitem l1, lineitem l2
WHERE  l1.l_orderkey = l2.l_orderkey
  AND  l1.l_shipdate < l2.l_shipdate
  AND  l1.l_quantity >= 10
  AND  l2.l_quantity <= 40;

-- C3: "both aliases <= 700" -> upper_multiset ~ [700, 700] (a x4 cardinality
-- jump at one break point) -- distinguishes "both bounded" from "only one".
SELECT ps1.ps_partkey
FROM   partsupp ps1, partsupp ps2
WHERE  ps1.ps_partkey = ps2.ps_partkey
  AND  ps1.ps_supplycost <= 700
  AND  ps2.ps_supplycost <= 700;


-- ---------------------------------------------------------------------------
-- D. Projection alias-lift: project the SAME column from BOTH aliases.  Without
--    the alias-lift the assembler would emit "ps1.ps_supplycost, ps1.ps_supplycost".
-- ---------------------------------------------------------------------------

-- D1: SELECT exposes ps1.ps_supplycost (the smaller) and ps2.ps_supplycost (the
-- larger).  Expected output_attribution {0:(partsupp,1,ps_supplycost),
-- 1:(partsupp,2,ps_supplycost)} and alias-aware SELECT
--   partsupp_a1.ps_supplycost AS lo, partsupp_a2.ps_supplycost AS hi.
SELECT ps1.ps_supplycost AS lo, ps2.ps_supplycost AS hi
FROM   partsupp ps1, partsupp ps2
WHERE  ps1.ps_partkey = ps2.ps_partkey
  AND  ps1.ps_supplycost < ps2.ps_supplycost;

-- D2: aggregate over one alias' column (single-column, non-COUNT) -> the
-- aggregate argument is alias-lifted: max(partsupp_a2.ps_supplycost).
SELECT ps1.ps_partkey, max(ps2.ps_supplycost) AS hi
FROM   partsupp ps1, partsupp ps2
WHERE  ps1.ps_partkey = ps2.ps_partkey
  AND  ps1.ps_supplycost < ps2.ps_supplycost
GROUP BY ps1.ps_partkey;


-- ---------------------------------------------------------------------------
-- E. Three-way self-join (mult = 3): exercises the k-coloured halving with k=3,
--    the topo-ordering of >=3 slots in the assembler, and the §F probe k>=3 path.
-- ---------------------------------------------------------------------------

-- E1: a strict 3-chain on l_shipdate over a shared l_orderkey.
-- Expected: mult(lineitem)=3, coupled l_orderkey, inter-alias chain
-- l1.l_shipdate < l2.l_shipdate < l3.l_shipdate -> aliases topo-ordered a1,a2,a3.
SELECT l1.l_orderkey
FROM   lineitem l1, lineitem l2, lineitem l3
WHERE  l1.l_orderkey = l2.l_orderkey
  AND  l2.l_orderkey = l3.l_orderkey
  AND  l1.l_shipdate < l2.l_shipdate
  AND  l2.l_shipdate < l3.l_shipdate;

-- E2 (Q21-flavoured, simplified): three lineitems on one order, chained on
-- l_linenumber, projecting one column from the middle alias.
SELECT l2.l_partkey, l1.l_quantity, l3.l_quantity
FROM   lineitem l1, lineitem l2, lineitem l3
WHERE  l1.l_orderkey = l2.l_orderkey
  AND  l2.l_orderkey = l3.l_orderkey
  AND  l1.l_linenumber < l2.l_linenumber
  AND  l2.l_linenumber < l3.l_linenumber;

-- E3 (k=3 + §F probe): l_shipdate chained AND projected from all 3 aliases, plus
-- a per-alias filter on l_quantity for each alias.  The per-(alias,attribute)
-- probe should discriminate l_shipdate, pin a1<a2<a3, and recover each alias'
-- l_quantity bound: a1 -> [>=5], a2 -> [>=15], a3 -> [<=45].
SELECT l1.l_shipdate AS d1, l2.l_shipdate AS d2, l3.l_shipdate AS d3
FROM   lineitem l1, lineitem l2, lineitem l3
WHERE  l1.l_orderkey = l2.l_orderkey
  AND  l2.l_orderkey = l3.l_orderkey
  AND  l1.l_shipdate < l2.l_shipdate
  AND  l2.l_shipdate < l3.l_shipdate
  AND  l1.l_quantity >= 5
  AND  l2.l_quantity >= 15
  AND  l3.l_quantity <= 45;


-- ---------------------------------------------------------------------------
-- F. Mixed multiplicity: a self-joined relation alongside a single-instance one.
--    Tests the "per-table independent treatment" (mult is detected per relation;
--    only the multi-instance one gets aliased).
-- ---------------------------------------------------------------------------

-- F1: customer joined to two orders.  mult(customer)=1, mult(orders)=2; coupled
-- o_custkey, inter-alias o1.o_orderdate < o2.o_orderdate; customer stays
-- single-instance and joins to orders_a1 (the legacy join edge, best-effort).
SELECT c1.c_name, o2.o_totalprice
FROM   customer c1, orders o1, orders o2
WHERE  c1.c_custkey = o1.o_custkey
  AND  c1.c_custkey = o2.o_custkey
  AND  o1.o_orderdate < o2.o_orderdate;

-- F2: partsupp self-joined + part single-instance + a per-alias filter.
SELECT p1.p_name
FROM   part p1, partsupp ps1, partsupp ps2
WHERE  p1.p_partkey = ps1.ps_partkey
  AND  p1.p_partkey = ps2.ps_partkey
  AND  ps1.ps_supplycost < ps2.ps_supplycost
  AND  ps1.ps_supplycost <= 600;


-- ---------------------------------------------------------------------------
-- G. Boundary cases (docs/multi_instance.md §5): the pipeline should NOT crash;
--    the exact behaviour is documented, not necessarily "ideal".
-- ---------------------------------------------------------------------------

-- G1: idempotent self-join on a unique key with the redundancy projected away.
-- ps_partkey,ps_suppkey is the PK of partsupp, so ps1=ps2 on the full key; this
-- is homomorphism-equivalent to a single scan.  Algorithm 1 reports mult=2 (the
-- query genuinely self-joins and |Q_H| does scale under bag semantics); we do
-- NOT do the homomorphism folding that would prefer "Select ps_partkey From partsupp".
SELECT ps1.ps_partkey
FROM   partsupp ps1, partsupp ps2
WHERE  ps1.ps_partkey = ps2.ps_partkey
  AND  ps1.ps_suppkey = ps2.ps_suppkey;

-- G2: GROUP BY suppression -- the self-join is hidden from |Q_H| (one group per
-- partkey regardless of how many (ps1,ps2) pairs feed it).  Algorithm 1's
-- cardinality fingerprint plateaus; the fresh-tuple fallback (slot counting)
-- should still report mult(partsupp)=2.
SELECT ps1.ps_partkey, count(*) AS pairs
FROM   partsupp ps1, partsupp ps2
WHERE  ps1.ps_partkey = ps2.ps_partkey
  AND  ps1.ps_supplycost <= ps2.ps_supplycost
GROUP BY ps1.ps_partkey;

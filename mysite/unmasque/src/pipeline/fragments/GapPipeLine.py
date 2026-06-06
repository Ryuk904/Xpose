"""Within-attribute gap (OR-of-intervals) extraction, relocated to run AFTER
Projection as a NEP-style diff pass.

Why here and not inside Filter
------------------------------
A within-attribute disjunction such as ``A in [10,20] OR A in [30,40]`` is a
*hole* in an otherwise contiguous interval. To discover the hole we diff the
reconstructed query Q_E (which carries the over-approximating envelope
``A between 10 and 40``) against the hidden Qh: any base row whose ``A`` falls
in the hole is accepted by Q_E but rejected by Qh, i.e. a witness.

The old in-Filter implementation ran *before* Projection, so Q_E did not exist
yet and it had to fabricate a comparison query from Qh's raw result header --
which broke the moment Qh projected an expression or an aggregate. NEP does not
have this problem because it diffs the *finished* Q_E, whose projection the
Projection stage already reconstructed to match Qh exactly. This pass copies
NEP's discipline: it runs right after ``formulate_query_string`` (and before
``_extract_NEP``), reuses the NEP comparator/minimizer to find a witness row,
and reads ``A`` directly from that base row -- so it works for bare-column,
scalar-expression and (count/sum/min/max) aggregate projections alike.

Algorithm (mirrors ``_extract_NEP``)
------------------------------------
Repeat up to ``GAP_CUTOFF`` times:
  1. ``GapMinimizer.match`` restores full D and diffs Q_E vs Qh. Bag-equal ->
     no (more) gaps, stop. Q_E malformed -> stop.
  2. For each core relation, ctid-bisect it down to the single witness row
     (``GapMinimizer.doJob``), then for each numeric/date attribute:
       - read the witness value ``v``;
       - build a projection-agnostic acceptance oracle on the 1-row witness
         table (Pop for row-preserving Qh; result-fingerprint delta for
         count-like Qh, auto-detected);
       - if ``v`` is a genuine hole on this attribute, binary-search outward
         for the gap edges and split the interval.
  3. Push the discovered intervals into the query generator's
     ``disjunctive_ranges`` side channel and re-render, so the next diff sees a
     tighter Q_E.

The render side is unchanged: an envelope ``range`` carrier tuple plus
``disjunctive_ranges[(tab,attr)]`` makes
``QueryStringGenerator.__generate_arithmetic_pure_conjunctions`` emit the
``(A between .. OR A between ..)`` clause. For the both-domain-extremes shape
(e.g. ``A < 5 OR A > 20``) Filter emits no carrier, so we synthesise a
full-domain one (see ``regenerate_with_disjunctions``).
"""

from ....src.core.gap_minimizer import GapMinimizer
from ....src.util.aoa_utils import get_constants_for
from ....src.util.utils import (get_cast_value, get_mid_val, get_format,
                                is_left_less_than_right_by_cutoff)


class GapPipeLine:
    """Mixin providing ``_extract_gap``. Designed to be combined into
    ExtractionPipeLine alongside DisjunctionPipeLine/NepPipeLine, which supply
    ``self.connectionHelper``, ``self.logger``, ``self.q_generator``,
    ``self.filter_extractor`` and ``self.all_sizes``."""

    GAP_CUTOFF = 10
    _BINSEARCH_GUARD = 128

    # ----------------------------------------------------------- entry point

    def _extract_gap(self, query, eq, core_relations):
        if not getattr(self.connectionHelper.config, 'detect_gap_aware', False):
            return eq
        fe = getattr(self, 'filter_extractor', None)
        if fe is None or not core_relations:
            return eq
        try:
            minimizer = GapMinimizer(self.connectionHelper, list(core_relations), self.all_sizes)
        except Exception as e:
            self.logger.debug(f"gap pass: cannot build minimizer: {e}")
            return eq

        intervals_by_key = {}   # (tab, attr) -> [(lo, hi), ...]
        carriers = {}           # (tab, attr) -> (env_lo, env_hi)  (both-ends synth)
        try:
            for _it in range(self.GAP_CUTOFF):
                eq = self.q_generator.write_query()
                try:
                    matched = minimizer.match(query, eq)
                except Exception as e:
                    self.logger.debug(f"gap pass: diff failed: {e}")
                    break
                if matched is None or matched:
                    # Q_E malformed, or Q_E and Qh already bag-equal -> done.
                    break
                progress = False
                for tab in core_relations:
                    try:
                        reduced = minimizer.doJob((query, eq, tab))
                    except Exception as e:
                        self.logger.debug(f"gap pass: minimize {tab} failed: {e}")
                        continue
                    if not reduced:
                        continue
                    try:
                        if self._carve_gaps_for_table(query, tab, intervals_by_key, carriers):
                            progress = True
                            eq = self._apply_disjunctions(intervals_by_key, carriers)
                    except Exception as e:
                        self.logger.debug(f"gap pass: carve {tab} failed: {e}")
                if not progress:
                    break
        except Exception as e:
            self.logger.debug(f"gap pass aborted: {e}")
        return self._apply_disjunctions(intervals_by_key, carriers)

    # ------------------------------------------------------- per-table carve

    def _carve_gaps_for_table(self, query, tab, intervals_by_key, carriers):
        fe = self.filter_extractor
        # 1) hole-in-interval: attributes for which Filter already emitted an
        #    enveloping 'range' carrier. Common case (DQ3/DQ7-style).
        for attr, (lo, hi) in self._range_envelopes(tab).items():
            dt = self._canon_dt(fe.get_datatype((tab, attr)))
            if dt is None:
                continue
            if self._find_one_gap(query, tab, attr, dt, lo, hi,
                                  intervals_by_key, carriers, synth=False):
                return True
        # 2) both-domain-extremes: numeric/date attributes with NO predicate
        #    (Filter dropped them). Synthesise a full-domain envelope carrier.
        for attr in self._unconstrained_numeric_attrs(tab):
            dt = self._canon_dt(fe.get_datatype((tab, attr)))
            if dt is None:
                continue
            dom = self._full_domain(tab, attr, dt)
            if dom is None:
                continue
            if self._find_one_gap(query, tab, attr, dt, dom[0], dom[1],
                                  intervals_by_key, carriers, synth=True):
                return True
        return False

    def _find_one_gap(self, query, tab, attr, dt, env_lo, env_hi,
                      intervals_by_key, carriers, synth):
        key = (tab, attr)
        try:
            env_lo, env_hi = get_cast_value(dt, env_lo), get_cast_value(dt, env_hi)
        except Exception:
            return False
        cur = intervals_by_key.get(key, [(env_lo, env_hi)])

        raw = self._read_witness_attr(tab, attr)
        if raw is None:
            return False
        try:
            v = get_cast_value(dt, raw)
        except Exception:
            return False

        # which sub-interval currently believed accepted contains v?
        idx = None
        for i, (a, b) in enumerate(cur):
            if a <= v <= b:
                idx = i
                break
        if idx is None:
            return False
        lb, ub = cur[idx]

        accepted = self._make_acceptance_oracle(query, tab, attr, dt, v)
        if accepted is None:
            return False
        # v must be a genuine hole on THIS attribute (false-witness guard), and
        # the interval endpoints must be accepted so a gap can be bracketed.
        if accepted(v):
            return False
        if not (accepted(lb) and accepted(ub)):
            return False

        try:
            unit, cutoff = get_constants_for(dt)
        except Exception:
            return False
        gap_left = self._gallop_last_sat(dt, lb, v, unit, cutoff, accepted)
        gap_right = self._gallop_first_sat(dt, v, ub, unit, cutoff, accepted)
        if gap_left is None or gap_right is None or not (gap_left < gap_right):
            return False

        intervals_by_key[key] = cur[:idx] + [(lb, gap_left), (gap_right, ub)] + cur[idx + 1:]
        if synth:
            carriers[key] = (env_lo, env_hi)
        return True

    # -------------------------------------------------- acceptance oracle

    def _make_acceptance_oracle(self, query, tab, attr, dt, v):
        """Return a callable accepted(e): does setting this attribute to ``e``
        make the witness row contribute to Qh's result?

        Projection-agnostic by construction. We compare a **result fingerprint**
        of Qh -- the number of output rows plus a row hash, computed by wrapping
        Qh in `SELECT count(*), sum(hashtext(row)) FROM (Qh) t` and reading the
        raw scalars -- against the fingerprint at the rejected witness ``v``. Any
        movement means the row now contributes.

        Why not a plain Pop (non-empty) check: the working executable treats a
        single result row valued ``0`` as "empty" (it is tuned for pure count
        queries), so Pop cannot tell "0 rows" from "1 row valued 0". That fools
        Pop for a `count(*)` aggregate *and* for a bare scalar expression whose
        value happens to be 0 at the probe point (e.g. `2*n_nationkey` at
        `n_nationkey=0`). The wrapped `count(*)` reads the true output-row count,
        so the fingerprint distinguishes all of these.
        """
        try:
            reject_sig = self._signature_at(query, tab, attr, dt, v)
        except Exception:
            return None

        def accepted(e):
            return self._signature_at(query, tab, attr, dt, e) != reject_sig
        return accepted

    def _signature_at(self, query, tab, attr, dt, e):
        # Set the (1-row) witness attribute to e, then fingerprint Qh's result.
        # No revert needed: every probe overwrites attr before measuring, and the
        # working table is rebuilt by the next iteration's full-D restore.
        self._raw_update(tab, attr, dt, e)
        qh = str(query).rstrip().rstrip(';').strip()
        sql = (f"SELECT count(*) AS gap_c, "
               f"COALESCE(SUM(hashtext(t::text)), 0) AS gap_h FROM ({qh}) AS t")
        res = self.filter_extractor.app.doJob(sql)
        if not res or len(res) < 2 or not res[1]:
            return ('0', '0')
        row = res[1]
        return (str(row[0]), str(row[1]))

    def _raw_update(self, tab, attr, dt, val):
        fqn = self.filter_extractor.get_fully_qualified_table_name(tab)
        q = self.connectionHelper.queries
        if dt == 'date':
            self.connectionHelper.execute_sql(
                [q.update_sql_query_tab_date_attrib_value(fqn, attr, get_format(dt, val))])
        else:
            self.connectionHelper.execute_sql(
                [q.update_tab_attrib_with_value(fqn, attr, get_format(dt, val))])

    def _read_witness_attr(self, tab, attr):
        fqn = self.filter_extractor.get_fully_qualified_table_name(tab)
        try:
            return self.connectionHelper.execute_sql_fetchone_0(
                f"SELECT {attr} FROM {fqn} LIMIT 1")
        except Exception:
            return None

    # ----------------------------------------------------- edge bisection

    def _binsearch_last_sat(self, dt, lo_acc, hi_rej, cutoff, accepted):
        # largest accepted value in [lo_acc, hi_rej]; precond accepted(lo_acc).
        lo, hi, guard = lo_acc, hi_rej, 0
        while is_left_less_than_right_by_cutoff(dt, lo, hi, cutoff) and guard < self._BINSEARCH_GUARD:
            guard += 1
            mid = get_mid_val(dt, hi, lo)
            if mid == lo or mid == hi:
                break
            if accepted(mid):
                lo = mid
            else:
                hi = mid
        return lo

    def _binsearch_first_sat(self, dt, lo_rej, hi_acc, cutoff, accepted):
        # smallest accepted value in [lo_rej, hi_acc]; precond accepted(hi_acc).
        lo, hi, guard = lo_rej, hi_acc, 0
        while is_left_less_than_right_by_cutoff(dt, lo, hi, cutoff) and guard < self._BINSEARCH_GUARD:
            guard += 1
            mid = get_mid_val(dt, hi, lo)
            if mid == lo or mid == hi:
                break
            if accepted(mid):
                hi = mid
            else:
                lo = mid
        return hi

    # --------------------------------------------- galloping edge search
    # A plain binary search assumes a SINGLE accept->reject transition between
    # the witness and the interval edge. With >=3 disjoint intervals that does
    # not hold: a midpoint can land in a far interval and the search overshoots,
    # carving one giant gap that swallows the intervals in between (verified:
    # `< 1500 OR [3000,4000] OR [6000,7000] OR > 8500` collapsed to just the two
    # extremes). Galloping outward from the witness in doubling steps locates the
    # *nearest* accepted value first, then binary-searches only inside the last
    # bracket (which has a single transition), so each carve removes exactly one
    # true gap. A swallowed interval narrower than the local stride is the one
    # residue galloping cannot guarantee; the falsify-and-rerun loop is the
    # completeness backstop for that case (see thesis 12 / future work).

    def _gallop_first_sat(self, dt, v, hi_acc, unit, cutoff, accepted):
        # smallest accepted value > v; precond accepted(hi_acc).
        if dt not in ('int', 'numeric'):
            return self._binsearch_first_sat(dt, v, hi_acc, cutoff, accepted)
        prev, step, guard = v, unit, 0
        while guard < self._BINSEARCH_GUARD:
            guard += 1
            try:
                cand = get_cast_value(dt, v + step)
            except Exception:
                break
            if not is_left_less_than_right_by_cutoff(dt, cand, hi_acc, cutoff):
                return self._binsearch_first_sat(dt, prev, hi_acc, cutoff, accepted)
            if accepted(cand):
                return self._binsearch_first_sat(dt, prev, cand, cutoff, accepted)
            prev, step = cand, step * 2
        return self._binsearch_first_sat(dt, prev, hi_acc, cutoff, accepted)

    def _gallop_last_sat(self, dt, lo_acc, v, unit, cutoff, accepted):
        # largest accepted value < v; precond accepted(lo_acc).
        if dt not in ('int', 'numeric'):
            return self._binsearch_last_sat(dt, lo_acc, v, cutoff, accepted)
        prev, step, guard = v, unit, 0
        while guard < self._BINSEARCH_GUARD:
            guard += 1
            try:
                cand = get_cast_value(dt, v - step)
            except Exception:
                break
            if not is_left_less_than_right_by_cutoff(dt, lo_acc, cand, cutoff):
                return self._binsearch_last_sat(dt, lo_acc, prev, cutoff, accepted)
            if accepted(cand):
                return self._binsearch_last_sat(dt, cand, prev, cutoff, accepted)
            prev, step = cand, step * 2
        return self._binsearch_last_sat(dt, lo_acc, prev, cutoff, accepted)

    # --------------------------------------------------- candidate discovery

    def _range_envelopes(self, tab):
        """{attr: (lo, hi)} for each contiguous 'range' carrier Filter emitted
        for ``tab`` -- the hole-in-interval candidates."""
        envs = {}
        for pred in list(self.q_generator.filter_predicates):
            if not (isinstance(pred, (tuple, list)) and len(pred) >= 5):
                continue
            if pred[0] != tab or str(pred[2]).strip().lower() != 'range':
                continue
            if pred[3] == pred[4]:
                continue   # point predicate, no interval to hole out
            envs[pred[1]] = (pred[3], pred[4])
        return envs

    def _unconstrained_numeric_attrs(self, tab):
        fe = self.filter_extractor
        try:
            all_attrs = list(fe.global_all_attribs.get(tab, []))
        except Exception:
            return []
        constrained = set()
        wc = self.q_generator._workingCopy
        for lst in (wc.arithmetic_filters, wc.filter_in_predicates,
                    wc.filter_not_in_predicates):
            for pred in lst:
                if isinstance(pred, (tuple, list)) and len(pred) >= 2 and pred[0] == tab:
                    constrained.add(pred[1])
        for edge in (getattr(fe, 'global_join_graph', None) or []):
            for itm in edge:
                if isinstance(itm, str):
                    constrained.add(itm)
                elif isinstance(itm, (tuple, list)) and len(itm) >= 2:
                    constrained.add(itm[1])
        return [a for a in all_attrs if a not in constrained]

    def _full_domain(self, tab, attr, dt):
        us = self.connectionHelper.config.user_schema
        try:
            res, _ = self.connectionHelper.execute_sql_fetchall(
                f"SELECT min({attr}), max({attr}) FROM {us}.{tab} WHERE {attr} IS NOT NULL")
        except Exception:
            return None
        if not res or not res[0] or res[0][0] is None or res[0][1] is None:
            return None
        try:
            lo, hi = get_cast_value(dt, res[0][0]), get_cast_value(dt, res[0][1])
        except Exception:
            return None
        return (lo, hi) if lo < hi else None

    # -------------------------------------------------------- render bridge

    def _apply_disjunctions(self, intervals_by_key, carriers):
        disj = {}
        for key, ivs in intervals_by_key.items():
            merged = self._coalesce(ivs)
            if len(merged) > 1:
                disj[key] = merged
        if not disj:
            return self.q_generator.write_query()
        carrier_list = [(k[0], k[1], lo, hi)
                        for k, (lo, hi) in carriers.items() if k in disj]
        return self.q_generator.regenerate_with_disjunctions(disj, carrier_list)

    @staticmethod
    def _coalesce(intervals):
        # Merge only genuinely overlapping intervals; real gaps (carved around a
        # rejected witness) are kept separate. >1 surviving interval => a true
        # disjunction worth rendering.
        s = sorted(intervals, key=lambda iv: (iv[0], iv[1]))
        merged = [s[0]]
        for lo, hi in s[1:]:
            plo, phi = merged[-1]
            if lo <= phi:
                merged[-1] = (plo, hi if hi > phi else phi)
            else:
                merged.append((lo, hi))
        return merged

    @staticmethod
    def _canon_dt(dt):
        d = str(dt).strip().lower()
        if d in ('int', 'integer', 'number', 'smallint', 'bigint'):
            return 'int'
        if d in ('numeric', 'float', 'decimal', 'real', 'double precision', 'money'):
            return 'numeric'
        if d == 'date':
            return 'date'
        return None

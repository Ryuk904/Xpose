"""
Alias-aware query assembler.

The legacy pipeline produces a *single-instance* extracted query: every table
appears once in the ``FROM`` clause, even when the hidden query self-joins it.
Algorithms 1-4 (see :mod:`multiplicity`, :mod:`alias_aware_minimizer`,
:mod:`cross_alias_predicate`, :mod:`per_alias_filter`) recover the per-relation
multiplicity, the cross-alias predicates and the per-alias filter bounds.  This
module stitches those together with the legacy query string to produce a
**candidate multi-instance query**:

* the ``FROM`` clause becomes ``R AS R_a1, R AS R_a2, ..., R AS R_ak`` for every
  relation ``R`` with ``mult(R) = k >= 2``;
* every reference to ``R``'s columns in the legacy query (qualified ``R.col`` or
  bare ``col``) is rebound to the *primary* alias ``R_a1``;
* the recovered alias-aware predicates are appended to the ``WHERE`` clause:
  - intra-alias self-equi-joins ``R_ai.c = R_ai.c'`` (applied to every alias);
  - same-column inter-alias predicates ``R_ap.c REL R_aq.c`` (Algorithm 3);
  - "coupled" columns (Algorithm 3) -- columns whose ``k`` values could not be
    discriminated -- chained ``R_a1.c = R_a2.c = ... = R_ak.c`` (this covers
    same-column cross-alias equi-joins and FKs the aliases share);
  - the looser per-alias filter bounds (Algorithm 4) on ``R_a2 .. R_ak``.

This is a *best-effort* reconstruction: the legacy SPJGAOL extractors are not yet
alias-aware, so the parts of the query they produced (joins to other tables,
projection, group-by, ...) are pinned to the primary alias and the other aliases
are connected only by what Algorithms 3-4 found.  For the common patterns
(2-way self-join with an equi-join plus an inequality, e.g. TPC-H Q2/Q17
rewrites) that is usually exact; in general it may be over- or under-constrained,
which is reported in :attr:`notes`.  The legacy single-instance query stays the
pipeline's primary result -- this candidate is published alongside it on
``self.alias_aware_query`` and is purely additive, gated behind
``[feature] multi_instance``.
"""
import re
from datetime import date

from .abstract.AppExtractorBase import AppExtractorBase


# clause keywords, in order, as emitted by QueryDetails.assembleQuery()
_CLAUSE_KW = ["select", "from", "where", "group by", "order by", "limit"]


def _word_re(kw):
    return r"(?<![\w])" + kw.replace(" ", r"\s+") + r"(?![\w])"


def _split_clauses(query):
    """Split a flat ``SELECT ... FROM ... WHERE ... GROUP BY ... ORDER BY ...
    LIMIT ...;`` string into an ordered dict of clause -> text.  Returns ``None``
    if the shape is not flat (e.g. it contains a UNION or a sub-query)."""
    if not query:
        return None
    q = query.strip().rstrip(";").strip()
    low = q.lower()
    if "union" in low or len(re.findall(_word_re("select"), low)) != 1:
        return None
    spans = []
    for kw in _CLAUSE_KW:
        m = re.search(_word_re(kw), q, re.IGNORECASE)
        if m:
            spans.append((m.start(), m.end(), kw))
    if not spans or min(spans)[2] != "select":
        return None
    spans.sort()
    out = {}
    for i, (s, e, kw) in enumerate(spans):
        end = spans[i + 1][0] if i + 1 < len(spans) else len(q)
        out[kw] = q[e:end].strip()
    return out


def _outside_quotes_sub(text, pattern, repl):
    """``re.sub`` applied only to the parts of ``text`` outside single-quoted
    string literals (``''`` treated as an escaped quote)."""
    parts = re.split(r"('(?:[^']|'')*')", text)
    for i in range(0, len(parts), 2):           # even indices are outside quotes
        parts[i] = re.sub(pattern, repl, parts[i])
    return "".join(parts)


def _qualify_text(text, table, alias, columns):
    """In ``text``, rebind references to ``table``'s columns to ``alias``:
    ``table.col`` -> ``alias.col`` and a bare whole-word ``col`` -> ``alias.col``
    (string literals left alone)."""
    if not text:
        return text
    text = _outside_quotes_sub(text, r"(?<![\w.])" + re.escape(table) + r"\.", alias + ".")
    for col in sorted([c for c in columns if c], key=len, reverse=True):
        text = _outside_quotes_sub(text, r"(?<![\w.])" + re.escape(col) + r"(?![\w])",
                                   f"{alias}.{col}")
    return text


def _qualify_select(select_text, table, alias, columns):
    """Like :func:`_qualify_text` but careful with ``<expr> AS <name>`` items:
    the alias name after ``AS`` is left untouched."""
    if not select_text:
        return select_text
    items = [s for s in re.split(r",", select_text)]
    out = []
    for it in items:
        m = re.match(r"^(.*?)(\s+as\s+)(\S+\s*)$", it, re.IGNORECASE | re.DOTALL)
        if m:
            out.append(_qualify_text(m.group(1), table, alias, columns) + m.group(2) + m.group(3))
        else:
            out.append(_qualify_text(it, table, alias, columns))
    return ",".join(out)


_AMBIGUOUS = object()           # sentinel: a (table, col) is projected from >1 alias

# aggregate functions whose output value is *not* the source column's value (so the
# discriminator-window attribution must not be applied to their argument)
_VALUE_FREE_AGGS = {"count"}


def _is_plain_colref(expr):
    """Return the match if ``expr`` is a single column reference (``col`` or
    ``something.col``), else ``None``."""
    return re.fullmatch(r"\s*(?:[\w]+\.)?([\w]+)\s*", expr or "")


def _is_single_col_agg(expr):
    """Return ``(aggfunc, col)`` if ``expr`` is ``aggfunc(col)`` / ``aggfunc(t.col)``
    over a single column, else ``None``."""
    m = re.fullmatch(r"\s*(\w+)\s*\(\s*(?:[\w]+\.)?([\w]+)\s*\)\s*", expr or "")
    return (m.group(1), m.group(2)) if m else None


def _rewrite_select(select_text, mi, columns_by_table, attribution, alias_name):
    """Rewrite the legacy ``SELECT`` list to alias-qualified form.

    For item ``i`` (= output column ``i``): if ``attribution`` says column ``i``
    exposes ``(R, j, c)`` with ``R`` multi-instance, then a plain ``c`` becomes
    ``R_a<j>.c`` and a single-column aggregate ``f(c)`` (f not COUNT) becomes
    ``f(R_a<j>.c)``; otherwise multi-instance column refs in the item are pinned to
    ``R_a1``.  Returns ``(new_select_text, col_attr)`` where ``col_attr`` maps a
    *plainly-projected* ``(R, c)`` to the alias it was projected under, or
    :data:`_AMBIGUOUS`."""
    items = re.split(r",", select_text)
    out_items, col_attr = [], {}

    def fallback(expr):
        e = expr
        for tab in mi:
            e = _qualify_text(e, tab, alias_name(tab, 1), columns_by_table.get(tab) or [])
        return e

    for idx, it in enumerate(items):
        m = re.match(r"^(.*?)(\s+as\s+\S+\s*)$", it, re.IGNORECASE | re.DOTALL)
        expr, as_part = (m.group(1), m.group(2)) if m else (it, "")
        attr = (attribution or {}).get(idx)
        R = attr[0] if attr else None
        cm = _is_plain_colref(expr)
        agg = _is_single_col_agg(expr)
        if attr and R in mi and cm and cm.group(1) == attr[2]:
            j, c = attr[1], attr[2]
            al = alias_name(R, j)
            out_items.append(f"{al}.{c}{as_part}")
            key = (R, c)
            if key in col_attr and col_attr[key] != al:
                col_attr[key] = _AMBIGUOUS
            elif key not in col_attr:
                col_attr[key] = al
        elif (attr and R in mi and agg and agg[1] == attr[2]
              and agg[0].lower() not in _VALUE_FREE_AGGS):
            j, c = attr[1], attr[2]
            out_items.append(f"{agg[0]}({alias_name(R, j)}.{c}){as_part}")
        else:
            out_items.append(fallback(expr) + as_part)
    return ", ".join(s.strip() for s in out_items), col_attr


def _qualify_clause(text, mi, columns_by_table, col_attr, alias_name):
    """Qualify column refs in a ``GROUP BY`` / ``ORDER BY`` list: a leading column
    ref is bound to the alias it was projected under (from ``col_attr``) when that is
    unambiguous, else to ``R_a1``; the rest of each item (``ASC``/``DESC``/...) is
    left alone."""
    out = []
    for it in re.split(r",", text):
        toks = it.split()
        replaced = False
        if toks:
            cm = re.fullmatch(r"(?:([\w]+)\.)?([\w]+)", toks[0])
            if cm:
                qual, col = cm.group(1), cm.group(2)
                cands = [qual] if qual in mi else [t for t in mi
                                                   if col in (columns_by_table.get(t) or [])]
                if len(cands) == 1:
                    t = cands[0]
                    a = col_attr.get((t, col))
                    target = a if (a is not None and a is not _AMBIGUOUS) else alias_name(t, 1)
                    toks[0] = f"{target}.{col}"
                    out.append(" ".join(toks))
                    replaced = True
        if not replaced:
            new = it
            for t in mi:
                new = _qualify_text(new, t, alias_name(t, 1), columns_by_table.get(t) or [])
            out.append(new)
    return ", ".join(s.strip() for s in out)


def _topo_order_slots(inter_preds):
    """Order the output slots referenced by ``{'kind':'inter','op':'<'|'>'|'=', 'slots':(p,q)}``
    predicates into one ascending chain (best effort).  ``=`` merges slots,
    ``<``/``>`` impose order.  Returns a list of slot ids, ascending."""
    nodes = set()
    less, eq = [], []
    for p in inter_preds:
        if p.get('kind') != 'inter':
            continue
        a, b = p['slots']
        nodes.update((a, b))
        op = p['op']
        if op == '=':
            eq.append((a, b))
        elif op == '<':
            less.append((a, b))
        elif op == '>':
            less.append((b, a))
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in eq:
        parent[find(a)] = find(b)
    reps = sorted({find(n) for n in nodes})
    succ = {r: set() for r in reps}
    indeg = {r: 0 for r in reps}
    for a, b in less:
        ra, rb = find(a), find(b)
        if ra != rb and rb not in succ[ra]:
            succ[ra].add(rb)
            indeg[rb] += 1
    order = sorted(r for r in reps if indeg[r] == 0)
    out, i = [], 0
    while i < len(order):
        r = order[i]
        i += 1
        if r in out:
            continue
        out.append(r)
        for s in sorted(succ[r]):
            indeg[s] -= 1
            if indeg[s] == 0:
                order.append(s)
    for r in reps:
        if r not in out:
            out.append(r)
    rep_member = {}
    for n in sorted(nodes):
        rep_member.setdefault(find(n), n)
    return [rep_member[r] for r in out]


def _sql_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, date):
        return f"'{v}'"
    s = str(v)
    try:
        float(s)
        return s
    except ValueError:
        return "'" + s.replace("'", "''") + "'"


def _strip_col_atoms(where_text, qcol):
    """Remove the (rebound) legacy filter atoms on ``qcol`` (e.g. ``lineitem_a1.l_quantity``)
    from a WHERE string: ``qcol <= v`` / ``>=`` / ``=`` / ``< v`` / ``> v`` / ``between a and b``,
    cleaning up the surrounding ``and``s."""
    if not where_text:
        return where_text
    qe = re.escape(qcol)
    t = re.sub(qe + r"\s+between\s+[^\s]+\s+and\s+[^\s]+", "\0", where_text, flags=re.IGNORECASE)
    t = re.sub(qe + r"\s*(?:<=|>=|<>|!=|=|<|>)\s*'[^']*'", "\0", t, flags=re.IGNORECASE)
    t = re.sub(qe + r"\s*(?:<=|>=|<>|!=|=|<|>)\s*[^\s,)]+", "\0", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+and\s+\0", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\0\s+and\s+", "", t, flags=re.IGNORECASE)
    t = t.replace("\0", "")
    return t.strip()


def build_alias_aware_query(legacy_query, mult, cross_alias_predicates, per_alias_filters,
                            columns_by_table, coupled_columns=None, projection_attribution=None,
                            pinned_filters=None, reverse_tails=False,
                            alias_name=lambda t, i: f"{t}_a{i}"):
    """Pure assembler: returns ``(candidate_sql, notes)`` or ``(None, notes)``.

    ``pinned_filters`` (Algorithm-F probe): ``{tab -> {alias_index -> {col -> {'upper': u,
    'lower': l}}}}`` -- when present for a ``(tab, col)``, the legacy filter atoms on that
    column are stripped and the per-alias bounds are emitted from the probe instead (with the
    correct alias identity).  ``reverse_tails``: for columns without a probe but with >1
    distinct bound, emit the per-alias bound tail (the part beyond ``a1``) in reverse order
    (a variant for the verifier-guided search)."""
    notes = []
    mi = {t: int(k) for t, k in (mult or {}).items() if int(k) > 1}
    if not mi:
        return None, ["no multi-instance relation -- nothing to assemble"]
    clauses = _split_clauses(legacy_query)
    if clauses is None:
        return None, ["legacy query shape not recognised (UNION / sub-query / empty) -- skipped"]

    cap = cross_alias_predicates or {}
    paf = per_alias_filters or {}
    cpl = coupled_columns or {}
    pa = projection_attribution or {}
    pinned = pinned_filters or {}

    # --- FROM clause -------------------------------------------------------
    from_items = [s.strip() for s in re.split(r",", clauses.get("from", "")) if s.strip()]
    new_from, seen_tables = [], set()
    for it in from_items:
        name = re.split(r"\s+", it.strip())[0]
        seen_tables.add(name)
        if name in mi and it.strip() == name:
            k = mi[name]
            new_from.append(", ".join(f"{name} AS {alias_name(name, i)}" for i in range(1, k + 1)))
        else:
            new_from.append(it)
            if name in mi:
                notes.append(f"{name} already aliased in the legacy FROM; left as-is")
    clauses["from"] = ", ".join(new_from)

    # --- SELECT (alias-lifted via the projection attribution), then GROUP/ORDER BY ---
    col_attr = {}
    if clauses.get("select"):
        clauses["select"], col_attr = _rewrite_select(clauses["select"], mi, columns_by_table,
                                                      pa, alias_name)
        for (R, c), a in col_attr.items():
            if a is _AMBIGUOUS:
                notes.append(f"{R}.{c} projected from >1 alias -- GROUP/ORDER BY refs to it left on a1")
    for kw in ("group by", "order by"):
        if clauses.get(kw):
            clauses[kw] = _qualify_clause(clauses[kw], mi, columns_by_table, col_attr, alias_name)

    extra_where = []
    for tab, k in mi.items():
        if tab not in seen_tables:
            notes.append(f"{tab} flagged mult={k} but not in legacy FROM; skipped")
            continue
        a1 = alias_name(tab, 1)
        cols = columns_by_table.get(tab) or []
        if not cols:
            notes.append(f"no column list for {tab}; bare column refs not rebound")
        if clauses.get("where"):
            clauses["where"] = _qualify_text(clauses["where"], tab, a1, cols)

        # intra-alias self-equi-joins (applied to every alias)
        for p in cap.get(tab, []):
            if p.get('kind') == 'intra_eq':
                c, c2 = p['cols']
                for i in range(1, k + 1):
                    extra_where.append(f"{alias_name(tab, i)}.{c} = {alias_name(tab, i)}.{c2}")
                notes.append(f"intra-alias {tab}.{c}={tab}.{c2} applied to all {k} aliases "
                             f"(may over-constrain)")

        # same-column inter-alias predicates (Algorithm 3)
        by_col = {}
        for p in cap.get(tab, []):
            if p.get('kind') == 'inter':
                by_col.setdefault(p['col'], []).append(p)
        for col, preds in by_col.items():
            chain = _topo_order_slots(preds)
            slot_to_alias = {s: alias_name(tab, idx + 1) for idx, s in enumerate(chain)}
            for p in preds:
                sp, sq = p['slots']
                ap, aq = slot_to_alias.get(sp), slot_to_alias.get(sq)
                if ap and aq and ap != aq:
                    extra_where.append(f"{ap}.{col} {p['op']} {aq}.{col}")

        # coupled columns -> equi-join chain across all aliases
        for col in cpl.get(tab, []):
            for i in range(2, k + 1):
                extra_where.append(f"{a1}.{col} = {alias_name(tab, i)}.{col}")
            if k >= 2 and cpl.get(tab):
                notes.append(f"coupled column {tab}.{col}: chained equal across {k} aliases "
                             f"(could be a cross-alias equi-join, an FK shared by the aliases, "
                             f"or a tight filter)")

        # per-alias filter bounds.  If the Algorithm-F probe attributed them to specific
        # aliases for this (tab, col), strip the rebound legacy atoms on `col` and emit the
        # probed bounds; otherwise fall back to Algorithm 4's multiset (tightest -> a1 via
        # the legacy WHERE, the rest -> a2, a3, ... in order, or reversed for the variant).
        pinned_tab = pinned.get(tab) or {}
        pinned_cols = {c for cols_of in pinned_tab.values() for c in cols_of}
        for col, info in (paf.get(tab) or {}).items():
            if col in pinned_cols:
                if clauses.get("where"):
                    clauses["where"] = _strip_col_atoms(clauses["where"], f"{a1}.{col}")
                for ai in range(1, k + 1):
                    b = (pinned_tab.get(ai) or {}).get(col) or {}
                    if b.get('lower') is not None:
                        extra_where.append(f"{alias_name(tab, ai)}.{col} >= {_sql_val(b['lower'])}")
                    if b.get('upper') is not None:
                        extra_where.append(f"{alias_name(tab, ai)}.{col} <= {_sql_val(b['upper'])}")
                notes.append(f"{tab}.{col}: per-alias bounds attributed by the discriminator probe")
                continue
            up_ms = list(info.get('upper_multiset') or sorted(info.get('upper') or [], key=str))
            lo_ms = list(info.get('lower_multiset')
                         or sorted(info.get('lower') or [], key=str, reverse=True))
            up_tail = list(reversed(up_ms[1:])) if reverse_tails else up_ms[1:]
            lo_tail = list(reversed(lo_ms[1:])) if reverse_tails else lo_ms[1:]
            for i, ub in enumerate(up_tail, start=2):
                if i <= k:
                    extra_where.append(f"{alias_name(tab, i)}.{col} <= {_sql_val(ub)}")
            for i, lb in enumerate(lo_tail, start=2):
                if i <= k:
                    extra_where.append(f"{alias_name(tab, i)}.{col} >= {_sql_val(lb)}")
            if up_ms or lo_ms:
                notes.append(f"{tab}.{col}: per-alias bounds {{lower:{lo_ms}, upper:{up_ms}}} "
                             f"(tightest -> a1, rest -> a2..{' reversed' if reverse_tails else ''}; "
                             f"assignment by tightness, not recovered alias identity)")
            if len(up_ms) > k or len(lo_ms) > k:
                notes.append(f"{tab}.{col}: per-alias bound multiset larger than k -- some dropped")

    # --- reassemble --------------------------------------------------------
    where = clauses.get("where", "").strip()
    if extra_where:
        where = (where + " and " if where else "") + " and ".join(dict.fromkeys(extra_where))
    out = "Select " + (clauses.get("select") or "*")
    out += "\n From " + clauses.get("from", "")
    if where:
        out += "\n Where " + where
    if clauses.get("group by"):
        out += "\n Group By " + clauses["group by"]
    if clauses.get("order by"):
        out += "\n Order By " + clauses["order by"]
    if clauses.get("limit"):
        out += "\n Limit " + clauses["limit"]
    out += ";"
    notes.append("syntactic reconstruction -- the legacy single-instance query stays the "
                 "primary result; AliasAwareAssembler verifies this candidate against the database")
    return out, notes


def _result_multiset(result):
    """Sorted multiset of an executable's result (data rows, values stringified)."""
    if not isinstance(result, list) or len(result) < 1:
        return None
    return sorted(tuple(str(v) for v in row) for row in result[1:])


class AliasAwareAssembler(AppExtractorBase):
    """Pipeline wrapper: assembles the candidate multi-instance query and verifies it.

    :meth:`doJob` is called as ``doJob(q_h, legacy_query)``.

    Public outputs after :meth:`doJob`:

    * ``alias_aware_query`` -- the candidate SQL string, or ``None``.
    * ``verified`` -- ``True`` if the candidate's result equals ``Q_H``'s on the original
      database, ``False`` if it differs / the candidate fails to execute, ``None`` if it
      could not be checked (no comparable result, too large, error running it).
    * ``notes`` -- confidence remarks (list of strings).
    """

    _VERIFY_ROW_CAP = 200_000

    def __init__(self, connectionHelper, core_relations, mult,
                 cross_alias_predicates, per_alias_filters, coupled_columns=None,
                 projection_attribution=None, pinned_filters=None):
        super().__init__(connectionHelper, "AliasAwareAssembler")
        self.core_relations = list(dict.fromkeys(core_relations))
        self.mult = dict(mult or {})
        self.cross_alias_predicates = dict(cross_alias_predicates or {})
        self.per_alias_filters = dict(per_alias_filters or {})
        self.coupled_columns = dict(coupled_columns or {})
        self.projection_attribution = dict(projection_attribution or {})
        self.pinned_filters = dict(pinned_filters or {})
        self.alias_aware_query = None
        self.verified = None
        self.notes = []

    def extract_params_from_args(self, args):
        # doJob(q_h, legacy_query)  -- accept (legacy_query,) too, then no verification.
        if len(args) >= 2:
            return args[0], args[1]
        return None, args[0]

    def _candidate_variants(self, legacy_query, cols_by_tab):
        """Up to a handful of candidate (sql, notes, label) triples to try, in order:
        the discriminator-probe-attributed one (if a probe ran), then the plain default,
        then the bound-tail-reversed variant."""
        common = dict(coupled_columns=self.coupled_columns,
                      projection_attribution=self.projection_attribution)
        seen, out = set(), []

        def add(label, **kw):
            sql, notes = build_alias_aware_query(legacy_query, self.mult, self.cross_alias_predicates,
                                                 self.per_alias_filters, cols_by_tab, **common, **kw)
            if sql and sql not in seen:
                seen.add(sql)
                out.append((sql, notes, label))

        if self.pinned_filters:
            add("probe-attributed", pinned_filters=self.pinned_filters)
        add("default")
        add("bound-tail-reversed", reverse_tails=True)
        return out

    def doActualJob(self, args=None):
        q_h, legacy_query = self.extract_params_from_args(args)
        cols_by_tab = {t: self._column_names(t) for t, k in self.mult.items() if int(k) > 1}
        variants = self._candidate_variants(legacy_query, cols_by_tab)
        if not variants:
            return None
        # default to the first variant; if Q_H is available, prefer whichever verifies.
        chosen_sql, chosen_notes, chosen_label = variants[0]
        chosen_verdict = None
        if q_h:
            for sql, notes, label in variants:
                verdict, note = self._run_and_compare(sql, q_h)
                if verdict is True:
                    chosen_sql, chosen_notes, chosen_label, chosen_verdict = sql, notes, label, True
                    self.logger.debug(f"alias-aware candidate '{label}' verified")
                    break
                if chosen_verdict is None and verdict is not None:
                    # remember the first definitive (False) verdict in case nothing verifies
                    chosen_verdict = verdict
            else:
                # nothing verified; keep the first variant, record why
                chosen_sql, chosen_notes, chosen_label = variants[0]
        else:
            chosen_notes = chosen_notes + ["not verified against the database (Q_H unavailable here)"]
        self.alias_aware_query = chosen_sql
        self.verified = chosen_verdict if q_h else None
        extra = []
        if q_h:
            if self.verified is True:
                extra.append(f"candidate VERIFIED ('{chosen_label}' variant reproduces Q_H on the database)")
            elif len(variants) > 1:
                extra.append(f"none of {len(variants)} candidate variants reproduced Q_H -- "
                             f"keeping the '{chosen_label}' variant as best-effort")
            else:
                extra.append("candidate does NOT match Q_H on the database -- best-effort only")
        self.notes = chosen_notes + extra
        return self.alias_aware_query

    def _run_and_compare(self, candidate, q_h):
        """Run ``candidate`` and ``q_h`` against the original schema; return
        ``(verdict, note)`` where verdict is ``True`` / ``False`` / ``None``."""
        try:
            self.app.reset_data_schema()        # run against the original (user) schema
            r_qh = self.app.doJob(q_h)
            r_cand = self.app.doJob(candidate)
        except Exception as e:
            self.logger.debug(f"verification run failed: {e}")
            return None, "could not run the candidate / Q_H for verification"
        if not isinstance(r_cand, list):
            return False, "candidate query failed to execute"
        ms_qh, ms_cand = _result_multiset(r_qh), _result_multiset(r_cand)
        if ms_qh is None:
            return None, "Q_H produced no comparable result"
        if len(ms_qh) > self._VERIFY_ROW_CAP or len(ms_cand) > self._VERIFY_ROW_CAP:
            return None, "result set too large to verify"
        return (ms_qh == ms_cand), ("matches Q_H" if ms_qh == ms_cand else "does not match Q_H")

    def _column_names(self, tab):
        for schema in (getattr(self.connectionHelper.config, "user_schema", None),
                       getattr(self.connectionHelper.config, "schema", None)):
            if not schema:
                continue
            try:
                rows, _ = self.connectionHelper.execute_sql_fetchall(
                    self.connectionHelper.queries.get_column_details_for_table(schema, tab))
                if rows:
                    return [r[0] for r in rows]
            except Exception as e:
                self.logger.debug(f"column lookup for {tab} in {schema}: {e}")
        return []

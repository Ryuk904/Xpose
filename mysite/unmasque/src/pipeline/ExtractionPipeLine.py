import copy

from ..core.dataclass.genPipeline_context import GenPipelineContext
from ..core.dataclass.pgao_context import PGAOcontext
from ...src.pipeline.fragments.DisjunctionPipeLine import DisjunctionPipeLine
from ...src.pipeline.fragments.NepPipeLine import NepPipeLine
from ...src.pipeline.fragments.GapPipeLine import GapPipeLine
from .abstract.generic_pipeline import GenericPipeLine
from ..core.elapsed_time import create_zero_time_profile
from ..util.constants import FROM_CLAUSE, START, DONE, RUNNING, PROJECTION, \
    GROUP_BY, AGGREGATE, ORDER_BY, LIMIT, UNION, ERROR, OK
from ...src.core.aggregation import Aggregation
from ...src.core.from_clause import FromClause
from ...src.core.groupby_clause import GroupBy
from ...src.core.limit import Limit
from ...src.core.orderby_clause import OrderBy
from ...src.core.projection import Projection


class IOState:
    def __init__(self, inp, output):
        self.input = inp
        self.output = output
        self.dic = {
            "input": self.input,
            "output": self.output
        }

    def get_dic(self):
        return self.dic


class ExtractionPipeLine(DisjunctionPipeLine,
                         NepPipeLine,
                         GapPipeLine):

    def __init__(self, connectionHelper, name="Extraction PipeLine"):
        DisjunctionPipeLine.__init__(self, connectionHelper, name)
        NepPipeLine.__init__(self, connectionHelper)
        self.genPipelineCtx = None
        self.pj = None
        self.global_pk_dict = None
        self.pgao_ctx = PGAOcontext()

    def process(self, query: str):
        return GenericPipeLine.process(self, query)

    def doJob(self, query, qe=None):
        return GenericPipeLine.doJob(self, query, qe)

    def verify_correctness(self, query, result):
        GenericPipeLine.verify_correctness(self, query, result)

    def extract(self, query):
        self.connectionHelper.connectUsingParams()
        self.info[UNION] = "SKIPPED"
        '''
        From Clause Extraction
        '''
        self.update_state(FROM_CLAUSE + START)
        fc = FromClause(self.connectionHelper)
        fc.reset_data_schema()
        self.update_state(FROM_CLAUSE + RUNNING)
        check = fc.doJob(query)
        self.update_state(FROM_CLAUSE + DONE)
        fc.local_elapsed_time = fc.local_elapsed_time - fc.init.local_elapsed_time
        self.time_profile.update_for_from_clause(fc.local_elapsed_time, fc.app_calls)
        io = IOState(query, fc.core_relations)
        if not check or not fc.done:
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            self.update_state(ERROR)
            self.info[FROM_CLAUSE] = None
            io.output = ""
            return None
        self.info[FROM_CLAUSE] = fc.core_relations
        self.IO[FROM_CLAUSE] = io.get_dic()
        self.core_relations = fc.core_relations
        self.all_sizes = fc.init.all_sizes
        self.key_lists = fc.get_key_lists()
        self.global_pk_dict = fc.init.global_pk_dict

        eq = self._after_from_clause_extract(query, self.core_relations)
        self.connectionHelper.closeConnection()
        return eq

    def _after_from_clause_extract(self, query, core_relations):

        time_profile = create_zero_time_profile()

        check, time_profile = self._mutation_pipeline(core_relations, query, time_profile)
        if not check:
            # self.error += OK
            self.logger.error(self.error)
            self.update_state(ERROR)
            self.time_profile.update(time_profile)
            return None

        check, time_profile = self._extract_disjunction(self.aoa.arithmetic_filters,
                                                        core_relations, query, time_profile)
        if not check:
            # self.error += OK
            self.logger.error(self.error)
            self.update_state(ERROR)
            self.time_profile.update(time_profile)
            return None

        self.time_profile.update(time_profile)
        self.__gen_pipeline_preprocess(core_relations)

        '''
        Projection Extraction
        '''
        self.update_state(PROJECTION + START)
        self.pj = Projection(self.connectionHelper, self.genPipelineCtx)

        self.update_state(PROJECTION + RUNNING)
        check = self.pj.doJob(query)
        self.update_state(PROJECTION + DONE)
        self.time_profile.update_for_projection(self.pj.local_elapsed_time, self.pj.app_calls)
        self.info[PROJECTION] = {'names': self.pj.projection_names, 'attribs': self.pj.projected_attribs}
        if not check:
            self.update_state(ERROR)
            self.info[PROJECTION] = None
            self.logger.error("Cannot find projected attributes. ")
            return None
        if not self.pj.done:
            self.update_state(ERROR)
            self.info[PROJECTION] = None
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            return None
        self.pgao_ctx.projection = self.pj

        self.update_state(GROUP_BY + START)
        gb = GroupBy(self.connectionHelper, self.genPipelineCtx, self.pgao_ctx)
        self.update_state(GROUP_BY + RUNNING)
        check = gb.doJob(query)
        self.update_state(GROUP_BY + DONE)
        self.time_profile.update_for_group_by(gb.local_elapsed_time, gb.app_calls)
        self.info[GROUP_BY] = gb.group_by_attrib
        if not check:
            self.update_state(ERROR)
            self.info[GROUP_BY] = None
            self.logger.info("Cannot find group by attributes. ")
        if not gb.done:
            self.update_state(ERROR)
            self.info[GROUP_BY] = None
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            return None
        self.pgao_ctx.group_by = gb

        self.update_state(AGGREGATE + START)
        agg = Aggregation(self.connectionHelper, self.genPipelineCtx, self.pgao_ctx)
        self.update_state(AGGREGATE + RUNNING)
        check = agg.doJob(query)
        self.update_state(AGGREGATE + DONE)
        self.time_profile.update_for_aggregate(agg.local_elapsed_time, agg.app_calls)
        self.info[AGGREGATE] = agg.global_aggregated_attributes
        if not check:
            self.update_state(ERROR)
            self.info[AGGREGATE] = None
            self.logger.info("Cannot find aggregations.")
        if not agg.done:
            self.update_state(ERROR)
            self.info[AGGREGATE] = None
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            return None
        self.pgao_ctx.aggregate = agg

        self.update_state(ORDER_BY + START)
        ob = OrderBy(self.connectionHelper, self.genPipelineCtx, self.pgao_ctx)
        self.update_state(ORDER_BY + RUNNING)
        ob.doJob(query)
        self.update_state(ORDER_BY + DONE)
        self.time_profile.update_for_order_by(ob.local_elapsed_time, ob.app_calls)
        self.info[ORDER_BY] = ob.orderBy_string
        if not ob.has_orderBy:
            self.update_state(ERROR)
            self.info[ORDER_BY] = None
            self.logger.info("Cannot find aggregations.")
        if not ob.done:
            self.update_state(ERROR)
            self.info[ORDER_BY] = None
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            return None
        self.pgao_ctx.order_by = ob

        self.update_state(LIMIT + START)
        lm = Limit(self.connectionHelper, self.genPipelineCtx, self.pgao_ctx)
        self.update_state(LIMIT + RUNNING)
        lm.doJob(query)
        self.update_state(LIMIT + DONE)
        self.time_profile.update_for_limit(lm.local_elapsed_time, lm.app_calls)
        self.info[LIMIT] = lm.limit
        if lm.limit is None:
            self.update_state(ERROR)
            self.info[LIMIT] = None
            self.logger.info("Cannot find limit.")
        if not lm.done:
            self.update_state(ERROR)
            self.info[LIMIT] = None
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            return None

        # WI-36: reclassify any uncorrelated EXISTS gate out of core_relations
        # BEFORE the FROM/instances are handed to the query generator. A gate
        # relation is load-bearing (core), non-projecting, non-joining, and
        # NON-SCALING; left in core_relations it would be comma-joined into FROM
        # as a wrong cross join. Pull it out and declare it as an EXISTS conjunct.
        exists_gates, core_relations = self._reclassify_exists_gates(core_relations, query)
        if exists_gates:
            self._strip_gate_relations(exists_gates)
        self.q_generator.exists_gates = exists_gates

        self.q_generator.get_datatype = self.filter_extractor.get_datatype  # method
        self.q_generator.from_clause = core_relations
        # Phase 6: alias-aware FROM rendering. For single-instance tables this
        # is a no-op (alias == base table); for multi-instance it emits the
        # `T alias` syntax needed for self-joins.
        self.q_generator.instances = self.instances
        self.q_generator.alias_to_table = self.alias_to_table
        # Per-alias column list (from the view minimizer / cardinality probe).
        # Lets QSG qualify SELECT cols against synthetic aliases even when the
        # column never appears in a WHERE-clause predicate (e.g. SJ3's n_name).
        if self.global_alias_row_dict:
            self.q_generator.cols_by_alias = {
                alias: entry.get("cols", ())
                for alias, entry in self.global_alias_row_dict.items()
            }
        self.q_generator.algebraic_predicates = self.aoa
        self.q_generator.arithmetic_disjunctions = self.genPipelineCtx

        self.q_generator.pgaoCtx = self.pgao_ctx
        self.q_generator.limit = lm
        eq = self.q_generator.formulate_query_string()
        self.logger.debug("extracted query:\n", eq)

        # Within-attribute OR-of-intervals (gap-aware). Runs as a NEP-style diff
        # pass AFTER Projection so Q_E carries Qh's real projection -- which lets
        # the Re EXCEPT ALL Rh witness search work for bare-column, scalar-
        # expression AND aggregate projections. Gated by config gap_aware=yes.
        # Placed before NEP so a wide continuous gap is carved as one OR-of-
        # BETWEEN rather than NEP emitting a flurry of single '<>' points.
        eq = self._extract_gap(query, eq, core_relations)

        eq = self._extract_NEP(core_relations, self.all_sizes, query, self.genPipelineCtx)

        # Phase 7: verification probe. Compares Qh vs the extracted Q_E to
        # catch multi-instance / self-join shapes that the floor signal missed
        # (e.g. when an aggregation collapses the result-cardinality fingerprint).
        # The probe borrows the filter_extractor's app executable since the
        # pipeline doesn't carry its own.
        try:
            from ..core.multiplicity_probe import MultiplicityProbe
            executor = getattr(self.filter_extractor, "app", None)
            if executor is not None:
                probe = MultiplicityProbe(self.connectionHelper, executor, logger=self.logger)
                report = probe.run(query, eq, min_card=self.min_card, instances=self.instances)
                if report.get("warnings"):
                    for w in report["warnings"]:
                        self.logger.info(f"MultiplicityProbe: {w}")
                self.info["MULTIPLICITY_PROBE"] = report
        except Exception as e:
            self.logger.debug(f"MultiplicityProbe skipped: {e}")
        return eq

    def __gen_pipeline_preprocess(self, core_relations):
        self.logger.debug("aoa post-process.")
        self.genPipelineCtx = GenPipelineContext(core_relations, self.aoa,
                                                 self.filter_extractor, self.global_min_instance_dict,
                                                 self.or_predicates)
        self.logger.debug(self.genPipelineCtx.arithmetic_filters)
        self.logger.debug(self.genPipelineCtx.global_join_graph)
        self.logger.debug(self.genPipelineCtx.filter_in_predicates)
        self.logger.debug(self.genPipelineCtx.filter_attrib_dict)
        self.genPipelineCtx.doJob()
        self.logger.debug("after doJob...")
        self.logger.debug(self.genPipelineCtx.arithmetic_filters)
        self.logger.debug(self.genPipelineCtx.global_join_graph)
        self.logger.debug(self.genPipelineCtx.filter_in_predicates)
        self.logger.debug(self.genPipelineCtx.filter_attrib_dict)

    # ------------------------------------------------------------------ WI-36
    def _reclassify_exists_gates(self, core_relations, query):
        """Detect uncorrelated EXISTS gates among ``core_relations``.

        Returns ``(gates, kept_relations)`` where ``gates`` is a list of
        ``{'tab', 'kind': 'EXISTS'}`` and ``kept_relations`` is ``core_relations``
        with the gate relations removed. Fail-closed: if anything is inconclusive
        the relation stays in ``core_relations`` (status quo = comma-FROM).

        A core relation ``T`` is an uncorrelated EXISTS gate iff it is
        (1) load-bearing  — guaranteed by core membership;
        (2) non-projecting — no projected column is attributed to ``T``;
        (3) non-joining    — no equi-join / AOA edge touches ``T``;
        (4) non-scaling    — duplicating one ``T`` row leaves ``|Qh|`` unchanged
                             (the decisive discriminator vs a cross join).
        """
        if not getattr(self.connectionHelper.config, 'detect_exists', False):
            return [], core_relations
        if not core_relations or len(core_relations) < 2:
            # A gate needs at least one outer (non-gate) relation alongside it.
            return [], core_relations
        try:
            return self.__reclassify_exists_gates_impl(core_relations, query)
        except Exception as e:
            # Fail closed: any unexpected failure in gate detection must leave
            # the baseline extraction (comma-FROM) intact, never abort it.
            self.logger.debug(f"WI-36: gate reclassification failed ({e}); keeping status quo.")
            return [], core_relations

    def __reclassify_exists_gates_impl(self, core_relations, query):
        projected = self._gate_projected_tables()      # condition (2)
        joined = self._gate_joined_tables()            # condition (3)
        candidates = [t for t in core_relations
                      if t not in projected and t not in joined]
        if not candidates:
            return [], core_relations

        from ..core.exists_gate_probe import ExistsGateProbe
        probe = ExistsGateProbe(self.connectionHelper, self.genPipelineCtx)

        gates = []
        kept = list(core_relations)
        for tab in candidates:
            if len(kept) <= 1:
                break  # never strip the last outer relation (fail-safe)
            try:
                verdict = probe.is_nonscaling_gate(query, tab)
            except Exception as e:
                self.logger.debug(f"WI-36: probe error on '{tab}': {e}; kept (fail-closed).")
                verdict = None
            if verdict is True:
                gates.append({'tab': tab, 'kind': 'EXISTS'})
                kept.remove(tab)
                self.logger.info(
                    f"WI-36: reclassified '{tab}' as an uncorrelated EXISTS gate "
                    f"(non-projecting, non-joining, non-scaling).")
            elif verdict is False:
                self.logger.info(
                    f"WI-36: '{tab}' scales on row-duplication -> cross/inner join; kept in FROM.")
            else:
                self.logger.info(
                    f"WI-36: '{tab}' EXISTS-gate probe inconclusive -> kept in FROM (fail-closed).")
        return gates, kept

    def _gate_projected_tables(self):
        """Tables that contribute a projected (or aggregated) output column.

        Projection stores only the *column name* in ``projected_attribs``; the
        per-relation attribution lives in ``Projection.dependencies`` (lists of
        ``(tab, attr)`` tuples). Also fold in aggregated-column tables, since an
        aggregate's projected attribute can be empty (a COUNT has no value-
        dependency). A gate appears in neither.
        """
        tabs = set()
        deps = getattr(self.pj, 'dependencies', None) or []
        for dep_list in deps:
            for node in (dep_list or []):
                if isinstance(node, (tuple, list)) and len(node) >= 2 and isinstance(node[0], str):
                    tabs.add(node[0])
        # PGAOcontext.aggregate is a write-only property (its getter raises
        # NotImplementedError); the concrete data lives in `aggregated_attributes`.
        # Each entry is (attr, op); fold in any tab-qualified aggregated column.
        for a in (getattr(self.pgao_ctx, 'aggregated_attributes', None) or []):
            col = a[0] if isinstance(a, (tuple, list)) and len(a) >= 1 else None
            if isinstance(col, (tuple, list)) and len(col) >= 2 and isinstance(col[0], str):
                tabs.add(col[0])
        return tabs

    def _gate_joined_tables(self):
        """Tables touched by any equi-join edge or AOA/theta edge.

        Nodes are ``(tab, attr)`` tuples (or constants, which are skipped). A
        pure uncorrelated gate has only single-table constant filters on its own
        columns, so it appears in none of these.
        """
        tabs = set()

        def add_node(node):
            if isinstance(node, (tuple, list)) and len(node) == 2 \
                    and isinstance(node[0], str) and isinstance(node[1], str):
                tabs.add(node[0])

        aoa = self.aoa
        for edge in (getattr(aoa, 'algebraic_eq_predicates', None) or []):
            for node in edge:
                add_node(node)
        for pred in (getattr(aoa, 'aoa_predicates', None) or []):
            for node in pred:
                add_node(node)
        for pred in (getattr(aoa, 'aoa_less_thans', None) or []):
            for node in pred:
                add_node(node)
        return tabs

    def _strip_gate_relations(self, exists_gates):
        """Remove the gate relations from ``self.instances`` / ``self.alias_to_table``
        so they never reach the FROM clause (which is built from instances)."""
        gate_tabs = {g['tab'] for g in exists_gates}
        if self.instances:
            self.instances = [inst for inst in self.instances if inst.table not in gate_tabs]
        if self.alias_to_table:
            self.alias_to_table = {a: t for a, t in self.alias_to_table.items()
                                   if t not in gate_tabs}

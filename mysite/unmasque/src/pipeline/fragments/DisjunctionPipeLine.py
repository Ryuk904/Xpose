import copy
from abc import abstractmethod, ABC

from ...core.abstract.MinimizerBase import Minimizer
from ...util.constants import UNMASQUE
from ....src.core.aoa import InequalityPredicate
from ....src.core.cs2 import Cs2
from ....src.core.db_restorer import DbRestorer
from ....src.core.alias_aware_minimizer import AliasAwareMinimizer
from ....src.core.cross_alias_predicate import CrossAliasPredicate
from ....src.core.per_alias_pinned_filter import PerAliasPinnedFilter
from ....src.core.equi_join import U2EquiJoin
from ....src.core.filter import Filter
from ....src.core.multiplicity import MultiplicityDetect
from ....src.core.per_alias_filter import PerAliasFilter
from ....src.core.view_minimizer import ViewMinimizer
from ....src.pipeline.abstract.generic_pipeline import GenericPipeLine
from ....src.util.aoa_utils import get_constants_for
from ....src.util.constants import FILTER, INEQUALITY, DONE, RUNNING, START, EQUALITY, DB_MINIMIZATION, \
    SAMPLING, RESTORE_DB, ERROR
from ....src.util.utils import get_format, get_val_plus_delta
from ....src.util.error_handling import UnmasqueError


def get_eq_filters(arithmetics):
    return [pred for pred in arithmetics if pred[2] in ['equal', '=']]


class DisjunctionPipeLine(GenericPipeLine, ABC):

    def __init__(self, connectionHelper, name):
        GenericPipeLine.__init__(self, connectionHelper, name)
        self.aoa = None
        self.equi_join = None
        self.filter_extractor = None
        self.db_restorer = None
        self.global_min_instance_dict = None
        self.key_lists = None
        # mult(T) for every core relation -- detected after minimization
        # (Algorithm 1 / MultiplicityDetect).  Defaults to 1 everywhere.
        self.mult = {}
        # Alias-aware D_min (Algorithm 2): {table -> [header, row, ...]} with
        # mult(table) rows per multi-instance table.  None when not computed.
        self.alias_aware_min_instance_dict = None
        # Cross-alias predicates (Algorithm 3): {table -> [pred dict, ...]}.
        self.cross_alias_predicates = {}
        self.cross_alias_coupled_columns = {}
        # Output-column -> (table, alias_index, source_col) from the discriminator
        # run (the alias-lift of the projection extractor).
        self.projection_alias_attribution = {}
        # Per-alias filter bounds (Algorithm 4): {table -> {col -> {...}}}.
        self.per_alias_filters = {}
        # ... attributed to specific aliases by the discriminator probe (report §F):
        # {table -> {alias_index -> {col -> {'lower':l,'upper':u}}}}.
        self.per_alias_pinned_filters = {}
        # Candidate multi-instance query string built by the alias-aware assembler,
        # and whether it verified against the database (True/False/None).
        self.alias_aware_query = None
        self.alias_aware_query_verified = None

    def _mutation_pipeline(self, core_relations, query, time_profile, restore_details=None):
        self.update_state(RESTORE_DB + START)
        self.db_restorer = DbRestorer(self.connectionHelper, core_relations)
        self.db_restorer.set_data_schema()
        self.db_restorer.set_all_sizes(self.all_sizes)
        # for tab in core_relations:
        #    self.db_restorer.last_restored_size[tab] = self.all_sizes[tab]
        self.update_state(RESTORE_DB + RUNNING)
        check = self.db_restorer.doJob(restore_details)
        self.update_state(RESTORE_DB + DONE)
        time_profile.update_for_db_restore(self.db_restorer.local_elapsed_time, self.db_restorer.app_calls)
        if not check or not self.db_restorer.done:
            self.info[RESTORE_DB] = None
            self.logger.info("DB restore failed!")
            return False, time_profile
        self.info[RESTORE_DB] = {'size': self.db_restorer.last_restored_size}

        """
        Correlated Sampling
        """
        self.update_state(SAMPLING + START)
        cs2 = Cs2(self.connectionHelper, self.all_sizes, core_relations, self.key_lists, perc_based_cutoff=True)
        self.update_state(SAMPLING + RUNNING)
        check = cs2.doJob(query)
        self.update_state(SAMPLING + DONE)
        time_profile.update_for_cs2(cs2.local_elapsed_time, cs2.app_calls)
        if not check or not cs2.done:
            self.info[SAMPLING] = None
            self.logger.info("Sampling failed!")
        if not self.connectionHelper.config.use_cs2:
            self.info[SAMPLING] = SAMPLING + "DISABLED"
            self.logger.info("Sampling is disabled!")
        else:
            self.info[SAMPLING] = {'sample': cs2.sample, 'size': cs2.sizes}

        '''
        Multi-Instance (self-join) detection -- Algorithm 1, run BEFORE minimization
        (on the sampled DB) so the minimizer can keep at least mult(R) rows for a
        k-way self-join instead of collapsing it.  Opt-in ([feature] multi_instance);
        failures here never abort the pipeline (mult just stays 1 everywhere).
        '''
        self.mult = {tab: 1 for tab in core_relations}
        if getattr(self.connectionHelper.config, "detect_multi_instance", False):
            try:
                md = MultiplicityDetect(self.connectionHelper, core_relations)
                md.doJob(query)
                self.mult = dict(md.mult)
                self.info['MULTIPLICITY'] = {'mult': dict(md.mult), 'method': dict(md.method_used),
                                             'ambiguous': sorted(md.ambiguous)}
                if any(v > 1 for v in self.mult.values()):
                    self.logger.info("Detected multi-instance relations: " +
                                     ", ".join(f"{t} x{k}" for t, k in self.mult.items() if k > 1))
            except Exception as e:
                self.logger.error("MultiplicityDetect stage failed; assuming mult=1 for all relations.", str(e))
                self.mult = {tab: 1 for tab in core_relations}

        """
            View based Database Minimization
            """
        self.update_state(DB_MINIMIZATION + START)
        vm = ViewMinimizer(self.connectionHelper, core_relations, self.db_restorer.last_restored_size, cs2.passed)
        # per-table floor: a k-way self-join keeps >= k rows in the (live) D_min.
        vm.min_rows = {t: int(k) for t, k in self.mult.items() if int(k) > 1}
        self.update_state(DB_MINIMIZATION + RUNNING)
        try:
            check = vm.doJob(query)
            self.update_state(DB_MINIMIZATION + DONE)
            time_profile.update_for_view_minimization(vm.local_elapsed_time, vm.app_calls)
        except UnmasqueError as e:
            e.report_to_logger(self.logger)

        if not check or not vm.done:
            # self.error = "Cannot do database minimization"
            self.logger.error(self.error)
            self.update_state(ERROR)
            self.info[DB_MINIMIZATION] = None
            return False, time_profile

        self.db_restorer.update_last_restored_size(vm.all_sizes)
        self.info[DB_MINIMIZATION] = vm.global_min_instance_dict
        self.global_min_instance_dict = copy.deepcopy(vm.global_min_instance_dict)

        # The floored minimizer leaves >= mult(R) rows in a multi-instance table.
        # global_min_instance_dict keeps the full k-row witness set (Algorithms 2-4 use
        # it), but the *live* table (and its D_min copy) are re-collapsed to the first row
        # so the (not yet alias-aware) Filter / equi-join / AOA / generation extractors keep
        # seeing a single witness row, exactly as in the legacy pipeline -- UNLESS that
        # 1-row collapse makes Q_H UNFIT (a strict self-join `t1.x < t2.x` has no single
        # FIT row), in which case the k-row witness set is kept so the legacy extractors at
        # least start from a FIT instance (they still won't fully handle a strict self-join;
        # that needs the alias-aware extractors -- see docs/multi_instance.md §6).
        mi_tables = [t for t, k in self.mult.items() if int(k) > 1
                     and self.global_min_instance_dict.get(t)
                     and len(self.global_min_instance_dict[t]) > 2]
        if getattr(self.connectionHelper.config, "detect_multi_instance", False) and mi_tables:
            try:
                self.connectionHelper.execute_sql(
                    [f"set search_path='{self.connectionHelper.config.schema}';"])
                q = self.connectionHelper.queries

                def _rebuild(_tab, _rows):
                    _fq = f"{self.connectionHelper.config.schema}.{_tab}"
                    _attribs = "(" + ", ".join(str(c) for c in self.global_min_instance_dict[_tab][0]) + ")"
                    self.connectionHelper.execute_sql([q.truncate_table(_fq)])
                    self.connectionHelper.execute_sql_with_params(
                        q.insert_into_tab_attribs_format(_attribs, "", _fq), [tuple(r) for r in _rows])
                    self.connectionHelper.execute_sql(
                        [q.drop_table_cascade(q.get_dmin_tabname(_tab)),
                         q.create_table_as_select_star_from(q.get_dmin_tabname(_tab), _tab)])

                for tab in mi_tables:                         # try collapsing each to its 1st witness row
                    _rebuild(tab, [self.global_min_instance_dict[tab][1]])
                res = vm.app.doJob(query)
                if not (isinstance(res, list) and len(res) > 1):
                    for tab in mi_tables:                     # 1-row collapse broke Q_H -> restore k rows
                        _rebuild(tab, self.global_min_instance_dict[tab][1:])
                    self.logger.info("re-collapse to 1 row makes Q_H UNFIT (strict self-join); kept the "
                                     "k-row witness set -- legacy SPJGAOL extraction will be partial.")
            except Exception as e:
                self.logger.error("could not re-collapse multi-instance tables to a single row.", str(e))

        '''
        Alias-aware processing of the multi-instance relations -- Algorithms 2-4 + the
        per-(alias, attribute) probe.  mult(R) was detected *before* minimization (above)
        and used as the per-table floor, so the live D_min now keeps >= mult(R) rows for
        each multi-instance table.  Opt-in ([feature] multi_instance); purely additive.
        '''
        if getattr(self.connectionHelper.config, "detect_multi_instance", False):
            '''
            Alias-aware k-coloured halving -- Algorithm 2.
            For every relation with mult >= 2, compute a k-row witness set on
            which Q_H is still FIT.  Published as self.alias_aware_min_instance_dict
            for Algorithms 3 & 4; does NOT disturb the legacy single-row D_min that
            the (not-yet-alias-aware) downstream extractors consume.
            '''
            if any(v > 1 for v in self.mult.values()):
                try:
                    aam = AliasAwareMinimizer(self.connectionHelper, core_relations,
                                              self.mult, self.global_min_instance_dict)
                    aam.doJob(query)
                    self.alias_aware_min_instance_dict = dict(aam.alias_aware_min_instance_dict)
                    self.info['ALIAS_AWARE_DMIN'] = {
                        'sizes': {t: max(0, len(v) - 1) for t, v in self.alias_aware_min_instance_dict.items()
                                  if v is not None},
                        'witnessed': sorted(aam.expanded),
                        'fallback': sorted(aam.fallback)}
                    self.logger.info("Alias-aware D_min built for: " +
                                     ", ".join(f"{t} x{self.mult[t]}" for t in self.mult if self.mult[t] > 1))
                    self.logger.info("NOTE: downstream predicate extraction is not yet alias-aware "
                                     "(Algorithm 4 pending); the extracted query may still collapse "
                                     "per-alias filters / HAVING.")
                except Exception as e:
                    self.logger.error("AliasAwareMinimizer stage failed; alias-aware D_min not built.", str(e))
                    self.alias_aware_min_instance_dict = None

            '''
            Cross-alias predicate extraction -- Algorithm 3.
            Recovers intra-alias self-equi-joins (t.c = t.c') and same-column
            inter-alias predicates (t_p.c REL t_q.c).  Published on
            self.cross_alias_predicates for the (still to be made alias-aware)
            query assembler.  Purely additive.
            '''
            if self.alias_aware_min_instance_dict and any(v > 1 for v in self.mult.values()):
                try:
                    cap = CrossAliasPredicate(self.connectionHelper, core_relations,
                                              self.mult, self.alias_aware_min_instance_dict)
                    cap.doJob(query)
                    self.cross_alias_predicates = dict(cap.cross_alias_predicates)
                    self.cross_alias_coupled_columns = dict(cap.coupled_columns)
                    self.projection_alias_attribution = dict(cap.output_attribution)
                    self.info['CROSS_ALIAS_PREDICATES'] = {
                        'predicates': dict(cap.cross_alias_predicates),
                        'coupled_columns': dict(cap.coupled_columns),
                        'output_attribution': dict(cap.output_attribution),
                        'notes': dict(cap.notes)}
                    for t, ps in self.cross_alias_predicates.items():
                        if ps:
                            self.logger.info(f"cross-alias predicates [{t}]: {ps}")
                except Exception as e:
                    self.logger.error("CrossAliasPredicate stage failed; no cross-alias predicates.", str(e))
                    self.cross_alias_predicates = {}

            '''
            Per-alias filter extraction -- Algorithm 4.
            Recovers per-alias filter bounds on a multi-instance table's columns
            via a cardinality-step search.  Published on self.per_alias_filters.
            (Per-alias HAVING is left as future work.)  Purely additive.
            '''
            if any(v > 1 for v in self.mult.values()):
                try:
                    paf = PerAliasFilter(self.connectionHelper, core_relations, self.mult,
                                         self.global_min_instance_dict, self.cross_alias_predicates,
                                         self.cross_alias_coupled_columns)
                    paf.doJob(query)
                    self.per_alias_filters = dict(paf.per_alias_filters)
                    self.info['PER_ALIAS_FILTERS'] = {'filters': dict(paf.per_alias_filters),
                                                      'notes': dict(paf.notes)}
                    for t, cols in self.per_alias_filters.items():
                        if cols:
                            self.logger.info(f"per-alias filters [{t}]: {cols}")
                except Exception as e:
                    self.logger.error("PerAliasFilter stage failed; no per-alias filters.", str(e))
                    self.per_alias_filters = {}

            '''
            Per-(alias, attribute) discriminator probe (report §F) -- attributes the
            per-alias filter bounds to specific aliases when an inter-alias chain pins
            them.  Published on self.per_alias_pinned_filters.  Purely additive.
            '''
            if self.per_alias_filters and self.cross_alias_predicates and self.alias_aware_min_instance_dict:
                try:
                    papf = PerAliasPinnedFilter(self.connectionHelper, core_relations, self.mult,
                                                self.alias_aware_min_instance_dict,
                                                self.cross_alias_predicates, self.per_alias_filters)
                    papf.doJob(query)
                    self.per_alias_pinned_filters = dict(papf.pinned_filters)
                    self.info['PER_ALIAS_PINNED_FILTERS'] = {'pinned': dict(papf.pinned_filters),
                                                             'notes': dict(papf.notes)}
                    for t, by_a in self.per_alias_pinned_filters.items():
                        self.logger.info(f"pinned per-alias filters [{t}]: {by_a}")
                except Exception as e:
                    self.logger.error("PerAliasPinnedFilter stage failed; no pinned bounds.", str(e))
                    self.per_alias_pinned_filters = {}

        '''
        Constant Filter Extraction
        '''
        self.update_state(FILTER + START)
        self.filter_extractor = Filter(self.connectionHelper, core_relations, self.global_min_instance_dict)
        self.update_state(FILTER + RUNNING)
        check = self.filter_extractor.doJob(query)
        self.update_state(FILTER + DONE)
        time_profile.update_for_where_clause(self.filter_extractor.local_elapsed_time,
                                             self.filter_extractor.app_calls)
        if not self.filter_extractor.done:
            self.update_state(ERROR)
            self.info[FILTER] = None
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            return False, time_profile
        if not check:
            self.info[FILTER] = None
            self.logger.info("No filter found")
        self.info[FILTER] = self.filter_extractor.filter_predicates

        '''
        Equality Relations (Equi-join + Constant Equality filters) Extraction
        '''
        self.update_state(EQUALITY + START)
        self.update_state(EQUALITY + RUNNING)
        self.equi_join = U2EquiJoin(self.connectionHelper, core_relations, self.filter_extractor.filter_predicates,
                                    self.filter_extractor, self.global_min_instance_dict)
        check = self.equi_join.doJob(query)
        self.update_state(EQUALITY + DONE)
        time_profile.update_for_where_clause(self.equi_join.local_elapsed_time, self.equi_join.app_calls)
        if not self.equi_join.done:
            self.update_state(ERROR)
            self.info[EQUALITY] = None
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            return False, time_profile
        if not check:
            self.info[EQUALITY] = None
            self.logger.info("No Equality predicate found")
        combined_eq_predicates = self.equi_join.algebraic_eq_predicates + self.equi_join.arithmetic_eq_predicates
        self.info[EQUALITY] = combined_eq_predicates

        '''
        AOA Extraction
        '''
        self.update_state(INEQUALITY + START)
        self.aoa = InequalityPredicate(self.connectionHelper, core_relations, self.equi_join.pending_predicates,
                                       self.equi_join.arithmetic_eq_predicates,
                                       self.equi_join.algebraic_eq_predicates, self.filter_extractor,
                                       self.global_min_instance_dict)
        self.update_state(INEQUALITY + RUNNING)
        check = self.aoa.doJob(query)
        self.update_state(INEQUALITY + DONE)
        time_profile.update_for_where_clause(self.aoa.local_elapsed_time, self.aoa.app_calls)
        self.info[INEQUALITY] = self.aoa.aoa_predicates + self.aoa.aoa_less_thans + self.aoa.arithmetic_ineq_predicates
        if not check:
            self.info[INEQUALITY] = None
            self.logger.info("Cannot find inequality Predicates.")
        if not self.aoa.done:
            self.info[INEQUALITY] = None
            self.error = check if check else self.error_string
            self.logger.error(self.error)
            self.update_state(ERROR)
            return False, time_profile
        return True, time_profile

    def __get_predicates_in_action(self):
        return self.aoa.arithmetic_filters

    @abstractmethod
    def process(self, query: str):
        raise NotImplementedError("Trouble!")

    @abstractmethod
    def doJob(self, query, qe=None):
        raise NotImplementedError("Trouble!")

    @abstractmethod
    def verify_correctness(self, query, result):
        raise NotImplementedError("Trouble!")

    def _extract_disjunction(self, init_predicates, core_relations, query, time_profile):  # for once
        self.or_predicates = []
        curr_eq_predicates = copy.deepcopy(init_predicates)
        all_eq_predicates = [curr_eq_predicates]
        ids = list(range(len(curr_eq_predicates)))
        if self.connectionHelper.config.detect_or:
            try:
                time_profile = self.__run_extraction_loop(all_eq_predicates, core_relations, ids, query, time_profile)
            except Exception as e:
                self.update_state(ERROR)
                self.logger.error("Error in disjunction loop. ", str(e))
                return False, time_profile
        self.or_predicates = list(zip(*all_eq_predicates))
        return True, time_profile

    def __run_extraction_loop(self, all_eq_predicates, core_relations, ids, query, time_profile):
        while True:
            or_eq_predicates = []
            for i in ids:
                in_candidates = [copy.deepcopy(em[i]) for em in all_eq_predicates]
                self.logger.debug("Checking OR predicate of ", in_candidates)
                if not len(in_candidates[-1]):
                    or_eq_predicates.append(tuple())
                    continue

                restore_details = self.__get_OR_db_restoration_details(core_relations, in_candidates)
                self.logger.debug(restore_details)
                check, time_profile = self._mutation_pipeline(core_relations, query, time_profile, restore_details)
                if not check or not self.__get_predicates_in_action():
                    or_eq_predicates.append(tuple())
                else:
                    or_eq_predicates.append(self.__get_predicates_in_action()[i])
                self.logger.debug("new or predicates...", all_eq_predicates, or_eq_predicates)
            if all(element == tuple() for element in or_eq_predicates):
                break
            all_eq_predicates.append(or_eq_predicates)
        return time_profile

    def __get_OR_db_restoration_details(self, core_relations, in_candidates):
        restore_details = []
        for tab in core_relations:
            where_condition = self.__falsify_predicates(tab, in_candidates)
            restore_details.append((tab, where_condition))
        return restore_details

    def __falsify_predicates(self, tabname, held_predicates):
        always = "true"
        where_condition = always
        wheres = []
        for pred in held_predicates:
            if not len(pred):
                return where_condition
            tab, attrib, op, lb, ub = pred[0], pred[1], pred[2], pred[3], pred[4]
            if tab != tabname:
                continue
            datatype = self.filter_extractor.get_datatype((tab, attrib))
            val_lb, val_ub = get_format(datatype, lb), get_format(datatype, ub)

            if op.lower() in ['equal', '=']:
                where_condition = f"{attrib} != {val_lb}"
            elif op.lower() == 'like':
                where_condition = f"{attrib} NOT LIKE {val_lb}"
            else:
                delta, _ = get_constants_for(datatype)
                val_lb_minus_one = get_format(datatype, get_val_plus_delta(datatype, lb, -1 * delta))
                val_ub_plus_one = get_format(datatype, get_val_plus_delta(datatype, ub, 1 * delta))
                where_condition = f"({attrib} <= {val_lb_minus_one} or {attrib} >= {val_ub_plus_one})"
            wheres.append(where_condition)
        where_condition = " and ".join(wheres) if len(wheres) else always
        self.logger.debug(where_condition)
        return where_condition

    @abstractmethod
    def extract(self, query):
        pass
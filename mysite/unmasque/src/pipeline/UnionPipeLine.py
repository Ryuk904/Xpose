import datetime
import time

from .OuterJoinPipeLine import OuterJoinPipeLine
from ..core.union import Union
from ..core.set_op_probe import SetOpProbe, SET_OP_UNION, SET_OP_UNION_ALL
from ..util.constants import UNION, START, DONE, RUNNING, WRONG, FROM_CLAUSE, ERROR


class UnionPipeLine(OuterJoinPipeLine):

    def __init__(self, connectionHelper, name="Union_PipeLine"):
        super().__init__(connectionHelper, name)

    def extract(self, query):
        # opening and closing connection actions are vital.
        self.connectionHelper.connectUsingParams()
        self.update_state(UNION + START)
        union = Union(self.connectionHelper)
        self.update_state(UNION + RUNNING)
        p, pstr, union_profile = union.doJob(query)
        self.update_state(UNION + DONE)
        self.connectionHelper.closeConnection()

        self.info[UNION] = [list(ele) for ele in p]
        print(f"Now end {str(datetime.datetime.now().time())}")
        self.__update_time_profile(union, union_profile)
        self.core_relations = [item for subset in p for item in subset]
        self.all_relations = union.all_relations
        self.all_sizes = union.all_sizes
        self.key_lists = union.key_lists
        self.logger.debug(f"relations, {self.all_relations} all sizes, {self.all_sizes}, key list: {self.key_lists}")
        u_eq = []
        # WI-14: per-branch UNION-vs-UNION-ALL dedup verdicts, collected while
        # each branch is isolated (every other branch's relations nullified).
        set_op_verdicts = []
        # return 0

        for rels in p:
            self.info[UNION] = [list(ele) for ele in p]
            core_relations = [r for r in rels]
            self.logger.debug(core_relations)
            self.info[FROM_CLAUSE] = core_relations

            nullify = set(self.all_relations).difference(core_relations)

            self.connectionHelper.connectUsingParams()
            self.__nullify_relations(nullify)
            eq = self._after_from_clause_extract(query, core_relations)
            # WI-14: probe the hidden set operator's dedup behaviour on this
            # isolated branch before reverting the nullifications. Must run
            # while the other branches are still empty so Qh returns only this
            # branch's contribution under the set operator.
            if eq is not None:
                self.__probe_set_op(query, core_relations, set_op_verdicts)
            self.__revert_nullifications(nullify)
            self.q_generator.reset()
            self.connectionHelper.closeConnection()

            if eq is not None:
                self.logger.debug(eq)
                eq = eq.replace('Select', '(Select')
                eq = eq.replace(';', ')')
                u_eq.append(eq)
            else:
                self.pipeLineError = True
                break

        set_op = self._resolve_set_op(set_op_verdicts)
        if not set_op_verdicts:
            self.logger.info("SetOpProbe: no decisive branch; defaulting to UNION ALL.")
        elif SET_OP_UNION_ALL in set_op_verdicts and SET_OP_UNION in set_op_verdicts:
            self.logger.info("SetOpProbe: conflicting per-branch verdicts (possible mixed "
                             "UNION / UNION ALL, unrepresentable); defaulting to UNION ALL.")
        else:
            self.logger.info(f"SetOpProbe: set operator resolved to {set_op}.")

        result = self.__post_process(pstr, u_eq, set_op)
        return result

    def __probe_set_op(self, query, core_relations, verdicts):
        """WI-14: append this branch's UNION-vs-UNION-ALL verdict (if any).

        The probe runs *Qh itself* (with the other branches nullified), so it
        observes the hidden set operator's behaviour directly — it is immune to
        anything in *our* extracted branch. The one thing that genuinely
        confounds the no-growth signal is a real per-branch AGGREGATE
        (sum/count/...), which absorbs the duplicate row into a group's value
        regardless of the operator and would masquerade as UNION. So we skip a
        branch only when our extraction found a genuine aggregate.

        A bare per-branch GROUP BY with NO aggregate is *not* skipped: the
        framework's group-by stage spuriously infers one on every branch of a
        set-`UNION` query (the union's dedup reads as grouping), so skipping
        those would make UNION undetectable. The probe stays correct because it
        watches Qh, whose branch is the plain projection. (The residual blind
        spot — a *genuine* per-branch ``SELECT DISTINCT`` under ``UNION ALL`` —
        is rare and out of the current coverage, where DISTINCT is undetected.)
        """
        if not self.connectionHelper.config.detect_union:
            return
        if not self.__branch_is_probeable():
            self.logger.debug("SetOpProbe: branch has an aggregate; skipping dedup probe.")
            return
        try:
            verdict = SetOpProbe(self.connectionHelper, self.genPipelineCtx).probe_branch(query)
        except Exception as e:
            self.logger.debug(f"SetOpProbe failed for branch {core_relations}: {e}")
            verdict = None
        if verdict is not None:
            self.logger.info(f"SetOpProbe verdict for branch {core_relations}: {verdict}")
            verdicts.append(verdict)

    def __branch_is_probeable(self):
        """True iff the just-extracted branch has NO genuine aggregate (read
        from our own extraction context — never from Qh's text).

        A genuine aggregate absorbs the probe's duplicate row regardless of the
        set operator, so it would falsely read as UNION. A bare GROUP BY with
        no aggregate does NOT disqualify the branch (see __probe_set_op)."""
        for agg in (getattr(self.pgao_ctx, "aggregated_attributes", None) or []):
            # aggregated_attributes entries are (attrib, op); op == '' for a
            # plain projected column, a non-empty token for an aggregate.
            if isinstance(agg, (list, tuple)) and len(agg) >= 2 and agg[1]:
                return False
        return True

    @staticmethod
    def _resolve_set_op(verdicts):
        """Combine per-branch verdicts into the global join token.

        Defaults to UNION ALL (historical behaviour, and the safe choice when
        D exposes no duplicate). Returns UNION only when dedup was positively
        observed on some branch and no branch showed bag-growth — a both-seen
        case signals a mixed operator we cannot represent, so we fall back to
        the safe UNION ALL default.
        """
        saw_all = SET_OP_UNION_ALL in verdicts
        saw_union = SET_OP_UNION in verdicts
        if saw_union and not saw_all:
            return SET_OP_UNION
        return SET_OP_UNION_ALL

    def __post_process(self, pstr, u_eq, set_op=SET_OP_UNION_ALL):
        u_Q = (f"\n {set_op} ").join(u_eq) + ";"
        # Single branch (no real set operation): unwrap the parenthesised arm.
        if len(u_eq) <= 1 and u_Q.startswith('(') and u_Q.endswith(');'):
            u_Q = u_Q[1:-2] + ';'
        result = ""
        if self.pipeLineError:
            self.error += f"Could not extract the query due to errors." \
                          f"\nHere's what I have as a half-baked answer:\n{pstr}\n"
            self.update_state(ERROR)
            return None
        result += u_Q
        return result

    def __update_time_profile(self, union, union_time):
        duration, app_calls = union_time[0], union_time[1]
        self.time_profile.update_for_from_clause(union.local_elapsed_time - duration, union.app_calls - app_calls)
        self.time_profile.update_for_union(duration, app_calls)

    def __nullify_relations(self, relations):
        for tab in relations:
            f_tab, f_union_tab, union_tab, original_tab = self.__get_void_names(tab)
            self.connectionHelper.execute_sql([self.connectionHelper.queries.alter_table_rename_to(f_tab, union_tab),
                                               self.connectionHelper.queries.create_table_like(f_tab, original_tab)],
                                              self.logger)
            self.connectionHelper.commit_transaction()
            self.all_sizes[tab] = 0

    def __get_void_names(self, tab):
        union_tab = self.connectionHelper.queries.get_union_tabname(tab)
        f_union_tab = f"{self.connectionHelper.config.schema}.{union_tab}"
        f_tab = f"{self.connectionHelper.config.schema}.{tab}"
        original_tab = f"{self.connectionHelper.config.user_schema}.{tab}"
        return f_tab, f_union_tab, union_tab, original_tab

    def __revert_nullifications(self, relations):
        for tab in relations:
            f_tab, f_union_tab, union_tab, original_tab = self.__get_void_names(tab)
            self.connectionHelper.execute_sql([self.connectionHelper.queries.drop_table_cascade(f_tab),
                                               self.connectionHelper.queries.alter_table_rename_to(
                                                   f_union_tab, tab)],
                                              self.logger)
            self.connectionHelper.commit_transaction()
            self.all_sizes[tab] = self.connectionHelper.execute_sql_fetchone_0(
                self.connectionHelper.queries.get_row_count(f_tab), self.logger)

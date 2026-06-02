from ..mocks.database import Schema
from ...src.core.abstract.AppExtractorBase import AppExtractorBase
from ...src.core.from_clause import FromClause


class UnionFromClause(Schema, AppExtractorBase):

    def __init__(self, connectionHelper):
        super().__init__(connectionHelper, "Union_fc")
        self.comtabs = None
        self.fromtabs = None
        self.to_nullify = None
        self.fromClause = FromClause(connectionHelper)

    def get_relations(self):
        if not self.fromClause.init.done:
            self.fromClause.init.doJob()
        self.set_all_relations(self.fromClause.all_relations)
        return self.all_relations

    def nullify_except(self, s_set):
        self.to_nullify = s_set.difference(self.comtabs)
        self.logger.debug("to nullify " + str(self.to_nullify))
        for tab in self.to_nullify:
            self.connectionHelper.execute_sql([self.connectionHelper.queries.alter_table_rename_to(
                self.get_original_table_name(tab), self._get_dirty_name(tab)),
                self.connectionHelper.queries.create_table_like(
                    self.get_original_table_name(tab),
                    self.get_original_table_name(self._get_dirty_name(tab)))])

    def run_query(self, QH):
        return self.app.doJob(QH)

    def revert_nullify(self):
        for tab in self.to_nullify:
            self.connectionHelper.execute_sql([self.connectionHelper.queries.drop_table(
                self.get_original_table_name(tab)),
                self.connectionHelper.queries.alter_table_rename_to(
                    self.get_original_table_name(self._get_dirty_name(tab)), tab),
                self.connectionHelper.queries.drop_table(
                    self.get_original_table_name(self._get_dirty_name(tab)))])

    def get_partial_QH(self, QH):
        return self.doJob(QH)

    def isEmpty(self, Res):
        return self.app.isQ_result_no_full_nullfree_row(Res)

    def extract_params_from_args(self, args):
        return args[0]

    def doActualJob(self, args=None):
        QH = self.extract_params_from_args(args)
        fromTabQ = set(self.get_fromTabs(QH))
        comTabQ = set(self.get_comTabs(QH, fromTabQ))
        self.logger.debug(f"from tab: {fromTabQ}, com tab: {comTabQ}")
        partTabQ = fromTabQ.difference(comTabQ)
        self.logger.debug(f"part tab: {partTabQ}")
        return partTabQ

    @staticmethod
    def _as_relation_list(result):
        """Normalise a FromClause.doJob result to a list of relation names.

        FromClause.doActualJob returns its core_relations list on success, but
        Base.doJob swallows an empty-result UnmasqueError (ERROR_006) and returns
        the status string OK ("OK "), and returns False / an exception string on
        other failures. For a UNION of disjoint single-table branches an EMPTY
        common-table set is the EXPECTED outcome (no relation appears in every
        arm), so a non-list result here is "no relations", not an error. Returning
        it verbatim lets the caller's set()/iteration char-split a string into junk
        relation names ({'O','K',' '} from "OK ") that then poison every branch
        partition in algorithm1.algo. Collapse any non-list result to [].
        """
        return result if isinstance(result, list) else []

    def get_fromTabs(self, QH):
        if self.fromtabs is None:
            self.fromtabs = self._as_relation_list(self.fromClause.doJob(QH, FromClause.TYPE_ERROR))
        self.logger.debug(str(self.fromtabs))
        return self.fromtabs

    def get_comTabs(self, QH, tabs):
        if self.comtabs is None:
            self.fromClause.set_check_relations(tabs)
            self.comtabs = self._as_relation_list(self.fromClause.doJob(QH, FromClause.TYPE_RENAME))
            self.fromClause.reset_check_relations()
        return self.comtabs


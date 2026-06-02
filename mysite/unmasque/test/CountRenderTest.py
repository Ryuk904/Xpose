import unittest

from mysite.unmasque.src.util.ConnectionFactory import ConnectionHelperFactory
from mysite.unmasque.src.util.QueryStringGenerator import QueryStringGenerator


class CountRenderTest(unittest.TestCase):
    """
    Regression test for WI-01: QueryStringGenerator must render a column COUNT as
    Count(<col>) (keeping its alias), not the bare invalid token 'Count'.

    Background: aggregation.analyze stores a column COUNT as (col, COUNT) where the
    COUNT constant is the string 'Count'; COUNT(*) is stored as ('', COUNT_STAR) where
    COUNT_STAR is 'Count(*)'. The select-clause renderer previously short-circuited on
    the substring test `COUNT in label`, which also matched 'Count(*)' and, for a column
    COUNT, emitted just 'Count' (dropping the column). The fix gates the short-circuit on
    `label == COUNT_STAR`, so column COUNT falls into the column-wrapping branch.

    This exercises the real __generate_select_clause method; it does not touch the DB
    (the connection is created lazily and never opened here).
    """

    @classmethod
    def setUpClass(cls):
        cls.conn = ConnectionHelperFactory().createConnectionHelper()

    def _render_select(self, projected, aggregated, names):
        qsg = QueryStringGenerator(self.conn)
        qsg.get_datatype = lambda ta: 'int'  # unused by the select clause; set for safety
        wc = qsg._workingCopy
        wc.global_projected_attributes = list(projected)
        wc.global_aggregated_attributes = list(aggregated)
        wc.projection_names = list(names)
        # name-mangled private method
        qsg._QueryStringGenerator__generate_select_clause()
        return wc.select_op

    def test_column_count_keeps_column_and_alias(self):
        out = self._render_select(
            projected=['o_orderpriority', 'o_orderkey'],
            aggregated=[('o_orderpriority', ''), ('o_orderkey', 'Count')],
            names=['o_orderpriority', 'order_count'],
        )
        self.assertIn('Count(o_orderkey) as order_count', out)
        # the buggy output was 'Count as order_count' / a bare 'Count'
        self.assertNotIn('Count as', out)

    def test_count_star_preserved(self):
        out = self._render_select(
            projected=['s_name', ''],
            aggregated=[('s_name', ''), ('', 'Count(*)')],
            names=['s_name', 'numwait'],
        )
        self.assertIn('Count(*) as numwait', out)

    def test_other_aggregates_unchanged(self):
        out = self._render_select(
            projected=['l_returnflag', 'l_quantity', 'l_extendedprice'],
            aggregated=[('l_returnflag', ''), ('l_quantity', 'Sum'), ('l_extendedprice', 'Avg')],
            names=['l_returnflag', 'sum_qty', 'avg_price'],
        )
        self.assertIn('Sum(l_quantity) as sum_qty', out)
        self.assertIn('Avg(l_extendedprice) as avg_price', out)

    def test_mixed_select_no_bare_count_token(self):
        out = self._render_select(
            projected=['o_orderpriority', 'o_orderkey', '', 'o_totalprice'],
            aggregated=[('o_orderpriority', ''), ('o_orderkey', 'Count'),
                        ('', 'Count(*)'), ('o_totalprice', 'Sum')],
            names=['o_orderpriority', 'order_count', 'total', 'rev'],
        )
        self.assertEqual(
            'o_orderpriority, Count(o_orderkey) as order_count, Count(*) as total, Sum(o_totalprice) as rev',
            out,
        )


if __name__ == '__main__':
    unittest.main()

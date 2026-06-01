import copy

from ....src.core.abstract.un2_where_clause import UN2WhereClause


class FilterHolder(UN2WhereClause):
    def __init__(self, connectionHelper,
                 core_relations,
                 global_min_instance_dict, filter_extractor, name):
        # Phase 3/4: inherit the alias-aware state from the filter extractor so
        # downstream holders (equi_join, aoa) see the same per-alias witnesses
        # AND can resolve aliases back to base tables for SQL emission.
        alias_dict = getattr(filter_extractor, "global_alias_row_dict", None)
        instances = getattr(filter_extractor, "instances", None)
        alias_to_table = getattr(filter_extractor, "alias_to_table", None)
        super().__init__(connectionHelper, core_relations, global_min_instance_dict, name,
                         global_alias_row_dict=alias_dict,
                         instances=instances,
                         alias_to_table=alias_to_table)
        # MutationPipeLineBase deep-copied the alias dict; for FilterHolder we
        # need to SHARE it by reference with the filter_extractor so probes
        # routed through filter_extractor.extract_filter_on_attrib_set and
        # mutations routed through inherited mutate_dmin_with_val both see the
        # same (and only) live ctids. Without this, the two dicts diverge and
        # one path issues stale ctids that match zero rows on Postgres MVCC.
        if alias_dict is not None:
            self.global_alias_row_dict = filter_extractor.global_alias_row_dict
        self.filter_extractor = filter_extractor

        # method from filter object
        self._extract_filter_on_attrib_set = self.filter_extractor.extract_filter_on_attrib_set
        self.global_d_plus_value.update(self.filter_extractor.global_d_plus_value)
        self.global_attrib_max_length.update(self.filter_extractor.global_attrib_max_length)

        # get all methods from filter object
        self.mutate_global_min_instance_dict = self.filter_extractor.mutate_global_min_instance_dict
        self.restore_d_min_from_dict = self.filter_extractor.restore_d_min_from_dict
        self.insert_into_dmin_dict_values = self.filter_extractor.insert_into_dmin_dict_values
        self.get_datatype = self.filter_extractor.get_datatype
        self.get_dmin_val_of_attrib_list = self.filter_extractor.get_dmin_val_of_attrib_list

    def get_dmin_val(self, attrib: str, tab: str):
        return self.global_d_plus_value[attrib]  # short-cut works since tpch has all relations distinct attrib name

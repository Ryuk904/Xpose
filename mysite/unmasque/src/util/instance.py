"""Alias-aware data model for self-join / multi-instance table support.

An Instance represents one logical use of a base table in Qh's FROM clause.
For single-instance tables (the common case) alias == table, so all existing
code paths that key by bare table name remain correct. For multi-instance
tables (min_card[T] > 1), aliases are synthetic identifiers of the form
`{table}__a{i}` for i = 1..k.

Aliases in the reconstructed query do not need to match Qh's source aliases;
we are reconstructing a result-equivalent query, not a character-identical one.
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple


ALIAS_SEP = "__a"


@dataclass(frozen=True)
class Instance:
    table: str
    alias: str

    def qualified(self, attr: str) -> str:
        return f"{self.alias}.{attr}"


def make_alias(table: str, idx: int) -> str:
    return f"{table}{ALIAS_SEP}{idx}"


def build_instances(core_relations: List[str],
                    min_card: Dict[str, int]) -> Tuple[List[Instance], Dict[str, str]]:
    """Materialize per-instance handles from base tables and floor cardinalities.

    Returns:
      instances: one Instance per logical use of each base table.
      alias_to_table: reverse map from alias name to base table name.
    """
    instances: List[Instance] = []
    alias_to_table: Dict[str, str] = {}
    for t in core_relations:
        k = max(1, int((min_card or {}).get(t, 1)))
        if k == 1:
            instances.append(Instance(table=t, alias=t))
            alias_to_table[t] = t
        else:
            for i in range(1, k + 1):
                alias = make_alias(t, i)
                instances.append(Instance(table=t, alias=alias))
                alias_to_table[alias] = t
    return instances, alias_to_table


def instances_of(table: str, instances: List[Instance]) -> List[Instance]:
    return [i for i in instances if i.table == table]


def is_synthetic_alias(alias: str, alias_to_table: Dict[str, str]) -> bool:
    """True iff the alias was synthesised for a multi-instance table
    (i.e. alias != its base table)."""
    return alias_to_table.get(alias, alias) != alias

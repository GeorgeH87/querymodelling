"""Model instances of a query (root and joins) and the resolution of field
references within nested scopes (subqueries see the instances of the outer
queries, inner instances shadow outer ones with the same key)."""
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import aliased
from sqlalchemy.sql.expression import FromClause

from .expressions import SqlFieldExpression
from .references import SqlModelInfo


@dataclass
class SqlInstance:
    """A model within a query. ``key`` is its ``as`` or its model name,
    ``nullable`` is set if it is on the outer side of an outer join."""

    key: str
    info: SqlModelInfo
    nullable: bool = False
    entity: Any = None

    def create_entity(self) -> Any:
        source = self.info.source
        if self.key == self.info.name:
            return source
        if isinstance(source, FromClause):
            return source.alias(self.key)
        return aliased(source, name=self.key)


Scopes = tuple[dict[str, SqlInstance], ...]


@dataclass
class SqlQueryConfig:
    """Shared configuration of the generated query models."""

    models: dict[str, SqlModelInfo]
    default_model: str
    max_joins: int
    max_depth: int
    result_name: str
    field_models: dict[str, type] = field(default_factory=dict)
    result_models: dict = field(default_factory=dict)


def resolve_instance(
    reference: SqlFieldExpression, scopes: Scopes
) -> SqlInstance:
    """Finds the instance of a field reference, innermost scope first."""
    key = reference.instance_key
    for scope in scopes:
        instance = scope.get(key)
        if instance is None:
            continue
        if instance.info.name != reference.model:
            raise ValueError(
                f"{key!r} is the model {instance.info.name!r}, "
                f"not {reference.model!r}"
            )
        return instance
    raise ValueError(
        f"model {key!r} is not part of the query, join it first")


def reference_label(
    reference: SqlFieldExpression, root_key: Optional[str]
) -> str:
    if reference.instance_key == root_key:
        return reference.field
    return f"{reference.instance_key}_{reference.field}"

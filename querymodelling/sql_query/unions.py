"""Discriminated unions of the expressions. The member is selected by the
keys of the object, which keeps validation fast and its errors small:

* plain JSON values are constants,
* objects with ``model`` and ``field`` are fields (discriminated by
  ``model``),
* objects with ``model`` but without ``field`` are subqueries,
* other objects are selected by their only key besides ``alias``.
"""
from typing import Annotated, Any, Optional, Sequence, Union

from pydantic import BaseModel, Discriminator, Tag

from .expressions import (
    SqlAliasReference, SqlFieldExpression, SqlFunctionExpression,
    SqlQueryExpression
)


LITERAL_TAG = ":constant"
FIELD_TAG = ":field"
QUERY_TAG = ":query"
REF_TAG = "ref"


def union_of(members: Sequence[Any]) -> Any:
    return members[0] if len(members) == 1 else Union[tuple(members)]


def expression_tag(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        if "model" in value:
            return FIELD_TAG if "field" in value else QUERY_TAG
        keys = [key for key in value if key != "alias"]
        return keys[0] if len(keys) == 1 else None
    if isinstance(value, SqlFieldExpression):
        return FIELD_TAG
    if isinstance(value, SqlQueryExpression):
        return QUERY_TAG
    if isinstance(value, SqlFunctionExpression):
        return value.__sql_function__.name
    if isinstance(value, SqlAliasReference):
        return REF_TAG
    if isinstance(value, (list, tuple, BaseModel)) or value is None:
        return None
    return LITERAL_TAG


def _field_union(members: Sequence[Any]) -> Any:
    if len(members) == 1:
        return members[0]
    return Annotated[Union[tuple(members)], Discriminator("model")]


def tagged_union(groups: dict[str, list]) -> Any:
    groups = {tag: members for tag, members in groups.items() if members}
    if len(groups) == 1:
        tag, members = next(iter(groups.items()))
        return _field_union(members) if tag == FIELD_TAG else union_of(
            members)
    expected = []
    if LITERAL_TAG in groups:
        expected.append("a constant")
    if FIELD_TAG in groups:
        expected.append('a field {"model": ..., "field": ...}')
    if QUERY_TAG in groups:
        expected.append('a subquery {"model": ..., "fields": [...]}')
    keys = [tag for tag in groups if not tag.startswith(":")]
    if keys:
        expected.append(
            "an object with exactly one of the keys " + ", ".join(keys)
            + " (and an optional alias)"
        )
    return Annotated[
        Union[tuple(
            Annotated[
                _field_union(members) if tag == FIELD_TAG
                else union_of(members),
                Tag(tag),
            ]
            for tag, members in groups.items()
        )],
        Discriminator(
            expression_tag,
            custom_error_type="invalid_expression",
            custom_error_message="Expected " + " or ".join(expected),
        ),
    ]

"""Access rules of models and fields.

Without rules everything is allowed. Rules are fixed values or callables
receiving the access context - any object of the application, e.g. the
user, ``None`` if no context is given (``(context) -> flags | bool``):

* fields: ``Annotated[str, SqlFieldAccess(rule)]`` or
  ``SqlModelAccess(fields={"name": rule})`` with :class:`FieldUsage` flags,
* models: ``__sql_access__ = SqlModelAccess(usage=rule, rows=...)`` on the
  model class or ``SqlQueryModel(..., access={"name": SqlModelAccess(...)})``
  with :class:`ModelUsage` flags. ``rows`` (``(context, entity) ->
  condition``) restricts the rows of the model wherever it is used (queried
  model, joins and subqueries).

Both are generic in the type of the context (``None`` if not given),
``SqlModelAccess`` also in the model (``typing.Self`` within the class
body)::

    class Book(SQLModel, table=True):
        __sql_access__ = SqlModelAccess[User, Self](
            rows=lambda user, book: book.tenant_id == user.tenant_id)
        cost: Annotated[float, SqlFieldAccess[User](
            lambda user: user.is_admin)]

Fields and models without any access are removed from the query model (and
its JSON schema), partial access is checked by the validation.
"""
from dataclasses import dataclass, field
from enum import Flag
from types import NoneType
from typing import Any, Callable, Generic, Mapping, Optional, Union

from pydantic.fields import FieldInfo
from typing_extensions import TypeVar


C = TypeVar("C", default=NoneType)
"""Type of the access context, ``None`` if not given."""
M = TypeVar("M", default=Any)
"""Type of the model (``Any`` for tables)."""
F = TypeVar("F", bound=Flag)


class FieldUsage(Flag):
    """What may be done with a field."""

    NONE = 0
    SELECT = 1
    """Within ``fields`` (outside of aggregate functions)."""
    FILTER = 2
    """Within ``where`` and ``on`` (also reveals the value indirectly)."""
    GROUP = 4
    """Within ``group_by``."""
    AGGREGATE = 8
    """Within aggregate functions (``min`` / ``max`` reveal values)."""
    ALL = 15


class ModelUsage(Flag):
    """Where a model may be used."""

    NONE = 0
    QUERY = 1
    """As queried model of the query."""
    JOIN = 2
    """Within ``joins``."""
    SUBQUERY = 4
    """As queried model of subqueries."""
    ALL = 7


VISIBLE = FieldUsage.ALL
INVISIBLE = FieldUsage.NONE


class SqlAccessDenied(PermissionError):
    """The query model must not be used at all within the context."""


Rule = Union[bool, F, Callable[[C], Union[bool, F]], None]
"""``Rule[FieldUsage, User]``: a fixed value or a callable receiving the
access context."""
RowsRule = Callable[[C, type[M]], Any]
"""``RowsRule[User, Book]``: ``(context, entity) -> condition``, ``entity``
is the (aliased) model or table."""


@dataclass(frozen=True)
class SqlFieldAccess(Generic[C]):
    """``Annotated`` marker of the access rule of a field."""

    rule: Rule[FieldUsage, C]


@dataclass(frozen=True)
class SqlModelAccess(Generic[C, M]):
    """Access rules of a model, generic in the type of the access context
    and the model (``SqlModelAccess[User, Self]`` within the class body).

    :param usage: :class:`ModelUsage` (or bool / callable) of the model.
    :param rows: ``(context, entity) -> condition`` restricting the rows of
        the model, ``entity`` is the (aliased) model or table.
    :param fields: :class:`FieldUsage` rules of the fields by their name.
    """

    usage: Rule[ModelUsage, C] = None
    rows: Optional[RowsRule[C, M]] = None
    fields: Mapping[str, Rule[FieldUsage, C]] = field(default_factory=dict)

    def merge(
        self, other: Optional["SqlModelAccess[C, M]"]
    ) -> "SqlModelAccess[C, M]":
        """Rules of ``other`` take precedence."""
        if other is None:
            return self
        return SqlModelAccess(
            usage=self.usage if other.usage is None else other.usage,
            rows=self.rows if other.rows is None else other.rows,
            fields={**self.fields, **other.fields},
        )


def resolve_rule(
    rule: Rule[F, C], flags: type[F], context: C, target: str = "rule"
) -> F:
    """Evaluates a rule for the context."""
    if callable(rule) and not isinstance(rule, Flag):
        rule = rule(context)
    if rule is None or rule is True:
        return flags.ALL
    if rule is False:
        return flags.NONE
    if isinstance(rule, flags):
        return rule
    raise TypeError(
        f"invalid access rule {rule!r} of {target}, expected bool or "
        f"{flags.__name__}"
    )


def declared_model_access(
    model: type, override: Optional[SqlModelAccess[Any, Any]] = None
) -> SqlModelAccess[Any, Any]:
    declared = getattr(model, "__sql_access__", None) or SqlModelAccess()
    if not isinstance(declared, SqlModelAccess):
        raise TypeError(
            f"{model.__name__}.__sql_access__ must be a SqlModelAccess")
    return declared.merge(override)


def declared_field_rule(field_info: FieldInfo) -> Rule[FieldUsage, Any]:
    """The rule of an ``Annotated`` field (``SqlFieldAccess`` or an
    :class:`FieldUsage` flag), ``None`` if not declared."""
    for meta in field_info.metadata:
        if isinstance(meta, SqlFieldAccess):
            return meta.rule
        if isinstance(meta, FieldUsage):
            return meta
    return None

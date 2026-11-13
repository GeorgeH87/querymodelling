"""Result columns: type, nullability and field information.

The result of a query is known without executing it. Every expression yields
a :class:`SqlResultInfo`:

* fields copy all information of their source field (title, description,
  examples, ``json_schema_extra``, ``deprecated`` and constraints),
* constants are of their type and never ``NULL``,
* functions define their result: ``result_type``, ``nullable``
  (:class:`Nullability`) and ``result_field`` (e.g. :func:`inherit`),
* subqueries are the result of their single field, ``NULL`` if they may
  return no row.
"""
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence

from pydantic import Field
from pydantic.fields import FieldInfo

from ..sql_filter import unwrap_optional


RESULT_FIELD_ATTRIBUTES = (
    "title", "description", "examples", "json_schema_extra", "deprecated")
"""Attributes of a source field copied to its result column."""
DESCRIPTIVE_ATTRIBUTES = ("title", "description")
"""Attributes describing a value independent of its constraints."""


class Nullability(Enum):
    """When the result of a function may be ``NULL``."""

    PROPAGATE = "propagate"
    """If any argument may be ``NULL`` (SQL default)."""
    COALESCE = "coalesce"
    """If all arguments may be ``NULL``."""
    ALWAYS = "always"
    """Always, e.g. aggregates of empty groups or ``nullif``."""
    NEVER = "never"
    """Never, e.g. ``count`` or ``is_null``."""

    @classmethod
    def of(cls, value: Any) -> "Nullability":
        if isinstance(value, cls):
            return value
        return cls.ALWAYS if value else cls.NEVER


def _is_access_rule(meta: Any) -> bool:
    from .access import FieldUsage, SqlFieldAccess
    return isinstance(meta, (SqlFieldAccess, FieldUsage))


@dataclass(frozen=True)
class SqlResultField:
    """Information of a result column besides its type: field attributes
    and constraints (``metadata``)."""

    attributes: Mapping[str, Any] = field(default_factory=dict)
    metadata: tuple = ()

    @classmethod
    def from_field_info(
        cls, info: Optional[FieldInfo]
    ) -> "SqlResultField":
        if info is None:
            return cls()
        return cls(
            {
                name: getattr(info, name)
                for name in RESULT_FIELD_ATTRIBUTES
                if getattr(info, name, None) is not None
            },
            tuple(m for m in info.metadata if not _is_access_rule(m)),
        )

    def only(self, *attributes: str) -> "SqlResultField":
        """Only the given attributes, without constraints."""
        return SqlResultField({
            name: value for name, value in self.attributes.items()
            if name in attributes
        })

    def field_info(self, alias: Optional[str]) -> FieldInfo:
        info = Field(alias=alias, **self.attributes)
        info.metadata = list(self.metadata) + info.metadata
        return info


ResultFieldRule = Callable[[Sequence[SqlResultField]], SqlResultField]
"""Receives the result fields of the arguments of a function and returns
the result field of the function."""


def inherit(argument: int = 0, *attributes: str) -> ResultFieldRule:
    """Result field rule: the result field of an argument, all of it or only
    the given attributes (without constraints)."""
    def rule(fields: Sequence[SqlResultField]) -> SqlResultField:
        if argument >= len(fields):
            return SqlResultField()
        return fields[argument].only(*attributes) if attributes else (
            fields[argument])
    return rule


def describe(argument: int = 0) -> ResultFieldRule:
    """Result field rule: the descriptive attributes of an argument (for
    functions changing the value, e.g. ``lower`` or ``avg``)."""
    return inherit(argument, *DESCRIPTIVE_ATTRIBUTES)


@dataclass(frozen=True)
class SqlResultInfo:
    """Type (without ``None``), nullability and field of a result."""

    annotation: Any
    nullable: bool
    field: SqlResultField = field(default_factory=SqlResultField)

    @classmethod
    def of_annotation(
        cls, annotation: Any, field: Optional[SqlResultField] = None,
        nullable: bool = False,
    ) -> "SqlResultInfo":
        base = unwrap_optional(annotation)
        return cls(
            base, nullable or base is not annotation,
            field or SqlResultField(),
        )

    def with_nullable(self, nullable: bool = True) -> "SqlResultInfo":
        return replace(self, nullable=self.nullable or nullable)

    @property
    def result_annotation(self) -> Any:
        return Optional[self.annotation] if self.nullable else (
            self.annotation)


def function_nullable(
    nullability: Nullability, arguments: Sequence[SqlResultInfo]
) -> bool:
    if nullability is Nullability.ALWAYS:
        return True
    if nullability is Nullability.NEVER:
        return False
    nullable = [argument.nullable for argument in arguments]
    if nullability is Nullability.COALESCE:
        return bool(nullable) and all(nullable)
    return any(nullable)

"""Building blocks of the typed expressions: argument placeholders, SQL
function definitions and the base classes of the generated expression
models.

Expressions follow one rule: a plain JSON value (string, number, boolean) is
a constant, an object is an expression identified by its keys:

* ``{"model": ..., "field": ...}`` - a field,
* ``{"model": ..., ...}`` without ``field`` - a subquery,
* ``{"ref": ...}`` - an alias of ``fields`` (``group_by`` only),
* ``{<function>: <arguments>}`` - a function.
"""
from dataclasses import dataclass
from typing import (
    Annotated, Any, Callable, ClassVar, Iterator, Optional, Union
)

from pydantic import BaseModel, ConfigDict, Field

from .results import Nullability, ResultFieldRule


RESERVED_KEYS = frozenset({
    "model", "as", "field", "fields", "joins", "where", "group_by", "alias",
    "ref",
})
LABEL_MAX_LENGTH = 63
Label = Annotated[
    str,
    Field(pattern=rf"^[A-Za-z_][A-Za-z0-9_]{{0,{LABEL_MAX_LENGTH - 1}}}$"),
]


class Expr:
    """Argument placeholder: an expression of one of the given types (of any
    known type if no type is given)."""

    __slots__ = ("types",)

    def __init__(self, *types: Any):
        self.types = types

    def __repr__(self):
        return f"{type(self).__name__}{self.types!r}"


class Many(Expr):
    """Argument placeholder: a list of expressions of the given types or a
    single expression, e.g. a subquery (``in``)."""


class Query:
    """Argument placeholder: a subquery with any fields (``exists``)."""

    __slots__ = ()

    def __repr__(self):
        return "Query()"


@dataclass(frozen=True)
class SqlExpressionFunction:
    """SQL function usable within expressions.

    :param name: JSON key of the function.
    :param result_type: Type of the result.
    :param arguments: :class:`Expr`, :class:`Many` or :class:`Query`
        placeholders or plain annotations.
    :param apply: Receives the compiled arguments (plain annotations are
        passed as validated value) and returns the SQL expression.
    :param min_args: Minimum number of arguments, defaults to all. Trailing
        arguments are optional if lower.
    :param variadic: The (single) argument may be repeated (JSON list).
    :param aggregate: Aggregate function (requires grouping).
    :param nullable: When the result may be ``NULL``, a
        :class:`Nullability` (``True`` / ``False`` for always / never),
        defaults to ``NULL`` if any argument may be ``NULL``.
    :param result_field: Field information of the result (title,
        description, constraints) from the field information of the
        arguments, e.g. :func:`~.results.inherit`. Without a rule the result
        has no field information.
    """

    name: str
    result_type: Any
    arguments: tuple
    apply: Callable[..., Any]
    min_args: Optional[int] = None
    variadic: bool = False
    aggregate: bool = False
    nullable: Union[bool, Nullability] = Nullability.PROPAGATE
    result_field: Optional[ResultFieldRule] = None
    description: Optional[str] = None

    @property
    def nullability(self) -> Nullability:
        return Nullability.of(self.nullable)

    @property
    def required_args(self) -> int:
        if self.min_args is not None:
            return self.min_args
        return 1 if self.variadic else len(self.arguments)


class SqlExpressionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def sql_children(self) -> Iterator[Any]:
        """The nested expressions (subqueries are not entered)."""
        return iter(())


class SqlFieldExpression(SqlExpressionBase):
    """``{"model": ..., "field": ..., "as": ..., "alias": ...}``"""

    @property
    def instance_key(self) -> str:
        return self.as_ or self.model


class SqlQueryExpression(SqlExpressionBase):
    """Base of the query and subquery models."""


class SqlAliasReference(SqlExpressionBase):
    """``{"ref": <alias of fields>}``"""

    ref: Label = Field(description="Alias of an entry of fields.")


class SqlFunctionExpression(SqlExpressionBase):
    """``{<function>: <arguments>, "alias": ...}``"""

    __sql_function__: ClassVar[SqlExpressionFunction]
    __sql_argument_field__: ClassVar[str]

    def sql_arguments(self) -> list[tuple[Any, Any]]:
        """Returns the ``(argument spec, value)`` pairs."""
        function = self.__sql_function__
        value = getattr(self, self.__sql_argument_field__)
        if function.variadic:
            return [(function.arguments[0], item) for item in value]
        if isinstance(value, tuple):
            return list(zip(function.arguments, value))
        return [(function.arguments[0], value)]

    def sql_children(self) -> Iterator[Any]:
        for spec, value in self.sql_arguments():
            if isinstance(spec, (Expr, Query)):
                if isinstance(value, list):
                    yield from value
                else:
                    yield value


def walk(value: Any) -> Iterator[Any]:
    """Yields the expression and all nested expressions. Subqueries are
    yielded but not entered."""
    yield value
    if isinstance(value, SqlExpressionBase):
        for child in value.sql_children():
            yield from walk(child)


def contains_aggregate(value: Any) -> bool:
    return any(
        isinstance(node, SqlFunctionExpression)
        and node.__sql_function__.aggregate
        for node in walk(value)
    )


def contains_alias(value: Any, nested_only: bool = False) -> bool:
    return any(
        getattr(node, "alias", None) is not None
        for index, node in enumerate(walk(value))
        if index or not nested_only
    )

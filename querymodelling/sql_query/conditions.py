"""Default conditions: functions returning ``bool`` (comparisons, pattern
matching, null checks, logical operators and ``exists``)."""
import operator as op
from datetime import date, datetime, time
from functools import cache
from typing import Any, Iterator

from sqlalchemy import and_, not_, or_
from sqlalchemy.sql.elements import BindParameter
from sqlalchemy.sql.selectable import ScalarSelect

from ..sql_filter import SCALAR_TYPES, is_type
from .expressions import Expr, Many, Query, SqlExpressionFunction
from .functions import NUMERIC_EXPRESSION_TYPES


ORDERED_CONDITION_TYPES: tuple[type, ...] = (
    *NUMERIC_EXPRESSION_TYPES, str, date, datetime, time)


def _values(value: Any) -> Any:
    if isinstance(value, list):
        return value
    if isinstance(value, ScalarSelect):
        return value.element
    return [value]


COMPARISONS = {
    "eq": (op.eq, "[a, b]: a = b"),
    "ne": (op.ne, "[a, b]: a <> b"),
    "gt": (op.gt, "[a, b]: a > b"),
    "gte": (op.ge, "[a, b]: a >= b"),
    "lt": (op.lt, "[a, b]: a < b"),
    "lte": (op.le, "[a, b]: a <= b"),
}


@cache
def comparison(name: str, annotation: Any) -> SqlExpressionFunction:
    if name == "between":
        return SqlExpressionFunction(
            "between", bool, (Expr(annotation),) * 3,
            lambda v, low, high: v.between(low, high),
            description="[value, low, high]: low <= value <= high")
    if name in ("in", "not_in"):
        method = "in_" if name == "in" else "not_in"
        return SqlExpressionFunction(
            name, bool, (Expr(annotation), Many(annotation)),
            lambda v, values: getattr(v, method)(_values(values)),
            description=(
                f"[value, values]: value {name.replace('_', ' ').upper()} "
                "(values), values is a list or a subquery"
            ))
    operator, description = COMPARISONS[name]
    return SqlExpressionFunction(
        name, bool, (Expr(annotation), Expr(annotation)), operator,
        description=description)


def _pattern(method: str, escape: bool):
    def apply(value, pattern):
        if (
            escape and isinstance(pattern, BindParameter)
            and isinstance(pattern.value, str)
        ):
            return getattr(value, method)(pattern.value, autoescape=True)
        return getattr(value, method)(pattern)
    return apply


def _matching(name: str, method: str, escape: bool, description: str):
    return SqlExpressionFunction(
        name, bool, (Expr(str), Expr(str)), _pattern(method, escape),
        description=description)


CONTAINS = _matching(
    "contains", "contains", True, "[value, text]: value contains text")
ICONTAINS = _matching(
    "icontains", "icontains", True,
    "[value, text]: value contains text (case insensitive)")
STARTSWITH = _matching(
    "startswith", "startswith", True, "[value, text]: value starts with text")
ISTARTSWITH = _matching(
    "istartswith", "istartswith", True,
    "[value, text]: value starts with text (case insensitive)")
ENDSWITH = _matching(
    "endswith", "endswith", True, "[value, text]: value ends with text")
IENDSWITH = _matching(
    "iendswith", "iendswith", True,
    "[value, text]: value ends with text (case insensitive)")
LIKE = _matching(
    "like", "like", False, "[value, pattern]: SQL LIKE pattern")
NOT_LIKE = _matching(
    "not_like", "not_like", False, "[value, pattern]: SQL NOT LIKE pattern")
ILIKE = _matching(
    "ilike", "ilike", False,
    "[value, pattern]: SQL LIKE pattern (case insensitive)")

IS_NULL = SqlExpressionFunction(
    "is_null", bool, (Expr(),), lambda v: v.is_(None), nullable=False,
    description="The value is NULL.")
IS_NOT_NULL = SqlExpressionFunction(
    "is_not_null", bool, (Expr(),), lambda v: v.is_not(None),
    nullable=False, description="The value is not NULL.")

AND = SqlExpressionFunction(
    "and", bool, (Expr(bool),), lambda *v: and_(*v), variadic=True,
    min_args=1, description="All conditions are true.")
OR = SqlExpressionFunction(
    "or", bool, (Expr(bool),), lambda *v: or_(*v), variadic=True,
    min_args=1, description="At least one condition is true.")
NOT = SqlExpressionFunction(
    "not", bool, (Expr(bool),), not_, description="The condition is false.")
EXISTS = SqlExpressionFunction(
    "exists", bool, (Query(),), lambda query: query.exists(),
    nullable=False, description="The subquery returns at least one row.")


def comparison_conditions(annotation: Any) -> Iterator[SqlExpressionFunction]:
    if is_type(annotation, *SCALAR_TYPES):
        for name in ("eq", "ne", "in", "not_in"):
            yield comparison(name, annotation)
    if annotation in ORDERED_CONDITION_TYPES:
        for name in ("gt", "gte", "lt", "lte", "between"):
            yield comparison(name, annotation)


def string_conditions(annotation: Any) -> Iterator[SqlExpressionFunction]:
    if annotation is str:
        yield from (
            CONTAINS, ICONTAINS, STARTSWITH, ISTARTSWITH, ENDSWITH,
            IENDSWITH, LIKE, NOT_LIKE, ILIKE,
        )


def null_conditions(annotation: Any) -> Iterator[SqlExpressionFunction]:
    yield from (IS_NULL, IS_NOT_NULL)


def logical_conditions(annotation: Any) -> Iterator[SqlExpressionFunction]:
    yield from (AND, OR, NOT, EXISTS)


DEFAULT_CONDITION_GENERATORS = (
    comparison_conditions,
    string_conditions,
    null_conditions,
    logical_conditions,
)

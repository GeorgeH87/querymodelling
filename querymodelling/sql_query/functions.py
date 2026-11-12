"""Default SQL functions of the expressions and their generators."""
from datetime import date, datetime, time
from decimal import Decimal
from functools import cache
from typing import Any, Callable, Iterable, Literal

from sqlalchemy import (
    Boolean, Date, DateTime, Float, Integer, Numeric, String, Time, cast,
    distinct, extract, func
)
from sqlalchemy.sql.elements import BindParameter

from ..sql_filter import SCALAR_TYPES, is_type
from .expressions import Expr, SqlExpressionFunction


ExpressionFunctionGenerator = Callable[[Any], Iterable[SqlExpressionFunction]]


def _function(*args, **kwargs) -> SqlExpressionFunction:
    return SqlExpressionFunction(*args, **kwargs)


NUMERIC_EXPRESSION_TYPES: tuple[type, ...] = (int, float, Decimal)
ORDERED_EXPRESSION_TYPES: tuple[type, ...] = (
    *NUMERIC_EXPRESSION_TYPES, str, date, datetime, time)

LOWER = _function(
    "lower", str, (Expr(str),), lambda v: func.lower(v, type_=String()),
    description="SQL LOWER()")
UPPER = _function(
    "upper", str, (Expr(str),), lambda v: func.upper(v, type_=String()),
    description="SQL UPPER()")
TRIM = _function(
    "trim", str, (Expr(str),), lambda v: func.trim(v, type_=String()),
    description="SQL TRIM()")
LENGTH = _function(
    "length", int, (Expr(str),), lambda v: func.length(v, type_=Integer()),
    description="SQL LENGTH()")
CONCAT = _function(
    "concat", str, (Expr(str),), lambda *v: func.concat(*v),
    variadic=True, min_args=2, description="SQL CONCAT() of the values.")
SUBSTRING = _function(
    "substring", str, (Expr(str), Expr(int), Expr(int)),
    lambda v, *a: func.substr(v, *a, type_=String()),
    min_args=2,
    description="SQL SUBSTR(): [value, start (1-based), length (optional)]")
REPLACE = _function(
    "replace", str, (Expr(str), Expr(str), Expr(str)),
    lambda v, old, new: func.replace(v, old, new, type_=String()),
    description="SQL REPLACE(): [value, search, replacement]")

DATE = _function(
    "date", date, (Expr(datetime),), lambda v: func.date(v, type_=Date()),
    description="SQL DATE()")


def _extract(field: str, *types: type) -> SqlExpressionFunction:
    return _function(
        field, int, (Expr(*types),), lambda v: extract(field, v),
        description=f"SQL EXTRACT({field.upper()})")


YEAR, MONTH, DAY = (
    _extract(field, date, datetime) for field in ("year", "month", "day"))
HOUR, MINUTE = (
    _extract(field, datetime, time) for field in ("hour", "minute"))

COUNT = _function(
    "count", int, (Expr(),),
    lambda v: func.count() if isinstance(v, BindParameter) else func.count(v),
    aggregate=True, nullable=False,
    description=(
        "SQL COUNT() of the values which are not NULL, of all rows if the "
        "value is a constant (e.g. \"*\")."
    ))
COUNT_DISTINCT = _function(
    "count_distinct", int, (Expr(),), lambda v: func.count(distinct(v)),
    aggregate=True, nullable=False,
    description="SQL COUNT(DISTINCT)")

CAST_TYPES: dict[str, tuple[type, Any]] = {
    "string": (str, String),
    "integer": (int, Integer),
    "float": (float, Float),
    "decimal": (Decimal, Numeric),
    "boolean": (bool, Boolean),
    "date": (date, Date),
    "datetime": (datetime, DateTime),
    "time": (time, Time),
}


def _cast(name: str, result_type: type, sql_type: Any):
    return _function(
        "cast", result_type, (Expr(), Literal[name]),
        lambda v, _: cast(v, sql_type()),
        description=f"SQL CAST(): [value, \"{name}\"]")


CASTS = tuple(
    _cast(name, result_type, sql_type)
    for name, (result_type, sql_type) in CAST_TYPES.items()
)


@cache
def _typed(name: str, result_type: Any, *args, **kwargs):
    factories = {
        "abs": lambda: _function(
            "abs", result_type, (Expr(result_type),), func.abs,
            description="SQL ABS()"),
        "round": lambda: _function(
            "round", result_type, (Expr(result_type), Expr(int)),
            func.round, min_args=1,
            description="SQL ROUND(): value or [value, decimals]"),
        "ceil": lambda: _function(
            "ceil", result_type, (Expr(result_type),), func.ceil,
            description="SQL CEIL()"),
        "floor": lambda: _function(
            "floor", result_type, (Expr(result_type),), func.floor,
            description="SQL FLOOR()"),
        "add": lambda: _function(
            "add", result_type, (Expr(result_type), Expr(result_type)),
            lambda a, b: a + b, description="[a, b]: a + b"),
        "sub": lambda: _function(
            "sub", result_type, (Expr(result_type), Expr(result_type)),
            lambda a, b: a - b, description="[a, b]: a - b"),
        "mul": lambda: _function(
            "mul", result_type, (Expr(result_type), Expr(result_type)),
            lambda a, b: a * b, description="[a, b]: a * b"),
        "div": lambda: _function(
            "div", result_type, (Expr(result_type), Expr(result_type)),
            lambda a, b: a / b, description="[a, b]: a / b"),
        "sum": lambda: _function(
            "sum", result_type, (Expr(result_type),), func.sum,
            aggregate=True, description="SQL SUM()"),
        "avg": lambda: _function(
            "avg", result_type, (Expr(result_type),), func.avg,
            aggregate=True, description="SQL AVG()"),
        "min": lambda: _function(
            "min", result_type, (Expr(result_type),), func.min,
            aggregate=True, description="SQL MIN()"),
        "max": lambda: _function(
            "max", result_type, (Expr(result_type),), func.max,
            aggregate=True, description="SQL MAX()"),
        "coalesce": lambda: _function(
            "coalesce", result_type, (Expr(result_type),), func.coalesce,
            variadic=True, min_args=2,
            description="SQL COALESCE(): first value which is not NULL"),
        "nullif": lambda: _function(
            "nullif", result_type, (Expr(result_type), Expr(result_type)),
            func.nullif, description="SQL NULLIF(): [value, null value]"),
    }
    return factories[name]()


def string_expression_functions(annotation: Any):
    if annotation is str:
        yield from (LOWER, UPPER, TRIM, LENGTH, CONCAT, SUBSTRING, REPLACE)


def numeric_expression_functions(annotation: Any):
    if annotation in NUMERIC_EXPRESSION_TYPES:
        for name in ("abs", "round", "ceil", "floor", "add", "sub", "mul"):
            yield _typed(name, annotation)
        yield _typed("div", Decimal if annotation is Decimal else float)


def temporal_expression_functions(annotation: Any):
    if annotation is datetime:
        yield DATE
    if annotation in (date, datetime):
        yield from (YEAR, MONTH, DAY)
    if annotation in (datetime, time):
        yield from (HOUR, MINUTE)


def generic_expression_functions(annotation: Any):
    if is_type(annotation, *SCALAR_TYPES):
        yield _typed("coalesce", annotation)
        yield _typed("nullif", annotation)


def cast_expression_functions(annotation: Any):
    yield from CASTS


def aggregate_expression_functions(annotation: Any):
    yield from (COUNT, COUNT_DISTINCT)
    if annotation in NUMERIC_EXPRESSION_TYPES:
        yield _typed("sum", annotation)
        yield _typed("avg", Decimal if annotation is Decimal else float)
    if annotation in ORDERED_EXPRESSION_TYPES:
        yield _typed("min", annotation)
        yield _typed("max", annotation)


DEFAULT_VALUE_FUNCTION_GENERATORS: tuple[
    ExpressionFunctionGenerator, ...
] = (
    string_expression_functions,
    numeric_expression_functions,
    temporal_expression_functions,
    generic_expression_functions,
    cast_expression_functions,
    aggregate_expression_functions,
)

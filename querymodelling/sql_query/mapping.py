"""Registry of the expression function generators."""
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional, Sequence
from uuid import UUID

from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr

from ..sql_filter import SCALAR_TYPES, is_type
from .conditions import DEFAULT_CONDITION_GENERATORS
from .expressions import RESERVED_KEYS, Expr, SqlExpressionFunction
from .functions import (
    DEFAULT_VALUE_FUNCTION_GENERATORS, ExpressionFunctionGenerator
)


DEFAULT_EXPRESSION_FUNCTION_GENERATORS: tuple[
    ExpressionFunctionGenerator, ...
] = (*DEFAULT_VALUE_FUNCTION_GENERATORS, *DEFAULT_CONDITION_GENERATORS)
DEFAULT_TYPE_ORDER: tuple[type, ...] = (
    bool, int, float, Decimal, str, date, datetime, time, UUID)
DEFAULT_COMPATIBLE_TYPES: dict[type, tuple[type, ...]] = {
    float: (int,),
    Decimal: (int,),
}
DEFAULT_LITERAL_TYPES: dict[type, Any] = {
    bool: StrictBool,
    int: StrictInt,
    float: StrictFloat,
    str: StrictStr,
}


def is_scalar_type(annotation: Any) -> bool:
    return is_type(annotation, *SCALAR_TYPES)


def function_key(function: SqlExpressionFunction) -> tuple:
    """Identifies a function by its name, result and argument types."""
    return (
        function.name,
        function.result_type,
        tuple(
            (type(spec), spec.types) if isinstance(spec, Expr)
            else repr(spec)
            for spec in function.arguments
        ),
    )


class SqlExpressionMapping:
    """Registry of the expression function generators.

    :param function_generators: Generators yielding the SQL functions, later
        generators override functions with the same name and result type.
    :param compatible_types: Expressions of the listed types are accepted
        where an expression of the key type is expected.
    :param literal_types: Annotation of the constants of a type, defaults to
        the type itself (e.g. ``date`` accepts ``"2024-01-31"``).
    :param type_order: Narrower types first, used to resolve ambiguous
        expressions like ``{"add": [<int field>, 1]}``.
    :param is_expression_type: Decides whether the type of a field can be
        used within expressions.
    """

    def __init__(
        self,
        function_generators: Sequence[ExpressionFunctionGenerator] = (),
        compatible_types: Optional[dict[type, tuple[type, ...]]] = None,
        literal_types: Optional[dict[type, Any]] = None,
        type_order: Sequence[type] = DEFAULT_TYPE_ORDER,
        is_expression_type: Callable[[Any], bool] = is_scalar_type,
    ):
        self.function_generators = list(function_generators)
        self.compatible_types = dict(compatible_types or {})
        self.literal_types = dict(literal_types or {})
        self.type_order = tuple(type_order)
        self.is_expression_type = is_expression_type

    def copy(self) -> "SqlExpressionMapping":
        return SqlExpressionMapping(
            self.function_generators,
            self.compatible_types,
            self.literal_types,
            self.type_order,
            self.is_expression_type,
        )

    def add_functions(
        self, *generators: ExpressionFunctionGenerator
    ) -> "SqlExpressionMapping":
        self.function_generators.extend(generators)
        return self

    def literal_type(self, annotation: Any) -> Any:
        return self.literal_types.get(annotation, annotation)

    def resolve(
        self, annotations: Iterable[Any]
    ) -> tuple[list[Any], dict[tuple, SqlExpressionFunction]]:
        """Returns all reachable types (sorted narrow first, ``bool`` is
        always included) and their functions keyed by
        :func:`function_key`."""
        types, queue = [], [bool, *annotations]
        while queue:
            annotation = queue.pop(0)
            if annotation in types:
                continue
            types.append(annotation)
            for generator in self.function_generators:
                for function in generator(annotation):
                    queue.append(function.result_type)
                    for spec in function.arguments:
                        if isinstance(spec, Expr):
                            queue.extend(spec.types)

        order = {t: index for index, t in enumerate(self.type_order)}
        types = [
            t for _, t in sorted(
                enumerate(types),
                key=lambda item: (order.get(item[1], len(order)), item[0]),
            )
        ]

        functions = {}
        for generator in self.function_generators:
            for annotation in types:
                for function in generator(annotation):
                    if function.name in RESERVED_KEYS:
                        raise ValueError(
                            f"function name {function.name!r} is reserved")
                    functions[function_key(function)] = function
        return types, functions


def default_sql_expression_mapping() -> SqlExpressionMapping:
    """Creates a new mapping with the default generators."""
    return SqlExpressionMapping(
        DEFAULT_EXPRESSION_FUNCTION_GENERATORS,
        DEFAULT_COMPATIBLE_TYPES,
        DEFAULT_LITERAL_TYPES,
    )


DEFAULT_SQL_EXPRESSION_MAPPING = default_sql_expression_mapping()

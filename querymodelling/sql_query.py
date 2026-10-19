"""Typed SQL query models with selectable fields, SQL functions, a where
clause and grouping.

Generates a pydantic model from a base model describing a fully typed query::

    {
        "fields": [
            "name",
            {"field": "id", "alias": "my_id"},
            {"lower": "name", "alias": "name_lower"},
            {"count": "id", "alias": "count_id"},
            {"concat": ["name", {"value": " - "}, "description"]}
        ],
        "where": {"name": {"contains": "wurst"}},
        "group_by": ["name", "id", "description"]
    }

Each entry of ``fields`` is either the name of a field or an object holding
exactly one function (or ``field``) and an optional ``alias``.

Expressions are typed per result type: a ``str`` expression is a ``str``
field name, ``{"field": <name>}``, ``{"value": <str>}`` or a function
returning ``str``. Function arguments are typed expressions as well, so the
JSON schema and the validation reject e.g. ``{"lower": "amount"}``. JSON
numbers and booleans are literals, strings are always field names - other
literals are written as ``{"value": ...}``.

Everything is assembled from generators:

* expression function generators ``(annotation) -> Iterable[
  SqlExpressionFunction]`` are called for every known type and yield SQL
  functions (``lower``, ``count``, ``add``, ``cast`` ...),
* reference generators ``(context, model, name, field_info, annotation) ->
  Iterable[SqlFieldReference]`` yield the referenceable fields.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from functools import cache
from typing import (
    Annotated, Any, Callable, ClassVar, Iterable, Iterator, Literal, Mapping,
    NamedTuple, Optional, Sequence, Union
)
from uuid import UUID

from pydantic import (
    BaseModel, ConfigDict, Discriminator, Field, StrictBool, StrictFloat,
    StrictInt, Tag, create_model, model_validator
)
from pydantic.fields import FieldInfo
from sqlalchemy import (
    Boolean, Date, DateTime, Float, Integer, Numeric, String, Time, cast,
    distinct, extract, func, literal, select
)
from sqlalchemy.sql import Select
from typing_extensions import TypeAliasType

from .sql_filter import (
    SCALAR_TYPES, SqlFilter, SqlFilterMapping, SqlFilterModel, _alias,
    _python_name, _type_name, is_type, resolve_expression, unwrap_optional
)


ExpressionFunctionGenerator = Callable[
    [Any], Iterable["SqlExpressionFunction"]]
ReferenceGenerator = Callable[
    ["SqlQueryContext", type, str, FieldInfo, Any],
    Iterable["SqlFieldReference"]
]

RESERVED_KEYS = frozenset({"field", "value", "alias"})
LABEL_MAX_LENGTH = 63
Label = Annotated[
    str,
    Field(
        pattern=rf"^[A-Za-z_][A-Za-z0-9_]{{0,{LABEL_MAX_LENGTH - 1}}}$",
        description="Name of the result column.",
    ),
]


class Expr:
    """Argument placeholder of :class:`SqlExpressionFunction`: an expression
    of one of the given types (of any known type if no type is given).
    ``also`` adds further plain annotations, e.g. ``Literal["*"]``."""

    __slots__ = ("types", "also")

    def __init__(self, *types: Any, also: Any = ()):
        self.types = types
        self.also = also if isinstance(also, tuple) else (also,)

    def __repr__(self):
        return f"Expr{self.types!r}"


@dataclass(frozen=True)
class SqlExpressionFunction:
    """SQL function usable as field expression.

    :param name: JSON key of the function.
    :param result_type: Type of the result.
    :param arguments: :class:`Expr` placeholders or plain annotations.
    :param apply: Receives the compiled arguments (plain annotations are
        passed as validated value) and returns the SQL expression.
    :param min_args: Minimum number of arguments, defaults to all. Trailing
        arguments are optional if lower.
    :param variadic: The (single) argument may be repeated (JSON list).
    :param aggregate: Aggregate function (requires grouping).
    :param nullable: The result may be ``NULL``.
    """

    name: str
    result_type: Any
    arguments: tuple
    apply: Callable[..., Any]
    min_args: Optional[int] = None
    variadic: bool = False
    aggregate: bool = False
    nullable: bool = True
    description: Optional[str] = None

    @property
    def required_args(self) -> int:
        if self.min_args is not None:
            return self.min_args
        return 1 if self.variadic else len(self.arguments)


@dataclass(frozen=True)
class SqlFieldReference:
    """Referenceable field. ``annotation`` is the expression type (``None``
    if the field can only be selected), ``column`` an attribute name on the
    source, a callable receiving the source or a column expression."""

    name: str
    annotation: Any
    column: Any
    result_annotation: Any = None

    def resolve(self, source: Any) -> Any:
        return resolve_expression(self.column, source)


class SqlQueryColumn(NamedTuple):
    label: str
    annotation: Any
    expression: Any


class SqlExpressionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SqlFieldExpression(SqlExpressionBase):
    """``{"field": <name>, "alias": ...}``"""


class SqlValueExpression(SqlExpressionBase):
    """``{"value": <literal>}``"""


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


def _expression_arguments(value: Any) -> Iterator[Any]:
    if isinstance(value, SqlFunctionExpression):
        for spec, argument in value.sql_arguments():
            if isinstance(spec, Expr):
                yield argument


def _contains_aggregate(value: Any) -> bool:
    if isinstance(value, SqlFunctionExpression):
        if value.__sql_function__.aggregate:
            return True
    return any(map(_contains_aggregate, _expression_arguments(value)))


def _has_nested_alias(value: Any) -> bool:
    return any(
        getattr(argument, "alias", None) is not None
        or _has_nested_alias(argument)
        for argument in _expression_arguments(value)
    )


class SqlQuery(BaseModel):
    """Base of the generated query models."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    __sql_query_source__: ClassVar[Any] = None
    __sql_query_references__: ClassVar[dict[str, SqlFieldReference]] = {}
    __sql_query_result_name__: ClassVar[str] = "SqlQueryResult"
    __sql_query_result_models__: ClassVar[dict] = {}

    def _source(self, source: Any) -> Any:
        return self.__sql_query_source__ if source is None else source

    def selected(self) -> list[Any]:
        """The entries of ``fields`` (all fields if not set)."""
        if self.fields is None:
            return list(self.__sql_query_references__)
        return list(self.fields)

    def _reference_name(self, value: Any) -> Optional[str]:
        if isinstance(value, SqlFieldExpression):
            return value.field
        if isinstance(value, str) and value in self.__sql_query_references__:
            return value
        return None

    def _key(self, value: Any) -> Any:
        if (name := self._reference_name(value)) is not None:
            return ("field", name)
        if isinstance(value, SqlFunctionExpression):
            return (
                value.__sql_function__.name,
                tuple(
                    self._key(argument) if isinstance(spec, Expr)
                    else ("literal", repr(argument))
                    for spec, argument in value.sql_arguments()
                ),
            )
        if isinstance(value, SqlValueExpression):
            return ("value", repr(value.value))
        return ("literal", repr(value))

    def _auto_label(self, value: Any) -> str:
        if (name := self._reference_name(value)) is not None:
            return name
        if isinstance(value, SqlFunctionExpression):
            parts = [value.__sql_function__.name] + [
                label for argument in _expression_arguments(value)
                if (label := self._auto_label(argument))
            ]
            return "_".join(parts)
        return ""

    def labels(self) -> list[str]:
        """The labels of the result columns. Generated labels are made
        unique, explicit aliases have to be unique."""
        selected = self.selected()
        explicit = [getattr(item, "alias", None) for item in selected]
        taken = set()
        for alias in explicit:
            if alias is None:
                continue
            if alias in taken:
                raise ValueError(f"duplicate alias {alias!r}")
            taken.add(alias)
        labels = []
        for item, alias in zip(selected, explicit):
            if alias is None:
                base = re.sub(r"\W", "_", self._auto_label(item)) or "column"
                base = base[:LABEL_MAX_LENGTH]
                alias, index = base, 2
                while alias in taken:
                    suffix = f"_{index}"
                    alias = base[:LABEL_MAX_LENGTH - len(suffix)] + suffix
                    index += 1
                taken.add(alias)
            labels.append(alias)
        return labels

    def _check_grouped(self, value: Any, grouped: set, aggregated: bool):
        if isinstance(value, SqlFunctionExpression):
            function = value.__sql_function__
            if aggregated and function.aggregate:
                raise ValueError(
                    f"aggregate function {function.name!r} must not be "
                    "nested within an aggregate function"
                )
            if not aggregated and self._key(value) in grouped:
                return
            for argument in _expression_arguments(value):
                self._check_grouped(
                    argument, grouped, aggregated or function.aggregate)
        elif not aggregated:
            name = self._reference_name(value)
            if name is not None and self._key(value) not in grouped:
                raise ValueError(
                    f"field {name!r} must be part of group_by or be used "
                    "within an aggregate function"
                )

    @model_validator(mode="after")
    def _validate_sql_query(self):
        selected = self.selected()
        for item in selected:
            if _has_nested_alias(item):
                raise ValueError(
                    "alias is only allowed on the entries of fields")
        for item in self.group_by or ():
            if getattr(item, "alias", None) is not None:
                raise ValueError("alias is not allowed within group_by")
            if _contains_aggregate(item):
                raise ValueError(
                    "aggregate functions are not allowed within group_by")
        self.labels()
        if self.group_by or any(map(_contains_aggregate, selected)):
            grouped = {self._key(item) for item in self.group_by or ()}
            for item in selected:
                self._check_grouped(item, grouped, False)
        return self

    def compile_expression(self, value: Any, source: Any = None) -> Any:
        """Compiles an expression into a SQL expression."""
        source = self._source(source)
        if (name := self._reference_name(value)) is not None:
            return self.__sql_query_references__[name].resolve(source)
        if isinstance(value, SqlFunctionExpression):
            return value.__sql_function__.apply(*(
                self.compile_expression(argument, source)
                if isinstance(spec, Expr) else argument
                for spec, argument in value.sql_arguments()
            ))
        if isinstance(value, SqlValueExpression):
            return literal(value.value)
        if isinstance(value, str):
            return value
        return literal(value)

    def _annotation(self, value: Any) -> Any:
        if (name := self._reference_name(value)) is not None:
            return self.__sql_query_references__[name].result_annotation
        function = value.__sql_function__
        if function.nullable:
            return Optional[function.result_type]
        return function.result_type

    def columns(self, source: Any = None) -> list[SqlQueryColumn]:
        """The result columns: label, annotation and SQL expression."""
        source = self._source(source)
        return [
            SqlQueryColumn(
                label,
                self._annotation(item),
                self.compile_expression(item, source),
            )
            for item, label in zip(self.selected(), self.labels())
        ]

    def to_select(self, source: Any = None) -> Select:
        """Builds the SQLAlchemy select statement."""
        source = self._source(source)
        statement = select(*(
            column.expression.label(column.label)
            for column in self.columns(source)
        ))
        if not isinstance(source, Mapping):
            statement = statement.select_from(source)
        if self.where is not None:
            statement = self.where.apply_sql(statement, source)
        if self.group_by:
            statement = statement.group_by(*(
                self.compile_expression(item, source)
                for item in self.group_by
            ))
        return statement

    def result_model(self) -> type[BaseModel]:
        """Typed model of a result row (cached per column signature)."""
        signature = tuple(
            (label, self._annotation(item))
            for item, label in zip(self.selected(), self.labels())
        )
        models = self.__sql_query_result_models__
        if signature not in models:
            fields, used = {}, set()
            for label, annotation in signature:
                python_name = _python_name(label, used)
                fields[python_name] = (
                    annotation, Field(alias=_alias(python_name, label)))
            models[signature] = create_model(
                self.__sql_query_result_name__,
                __config__=ConfigDict(populate_by_name=True),
                **fields,
            )
        return models[signature]

    def fetch(self, session: Any, source: Any = None) -> list[BaseModel]:
        """Executes the query and returns typed result rows."""
        model = self.result_model()
        result = session.execute(self.to_select(source))
        return [model.model_validate(dict(row._mapping)) for row in result]


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
    "count", int, (Expr(also=Literal["*"]),),
    lambda v: func.count() if isinstance(v, str) else func.count(v),
    aggregate=True, nullable=False,
    description="SQL COUNT() of the value or of all rows (\"*\").")
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


DEFAULT_EXPRESSION_FUNCTION_GENERATORS: tuple[
    ExpressionFunctionGenerator, ...
] = (
    string_expression_functions,
    numeric_expression_functions,
    temporal_expression_functions,
    generic_expression_functions,
    cast_expression_functions,
    aggregate_expression_functions,
)
DEFAULT_TYPE_ORDER: tuple[type, ...] = (
    bool, int, float, Decimal, str, date, datetime, time, UUID)
DEFAULT_COMPATIBLE_TYPES: dict[type, tuple[type, ...]] = {
    float: (int,),
    Decimal: (int,),
}
DEFAULT_JSON_LITERALS: dict[type, Any] = {
    bool: StrictBool,
    int: StrictInt,
    float: StrictFloat,
}


def is_scalar_type(annotation: Any) -> bool:
    return is_type(annotation, *SCALAR_TYPES)


class SqlExpressionMapping:
    """Registry of the expression function generators.

    :param function_generators: Generators yielding the SQL functions, later
        generators override functions with the same name and result type.
    :param compatible_types: Expressions of the listed types are accepted
        where an expression of the key type is expected.
    :param json_literals: Types which can be written as plain JSON literal
        (all other literals are written as ``{"value": ...}``).
    :param type_order: Narrower types first, used to resolve ambiguous
        expressions like ``{"add": ["amount", 1]}``.
    :param is_expression_type: Decides whether the type of a field can be
        used within expressions.
    """

    def __init__(
        self,
        function_generators: Sequence[ExpressionFunctionGenerator] = (),
        compatible_types: Optional[dict[type, tuple[type, ...]]] = None,
        json_literals: Optional[dict[type, Any]] = None,
        type_order: Sequence[type] = DEFAULT_TYPE_ORDER,
        is_expression_type: Callable[[Any], bool] = is_scalar_type,
    ):
        self.function_generators = list(function_generators)
        self.compatible_types = dict(compatible_types or {})
        self.json_literals = dict(json_literals or {})
        self.type_order = tuple(type_order)
        self.is_expression_type = is_expression_type

    def copy(self) -> "SqlExpressionMapping":
        return SqlExpressionMapping(
            self.function_generators,
            self.compatible_types,
            self.json_literals,
            self.type_order,
            self.is_expression_type,
        )

    def add_functions(
        self, *generators: ExpressionFunctionGenerator
    ) -> "SqlExpressionMapping":
        self.function_generators.extend(generators)
        return self

    def resolve(
        self, annotations: Iterable[Any]
    ) -> tuple[list[Any], dict[tuple[str, Any], SqlExpressionFunction]]:
        """Returns all reachable types (sorted narrow first) and their
        functions keyed by ``(name, result type)``."""
        types, queue = [], list(annotations)
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
                    functions[(function.name, function.result_type)] = (
                        function)
        return types, functions


def default_sql_expression_mapping() -> SqlExpressionMapping:
    """Creates a new mapping with the default generators."""
    return SqlExpressionMapping(
        DEFAULT_EXPRESSION_FUNCTION_GENERATORS,
        DEFAULT_COMPATIBLE_TYPES,
        DEFAULT_JSON_LITERALS,
    )


DEFAULT_SQL_EXPRESSION_MAPPING = default_sql_expression_mapping()


@dataclass(frozen=True)
class SqlQueryContext:
    """Passed to the reference generators."""

    mapping: SqlExpressionMapping
    use_alias_reference: bool = True


def model_references(
    context: SqlQueryContext,
    model: type,
    name: str,
    field_info: FieldInfo,
    annotation: Any,
) -> Iterator[SqlFieldReference]:
    """Default reference generator: every field of the base model."""
    yield SqlFieldReference(
        name=(
            field_info.alias
            if context.use_alias_reference and field_info.alias else name
        ),
        annotation=(
            annotation if context.mapping.is_expression_type(annotation)
            else None
        ),
        column=name,
        result_annotation=field_info.annotation,
    )


DEFAULT_REFERENCE_GENERATORS: tuple[ReferenceGenerator, ...] = (
    model_references,)


def _camel(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(
        r"[^A-Za-z0-9]+", name))


def _union(members: Sequence[Any]) -> Any:
    return members[0] if len(members) == 1 else Union[tuple(members)]


REFERENCE_TAG = ":reference"
LITERAL_TAG = ":literal"


def _expression_tag(value: Any) -> Optional[str]:
    """Selects the union member of an expression by its function key, which
    keeps validation fast and its errors small."""
    if isinstance(value, str):
        return REFERENCE_TAG
    if isinstance(value, (bool, int, float, Decimal)):
        return LITERAL_TAG
    if isinstance(value, dict):
        keys = [key for key in value if key != "alias"]
        return keys[0] if len(keys) == 1 else None
    if isinstance(value, SqlFieldExpression):
        return "field"
    if isinstance(value, SqlValueExpression):
        return "value"
    if isinstance(value, SqlFunctionExpression):
        return value.__sql_function__.name
    return None


def _tagged_union(groups: dict[str, list]) -> Any:
    groups = {tag: members for tag, members in groups.items() if members}
    if len(groups) == 1:
        return _union(next(iter(groups.values())))
    expected = []
    if REFERENCE_TAG in groups:
        expected.append("a field name")
    if LITERAL_TAG in groups:
        expected.append("a number / boolean")
    keys = [tag for tag in groups if not tag.startswith(":")]
    expected.append(
        "an object with exactly one of the keys " + ", ".join(keys)
        + " (and an optional alias)"
    )
    return Annotated[
        Union[tuple(
            Annotated[_union(members), Tag(tag)]
            for tag, members in groups.items()
        )],
        Discriminator(
            _expression_tag,
            custom_error_type="invalid_expression",
            custom_error_message="Expected " + " or ".join(expected),
        ),
    ]


def SqlQueryModel(
    base_model: type[BaseModel],
    *,
    mapping: Optional[SqlExpressionMapping] = None,
    reference_generators: Sequence[ReferenceGenerator] = (
        DEFAULT_REFERENCE_GENERATORS),
    where_model: Optional[type[SqlFilter]] = None,
    filter_mapping: Optional[SqlFilterMapping] = None,
    exclude_fields: Optional[list[str]] = None,
    include_fields: Optional[list[str]] = None,
    source: Any = None,
    use_alias_reference: bool = True,
    model_name: Optional[str] = None,
) -> type[SqlQuery]:
    """Generates a typed SQL query model (``fields``, ``where`` and
    ``group_by``) based on the fields of the base model.

    :param base_model: The model to generate the query model from.
    :param mapping: Maps types to SQL functions, defaults to
        :data:`DEFAULT_SQL_EXPRESSION_MAPPING`.
    :param reference_generators: Generators yielding the referenceable
        fields per field of the base model.
    :param where_model: Filter model of ``where``, defaults to a generated
        :func:`~querymodelling.sql_filter.SqlFilterModel`.
    :param filter_mapping: Mapping of the generated filter model.
    :param exclude_fields: Fields of the base model to exclude.
    :param include_fields: Fields of the base model to include.
    :param source: The column source (SQLModel table model, table, alias or
        mapping of column names to columns), defaults to the base model.
    :param use_alias_reference: Reference fields by their alias.
    :param model_name: Name of the model (prefix of all generated models),
        defaults to ``<BaseModel>SqlQuery``.
    :return: The query model.
    """
    mapping = DEFAULT_SQL_EXPRESSION_MAPPING if mapping is None else mapping
    prefix = model_name or f"{base_model.__name__}SqlQuery"
    source = base_model if source is None else source
    context = SqlQueryContext(mapping, use_alias_reference)

    references: dict[str, SqlFieldReference] = {}
    for name, field_info in base_model.model_fields.items():
        if exclude_fields and name in exclude_fields:
            continue
        if include_fields and name not in include_fields:
            continue
        annotation = unwrap_optional(field_info.annotation)
        for generator in reference_generators:
            for reference in generator(
                context, base_model, name, field_info, annotation
            ):
                if reference.name in references:
                    raise ValueError(
                        f"duplicate field reference {reference.name!r}")
                if reference.result_annotation is None:
                    reference = SqlFieldReference(
                        reference.name, reference.annotation,
                        reference.column, Optional[reference.annotation],
                    )
                references[reference.name] = reference
    if not references:
        raise ValueError(f"{base_model.__name__} has no fields to query")

    types, functions = mapping.resolve(
        r.annotation for r in references.values()
        if r.annotation is not None
    )
    type_names: dict[Any, str] = {}
    for annotation in types:
        name, index = _type_name(annotation), 2
        while name in type_names.values():
            name, index = f"{_type_name(annotation)}{index}", index + 1
        type_names[annotation] = name

    def expression_name(annotation: Any) -> str:
        return f"{prefix}{type_names[annotation]}Expression"

    combined: dict[str, tuple] = {}

    def expressions_name(spec: Expr) -> str:
        if not spec.types:
            name = f"{prefix}AnyExpression"
        elif len(spec.types) == 1:
            return expression_name(spec.types[0])
        else:
            name = f"{prefix}{''.join(type_names[t] for t in spec.types)}"
            name += "Expression"
        combined[name] = spec.types
        return name

    def argument_annotation(spec: Any) -> Any:
        if not isinstance(spec, Expr):
            return spec
        return _union([expressions_name(spec), *spec.also])

    def arguments_annotation(function: SqlExpressionFunction) -> Any:
        arguments = [argument_annotation(a) for a in function.arguments]
        if function.variadic:
            return Annotated[
                list[arguments[0]],
                Field(min_length=function.required_args),
            ]
        options = []
        for count in range(function.required_args, len(arguments) + 1):
            if count == 1:
                options.append(arguments[0])
            elif count > 1:
                options.append(tuple[tuple(arguments[:count])])
        return _union(options)

    alias_field = (
        Optional[Label],
        Field(None, description="Name of the result column."),
    )
    created: list[type[BaseModel]] = []

    field_models: dict[Any, tuple[Any, type]] = {}
    for annotation in types:
        names = tuple(
            r.name for r in references.values() if r.annotation is annotation)
        if names:
            model = create_model(
                f"{prefix}{type_names[annotation]}FieldReference",
                __base__=SqlFieldExpression,
                __module__=__name__,
                field=(Literal[names], ...),
                alias=alias_field,
            )
            field_models[annotation] = (Literal[names], model)
            created.append(model)

    value_models = {}
    for annotation in types:
        value_models[annotation] = create_model(
            f"{prefix}{type_names[annotation]}Value",
            __base__=SqlValueExpression,
            __module__=__name__,
            value=(annotation, ...),
        )
        created.append(value_models[annotation])

    function_models: dict[Any, list[type]] = {t: [] for t in types}
    for function in functions.values():
        python_name = _python_name(function.name, {"alias"})
        model = create_model(
            f"{prefix}{_camel(function.name)}"
            f"{type_names[function.result_type]}Function",
            __base__=SqlFunctionExpression,
            __module__=__name__,
            **{
                python_name: (
                    arguments_annotation(function),
                    Field(
                        alias=_alias(python_name, function.name),
                        description=function.description,
                    ),
                ),
                "alias": alias_field,
            },
        )
        model.__sql_function__ = function
        model.__sql_argument_field__ = python_name
        function_models[function.result_type].append(model)
        created.append(model)

    def add(groups: dict[str, list], tag: str, member: Any):
        if member not in groups.setdefault(tag, []):
            groups[tag].append(member)

    def add_members(annotation: Any, groups: dict[str, list]):
        if annotation in field_models:
            names, model = field_models[annotation]
            add(groups, REFERENCE_TAG, names)
            add(groups, "field", model)
        if annotation in mapping.json_literals:
            add(groups, LITERAL_TAG, mapping.json_literals[annotation])
        add(groups, "value", value_models[annotation])
        for model in function_models[annotation]:
            add(groups, model.__sql_function__.name, model)

    reference_model = create_model(
        f"{prefix}FieldReference",
        __base__=SqlFieldExpression,
        __module__=__name__,
        field=(Literal[tuple(references)], ...),
        alias=alias_field,
    )
    created.append(reference_model)

    namespace: dict[str, Any] = {}
    for annotation in types:
        groups: dict[str, list] = {}
        add_members(annotation, groups)
        for compatible in mapping.compatible_types.get(annotation, ()):
            if compatible in type_names:
                add_members(compatible, groups)
        namespace[expression_name(annotation)] = TypeAliasType(
            expression_name(annotation), _tagged_union(groups))

    for name, combined_types in combined.items():
        groups = {}
        for annotation in combined_types or types:
            add_members(annotation, groups)
        if not combined_types:
            # any type: every field, also those without expression type
            groups[REFERENCE_TAG] = [Literal[tuple(references)]]
            groups["field"] = [reference_model]
        namespace[name] = TypeAliasType(name, _tagged_union(groups))

    column_groups: dict[str, list] = {
        REFERENCE_TAG: [Literal[tuple(references)]],
        "field": [reference_model],
    }
    for annotation in types:
        for model in function_models[annotation]:
            column_groups.setdefault(
                model.__sql_function__.name, []).append(model)
    column_type = TypeAliasType(
        f"{prefix}Column", _tagged_union(column_groups))

    for model in created:
        model.model_rebuild(force=True, _types_namespace=namespace)

    if where_model is None:
        where_model = SqlFilterModel(
            base_model,
            mapping=filter_mapping,
            exclude_fields=exclude_fields,
            include_fields=include_fields,
            source=source,
            use_alias_reference=use_alias_reference,
            model_name=f"{prefix}Where",
        )

    columns = Annotated[list[column_type], Field(min_length=1)]
    model = create_model(
        prefix,
        __base__=SqlQuery,
        __module__=base_model.__module__,
        fields=(
            Optional[columns],
            Field(
                None,
                description=(
                    "Result columns: field names or objects with one "
                    "function (or field) and an optional alias. All fields "
                    "if not set."
                ),
            ),
        ),
        where=(Optional[where_model], Field(None)),
        group_by=(
            Optional[columns],
            Field(None, description="Expressions to group by."),
        ),
    )
    model.__sql_query_source__ = source
    model.__sql_query_references__ = references
    model.__sql_query_result_name__ = f"{prefix}Result"
    model.__sql_query_result_models__ = {}
    return model


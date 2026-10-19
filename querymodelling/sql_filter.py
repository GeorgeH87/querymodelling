"""Typed, nested SQL filter models.

Generates a pydantic model from a base model whose instances describe a
nested, fully typed filter that is compiled into a SQLAlchemy where clause::

    {
        "and": {
            "name": {"contains": "wurst", "startswith": "bump"},
            "description": {"lower": {"contains": "test"}}
        }
    }

Everything is assembled from generators, which makes the module modular and
extensible:

* operator generators ``(annotation) -> Iterable[SqlOperator]`` yield the
  operators (``eq``, ``contains``, ``gt`` ...) available for a type,
* function generators ``(annotation) -> Iterable[SqlFunction]`` yield the SQL
  functions (``lower``, ``length``, ``year`` ...) applicable to a type,
* field generators ``(context, model, name, field_info, annotation) ->
  Iterable[SqlFilterField]`` yield the fields of the root filter model,
* logical generators ``(model) -> Iterable[SqlLogicalOperator]`` yield the
  logical operators (``and``, ``or``, ``not``) of the root filter model.

Compiling a filter is generator based as well: each entry yields its SQL
conditions via ``conditions(target, value)``.
"""
import keyword
import operator as op
import re
import types
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import (
    Any, Callable, ClassVar, Iterable, Iterator, Mapping, Optional, Sequence,
    TypeVar, Union, get_args, get_origin
)
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic.fields import FieldInfo
from sqlalchemy import (
    Date, Integer, String, and_, extract, func, not_, or_, true
)
from sqlalchemy.sql.expression import ColumnElement, FromClause


S = TypeVar("S")

OperatorGenerator = Callable[[Any], Iterable["SqlOperator"]]
FunctionGenerator = Callable[[Any], Iterable["SqlFunction"]]
FieldGenerator = Callable[
    ["SqlFilterContext", type, str, FieldInfo, Any],
    Iterable["SqlFilterField"]
]
LogicalGenerator = Callable[[type], Iterable["SqlLogicalOperator"]]


class SqlFilterBase(BaseModel):
    """Base of all generated filter models."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    __sql_filter_entries__: ClassVar[dict[str, Any]] = {}

    def _sql_conditions(self, target: Any) -> Iterator[ColumnElement[bool]]:
        for name, entry in self.__sql_filter_entries__.items():
            value = getattr(self, name)
            if value is not None:
                yield from entry.conditions(target, value)


class SqlFieldFilter(SqlFilterBase):
    """Base of the generated per type filter models (e.g. ``SqlStrFilter``).
    The target of the conditions is the column (expression) to filter."""


class SqlFilter(SqlFilterBase):
    """Base of the generated root filter models. The target of the
    conditions is the column source (model, table, alias, mapping)."""

    __sql_filter_source__: ClassVar[Any] = None

    def _source(self, source: Any) -> Any:
        return self.__sql_filter_source__ if source is None else source

    def to_sql_expression(self, source: Any = None) -> ColumnElement[bool]:
        """Compiles the filter into a single where clause. Returns ``true()``
        if the filter does not contain any condition.

        :param source: Overrides the column source (e.g. an aliased model).
        """
        expression = join_conditions(
            and_, self._sql_conditions(self._source(source)))
        return true() if expression is None else expression

    def apply_sql(self, statement: S, source: Any = None) -> S:
        """Adds the conditions of the filter to the statement."""
        conditions = list(self._sql_conditions(self._source(source)))
        if not conditions:
            return statement
        return statement.where(*conditions)


def sql_filter_expression(
    sql_filter: Optional[SqlFilter], source: Any = None
) -> ColumnElement[bool]:
    """Null safe variant of :meth:`SqlFilter.to_sql_expression`, e.g. usable
    as ``QueryField(sql_filter_expression)`` in existing query models."""
    if sql_filter is None:
        return true()
    return sql_filter.to_sql_expression(source)


def join_conditions(
    combine: Callable[..., ColumnElement[bool]],
    conditions: Iterable[Optional[ColumnElement[bool]]],
) -> Optional[ColumnElement[bool]]:
    conditions = [c for c in conditions if c is not None]
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return combine(*conditions)


def resolve_column(source: Any, name: str) -> Any:
    """Resolves a column by name on a model, table, alias or mapping."""
    if isinstance(source, Mapping):
        return source[name]
    if isinstance(source, FromClause):
        return source.c[name]
    try:
        return getattr(source, name)
    except AttributeError as error:
        raise ValueError(
            f"cannot resolve column {name!r} on {source!r}") from error


def resolve_expression(column: Any, source: Any) -> Any:
    """Resolves a column definition: an attribute name on the source, a
    callable receiving the source or a column expression."""
    if isinstance(column, str):
        return resolve_column(source, column)
    if hasattr(column, "__clause_element__") or isinstance(
            column, ColumnElement):
        return column
    if callable(column):
        return column(source)
    return column


@dataclass(frozen=True)
class SqlOperator:
    """Compares a column with a typed value, e.g. ``contains`` -> LIKE."""

    name: str
    apply: Callable[[Any, Any], ColumnElement[bool]]
    value_type: Callable[[Any], Any] = lambda annotation: annotation
    description: Optional[str] = None

    def conditions(self, column: Any, value: Any):
        yield self.apply(column, value)


@dataclass(frozen=True)
class SqlFunction:
    """Applies a SQL function to a column, the result is filtered by the
    filter model of ``result_type``, e.g. ``length`` -> ``SqlIntFilter``."""

    name: str
    apply: Callable[[Any], Any]
    result_type: Callable[[Any], Any] = lambda annotation: annotation
    description: Optional[str] = None

    def conditions(self, column: Any, value: SqlFieldFilter):
        yield from value._sql_conditions(self.apply(column))


@dataclass(frozen=True)
class SqlLogicalOperator:
    """Combines nested root filters, e.g. ``and`` / ``or`` / ``not``.

    ``annotation`` receives the (forward referenced) root model and returns
    the annotation of the field. ``apply`` receives the value and a function
    returning the conditions of a nested root filter."""

    name: str
    annotation: Callable[[Any], Any]
    apply: Callable[
        [Any, Callable[[SqlFilter], list[ColumnElement[bool]]]],
        Optional[ColumnElement[bool]]
    ]
    description: Optional[str] = None

    def conditions(self, source: Any, value: Any):
        expression = self.apply(
            value, lambda nested: list(nested._sql_conditions(source)))
        if expression is not None:
            yield expression


@dataclass(frozen=True)
class SqlFilterField:
    """Field of the root filter model. ``column`` is either the attribute
    name on the source, a callable receiving the source or a column
    expression. Subclass and override :meth:`conditions` for custom
    behaviour."""

    name: str
    annotation: type[SqlFieldFilter]
    column: Any
    alias: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    json_schema_extra: Optional[dict] = None

    def resolve(self, source: Any) -> Any:
        return resolve_expression(self.column, source)

    def conditions(self, source: Any, value: SqlFieldFilter):
        yield from value._sql_conditions(self.resolve(source))


def is_type(annotation: Any, *types_: type, exclude: tuple = ()) -> bool:
    """Safe ``issubclass`` check for annotations."""
    if not isinstance(annotation, type):
        return False
    if exclude and issubclass(annotation, exclude):
        return False
    return issubclass(annotation, types_)


def only(generator: Callable[[Any], Iterable[S]], *names: str):
    """Restricts a generator to the given operator / function names."""
    def wrapped(annotation):
        for item in generator(annotation):
            if item.name in names:
                yield item
    return wrapped


def without(generator: Callable[[Any], Iterable[S]], *names: str):
    """Removes the given operator / function names from a generator."""
    def wrapped(annotation):
        for item in generator(annotation):
            if item.name not in names:
                yield item
    return wrapped


def for_types(
    generator: Callable[[Any], Iterable[S]], *types_: type,
    exclude: tuple = ()
):
    """Restricts a generator to the given types."""
    def wrapped(annotation):
        if is_type(annotation, *types_, exclude=exclude):
            yield from generator(annotation)
    return wrapped


def items(*values: S) -> Callable[[Any], Iterator[S]]:
    """Creates a generator yielding the given operators / functions."""
    def generator(annotation):
        yield from values
    return generator


EQ = SqlOperator("eq", op.eq, description="Equal to the value.")
NE = SqlOperator("ne", op.ne, description="Not equal to the value.")
GT = SqlOperator("gt", op.gt, description="Greater than the value.")
GTE = SqlOperator(
    "gte", op.ge, description="Greater than or equal to the value.")
LT = SqlOperator("lt", op.lt, description="Less than the value.")
LTE = SqlOperator("lte", op.le, description="Less than or equal to the value.")
BETWEEN = SqlOperator(
    "between",
    lambda column, value: column.between(value[0], value[1]),
    value_type=lambda annotation: tuple[annotation, annotation],
    description="Between the two values (inclusive).",
)
IN = SqlOperator(
    "in",
    lambda column, value: column.in_(value),
    value_type=lambda annotation: list[annotation],
    description="Equal to one of the values.",
)
NOT_IN = SqlOperator(
    "not_in",
    lambda column, value: column.not_in(value),
    value_type=lambda annotation: list[annotation],
    description="Equal to none of the values.",
)
IS_NULL = SqlOperator(
    "is_null",
    lambda column, value: column.is_(None) if value else column.is_not(None),
    value_type=lambda annotation: bool,
    description="true: is NULL, false: is not NULL.",
)
CONTAINS = SqlOperator(
    "contains",
    lambda column, value: column.contains(value, autoescape=True),
    description="Contains the value.",
)
ICONTAINS = SqlOperator(
    "icontains",
    lambda column, value: column.icontains(value, autoescape=True),
    description="Contains the value (case insensitive).",
)
STARTSWITH = SqlOperator(
    "startswith",
    lambda column, value: column.startswith(value, autoescape=True),
    description="Starts with the value.",
)
ISTARTSWITH = SqlOperator(
    "istartswith",
    lambda column, value: column.istartswith(value, autoescape=True),
    description="Starts with the value (case insensitive).",
)
ENDSWITH = SqlOperator(
    "endswith",
    lambda column, value: column.endswith(value, autoescape=True),
    description="Ends with the value.",
)
IENDSWITH = SqlOperator(
    "iendswith",
    lambda column, value: column.iendswith(value, autoescape=True),
    description="Ends with the value (case insensitive).",
)
LIKE = SqlOperator(
    "like",
    lambda column, value: column.like(value),
    description="Matches the SQL LIKE pattern.",
)
NOT_LIKE = SqlOperator(
    "not_like",
    lambda column, value: column.not_like(value),
    description="Does not match the SQL LIKE pattern.",
)
ILIKE = SqlOperator(
    "ilike",
    lambda column, value: column.ilike(value),
    description="Matches the SQL LIKE pattern (case insensitive).",
)

LOWER = SqlFunction(
    "lower", lambda column: func.lower(column, type_=String()),
    description="SQL LOWER() of the value.")
UPPER = SqlFunction(
    "upper", lambda column: func.upper(column, type_=String()),
    description="SQL UPPER() of the value.")
TRIM = SqlFunction(
    "trim", lambda column: func.trim(column, type_=String()),
    description="SQL TRIM() of the value.")
LENGTH = SqlFunction(
    "length", lambda column: func.length(column, type_=Integer()),
    result_type=lambda annotation: int,
    description="SQL LENGTH() of the value.")
ABS = SqlFunction(
    "abs", lambda column: func.abs(column, type_=getattr(column, "type", None)),
    description="SQL ABS() of the value.")
DATE = SqlFunction(
    "date", lambda column: func.date(column, type_=Date()),
    result_type=lambda annotation: date,
    description="SQL DATE() of the value.")


def _extract(field: str) -> SqlFunction:
    return SqlFunction(
        field,
        lambda column: extract(field, column),
        result_type=lambda annotation: int,
        description=f"SQL EXTRACT({field.upper()}) of the value.",
    )


YEAR, MONTH, DAY, HOUR, MINUTE = (
    _extract(field) for field in ("year", "month", "day", "hour", "minute"))

SCALAR_TYPES: tuple[type, ...] = (
    str, int, float, Decimal, bool, datetime, date, time, UUID, Enum)
NUMERIC_TYPES: tuple[type, ...] = (int, float, Decimal)
ORDERED_TYPES: tuple[type, ...] = (*NUMERIC_TYPES, datetime, date, time)
NON_NUMERIC_SUBTYPES: tuple[type, ...] = (bool, Enum)


def equality_operators(annotation: Any) -> Iterator[SqlOperator]:
    if is_type(annotation, *SCALAR_TYPES):
        yield from (EQ, NE, IS_NULL)


def membership_operators(annotation: Any) -> Iterator[SqlOperator]:
    if is_type(annotation, *SCALAR_TYPES, exclude=(bool,)):
        yield from (IN, NOT_IN)


def comparison_operators(annotation: Any) -> Iterator[SqlOperator]:
    if is_type(annotation, *ORDERED_TYPES, exclude=NON_NUMERIC_SUBTYPES):
        yield from (GT, GTE, LT, LTE, BETWEEN)


def string_operators(annotation: Any) -> Iterator[SqlOperator]:
    if is_type(annotation, str, exclude=(Enum,)):
        yield from (
            CONTAINS, ICONTAINS, STARTSWITH, ISTARTSWITH, ENDSWITH,
            IENDSWITH, LIKE, NOT_LIKE, ILIKE
        )


def string_functions(annotation: Any) -> Iterator[SqlFunction]:
    if is_type(annotation, str, exclude=(Enum,)):
        yield from (LOWER, UPPER, TRIM, LENGTH)


def numeric_functions(annotation: Any) -> Iterator[SqlFunction]:
    if is_type(annotation, *NUMERIC_TYPES, exclude=NON_NUMERIC_SUBTYPES):
        yield ABS


def temporal_functions(annotation: Any) -> Iterator[SqlFunction]:
    if is_type(annotation, datetime):
        yield from (DATE, YEAR, MONTH, DAY, HOUR, MINUTE)
    elif is_type(annotation, date):
        yield from (YEAR, MONTH, DAY)
    elif is_type(annotation, time):
        yield from (HOUR, MINUTE)


DEFAULT_OPERATOR_GENERATORS: tuple[OperatorGenerator, ...] = (
    equality_operators,
    membership_operators,
    comparison_operators,
    string_operators,
)
DEFAULT_FUNCTION_GENERATORS: tuple[FunctionGenerator, ...] = (
    string_functions,
    numeric_functions,
    temporal_functions,
)


def _all_of(value, conditions):
    if isinstance(value, list):
        return join_conditions(
            and_, (join_conditions(and_, conditions(v)) for v in value))
    return join_conditions(and_, conditions(value))


def _any_of(value, conditions):
    if isinstance(value, list):
        return join_conditions(
            or_, (join_conditions(and_, conditions(v)) for v in value))
    return join_conditions(or_, conditions(value))


def _none_of(value, conditions):
    expression = _all_of(value, conditions)
    return None if expression is None else not_(expression)


AND = SqlLogicalOperator(
    "and",
    lambda model: Union[model, list[model]],
    _all_of,
    description=(
        "All conditions of the object (or all objects of the list) must "
        "match."
    ),
)
OR = SqlLogicalOperator(
    "or",
    lambda model: Union[model, list[model]],
    _any_of,
    description=(
        "At least one condition of the object (or one object of the list) "
        "must match."
    ),
)
NOT = SqlLogicalOperator(
    "not",
    lambda model: model,
    _none_of,
    description="The conditions of the object must not match.",
)


def logical_operators(model: type) -> Iterator[SqlLogicalOperator]:
    yield from (AND, OR, NOT)


def column_fields(
    context: "SqlFilterContext",
    model: type,
    name: str,
    field_info: FieldInfo,
    annotation: Any,
) -> Iterator[SqlFilterField]:
    """Default field generator: one filter field per supported model field,
    filtering the column with the same attribute name."""
    filter_model = context.filter_model(annotation)
    if filter_model is None:
        return
    yield SqlFilterField(
        name=name,
        annotation=filter_model,
        column=name,
        alias=field_info.alias if context.use_alias_reference else None,
        title=field_info.title,
        description=field_info.description,
    )


DEFAULT_FIELD_GENERATORS: tuple[FieldGenerator, ...] = (column_fields,)
DEFAULT_LOGICAL_GENERATORS: tuple[LogicalGenerator, ...] = (
    logical_operators,)


def _type_name(annotation: Any) -> str:
    name = getattr(annotation, "__name__", None) or re.sub(
        r"\W+", "", str(annotation))
    return name[:1].upper() + name[1:]


def _python_name(name: str, used: set[str]) -> str:
    python_name = re.sub(r"\W", "_", name)
    while (
        keyword.iskeyword(python_name)
        or python_name.startswith(("_", "model_"))
        or hasattr(SqlFilter, python_name)
        or python_name in used
    ):
        python_name += "_"
    used.add(python_name)
    return python_name


def unwrap_optional(annotation: Any) -> Any:
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


class SqlFilterMapping:
    """Registry of operator and function generators mapping python types to
    SQL operators and functions. Generators are evaluated in order, later
    generators override operators / functions of the same name.

    The generated per type filter models are cached per mapping. Use
    :meth:`copy` to customize the default mapping without side effects.
    """

    def __init__(
        self,
        operator_generators: Sequence[OperatorGenerator] = (),
        function_generators: Sequence[FunctionGenerator] = (),
        name_prefix: str = "Sql",
    ):
        self.operator_generators = list(operator_generators)
        self.function_generators = list(function_generators)
        self.name_prefix = name_prefix
        self._models: dict[Any, type[SqlFieldFilter]] = {}
        self._names: dict[Any, str] = {}

    def copy(self, name_prefix: Optional[str] = None) -> "SqlFilterMapping":
        return SqlFilterMapping(
            self.operator_generators,
            self.function_generators,
            self.name_prefix if name_prefix is None else name_prefix,
        )

    def add_operators(
        self, *generators: OperatorGenerator
    ) -> "SqlFilterMapping":
        self.operator_generators.extend(generators)
        self._clear()
        return self

    def add_functions(
        self, *generators: FunctionGenerator
    ) -> "SqlFilterMapping":
        self.function_generators.extend(generators)
        self._clear()
        return self

    def operators_for(self, annotation: Any) -> dict[str, SqlOperator]:
        return {
            operator.name: operator
            for generator in self.operator_generators
            for operator in generator(annotation)
        }

    def functions_for(self, annotation: Any) -> dict[str, SqlFunction]:
        return {
            function.name: function
            for generator in self.function_generators
            for function in generator(annotation)
        }

    def supports(self, annotation: Any) -> bool:
        return bool(
            self.operators_for(annotation) or self.functions_for(annotation))

    def filter_model(self, annotation: Any) -> Optional[type[SqlFieldFilter]]:
        """Returns the (cached) filter model of the type or ``None`` if the
        type is not supported."""
        if annotation in self._models:
            return self._models[annotation]

        plans = {}
        queue = [annotation]
        while queue:
            current = queue.pop()
            if current in self._models or current in plans:
                continue
            operators = self.operators_for(current)
            functions = {
                name: function
                for name, function in self.functions_for(current).items()
                if self.supports(function.result_type(current))
            }
            if not operators and not functions:
                continue
            plans[current] = (operators, functions)
            queue.extend(
                function.result_type(current)
                for function in functions.values()
            )

        if annotation not in plans:
            return None

        for current in plans:
            self._names[current] = self._unique_name(current)

        created = {}
        for current, (operators, functions) in plans.items():
            fields, entries, used = {}, {}, set()
            for operator in operators.values():
                python_name = _python_name(operator.name, used)
                fields[python_name] = (
                    Optional[operator.value_type(current)],
                    Field(
                        None,
                        alias=_alias(python_name, operator.name),
                        description=operator.description,
                    ),
                )
                entries[python_name] = operator
            for function in functions.values():
                python_name = _python_name(function.name, used)
                fields[python_name] = (
                    Optional[self._names[function.result_type(current)]],
                    Field(
                        None,
                        alias=_alias(python_name, function.name),
                        description=function.description,
                    ),
                )
                entries[python_name] = function
            model = create_model(
                self._names[current],
                __base__=SqlFieldFilter,
                __module__=__name__,
                **fields,
            )
            model.__sql_filter_entries__ = entries
            created[current] = model

        namespace = {
            self._names[current]: model
            for current, model in (self._models | created).items()
        }
        for model in created.values():
            model.model_rebuild(force=True, _types_namespace=namespace)
        self._models.update(created)
        return created[annotation]

    def _unique_name(self, annotation: Any) -> str:
        base = f"{self.name_prefix}{_type_name(annotation)}Filter"
        taken = set(self._names.values())
        name, index = base, 2
        while name in taken:
            name, index = f"{base}{index}", index + 1
        return name

    def _clear(self):
        self._models.clear()
        self._names.clear()


def _alias(python_name: str, name: str) -> Optional[str]:
    return None if python_name == name else name


def default_sql_filter_mapping(name_prefix: str = "Sql") -> SqlFilterMapping:
    """Creates a new mapping with the default generators. Use a distinct
    ``name_prefix`` for customized mappings to keep the names of the
    generated models unique (e.g. in an OpenAPI schema)."""
    return SqlFilterMapping(
        DEFAULT_OPERATOR_GENERATORS, DEFAULT_FUNCTION_GENERATORS, name_prefix)


DEFAULT_SQL_FILTER_MAPPING = default_sql_filter_mapping()


@dataclass(frozen=True)
class SqlFilterContext:
    """Passed to the field generators."""

    mapping: SqlFilterMapping
    use_alias_reference: bool = True

    def filter_model(self, annotation: Any) -> Optional[type[SqlFieldFilter]]:
        return self.mapping.filter_model(annotation)


def SqlFilterModel(
    base_model: type[BaseModel],
    *,
    mapping: Optional[SqlFilterMapping] = None,
    field_generators: Sequence[FieldGenerator] = DEFAULT_FIELD_GENERATORS,
    logical_generators: Sequence[LogicalGenerator] = (
        DEFAULT_LOGICAL_GENERATORS),
    exclude_fields: Optional[list[str]] = None,
    include_fields: Optional[list[str]] = None,
    source: Any = None,
    use_alias_reference: bool = True,
    model_name: Optional[str] = None,
) -> type[SqlFilter]:
    """Generates a typed, nested SQL filter model based on the fields of the
    base model.

    :param base_model: The model to generate the filter model from.
    :param mapping: Maps types to SQL operators / functions, defaults to
        :data:`DEFAULT_SQL_FILTER_MAPPING`.
    :param field_generators: Generators yielding the filter fields per field
        of the base model.
    :param logical_generators: Generators yielding the logical operators.
    :param exclude_fields: Fields of the base model to exclude.
    :param include_fields: Fields of the base model to include.
    :param source: The column source (SQLModel table model, table, alias or
        mapping of column names to columns), defaults to the base model.
    :param use_alias_reference: Use the field alias as key of the filter.
    :param model_name: Name of the model, defaults to
        ``<BaseModel>SqlFilter``.
    :return: The filter model.
    """
    mapping = DEFAULT_SQL_FILTER_MAPPING if mapping is None else mapping
    model_name = model_name or f"{base_model.__name__}SqlFilter"
    context = SqlFilterContext(mapping, use_alias_reference)

    fields, entries, python_names, keys = {}, {}, set(), set()

    def register(python_name, key, annotation, field, entry):
        if python_name in python_names or key in keys:
            raise ValueError(
                f"duplicate filter field {key!r} in {model_name}")
        python_names.add(python_name)
        keys.add(key)
        fields[python_name] = (annotation, field)
        entries[python_name] = entry

    for name, field_info in base_model.model_fields.items():
        if exclude_fields and name in exclude_fields:
            continue
        if include_fields and name not in include_fields:
            continue
        annotation = unwrap_optional(field_info.annotation)
        for generator in field_generators:
            for filter_field in generator(
                context, base_model, name, field_info, annotation
            ):
                register(
                    filter_field.name,
                    filter_field.alias or filter_field.name,
                    Optional[filter_field.annotation],
                    Field(
                        None,
                        alias=filter_field.alias,
                        title=filter_field.title,
                        description=filter_field.description,
                        json_schema_extra=filter_field.json_schema_extra,
                    ),
                    filter_field,
                )

    for generator in logical_generators:
        for logical_operator in generator(base_model):
            python_name = _python_name(logical_operator.name, set(fields))
            register(
                python_name,
                logical_operator.name,
                Optional[logical_operator.annotation(model_name)],
                Field(
                    None,
                    alias=_alias(python_name, logical_operator.name),
                    description=logical_operator.description,
                ),
                logical_operator,
            )

    model = create_model(
        model_name, __base__=SqlFilter, __module__=base_model.__module__,
        **fields
    )
    model.__sql_filter_entries__ = entries
    model.__sql_filter_source__ = base_model if source is None else source
    model.model_rebuild(force=True, _types_namespace={model_name: model})
    return model

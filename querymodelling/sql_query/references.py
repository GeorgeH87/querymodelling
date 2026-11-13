"""Queryable models, their referenceable fields and the generators of the
fields."""
from dataclasses import dataclass, replace
from typing import (
    TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Optional,
    Sequence, Union
)

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from sqlalchemy.sql.expression import FromClause

from ..sql_filter import resolve_expression, unwrap_optional
from .access import (
    FieldUsage, ModelUsage, RowsRule, Rule, SqlAccessDenied,
    SqlModelAccess, declared_field_rule, declared_model_access, resolve_rule
)

if TYPE_CHECKING:
    from .mapping import SqlExpressionMapping


ReferenceGenerator = Callable[
    ["SqlQueryContext", type, str, FieldInfo, Any],
    Iterable["SqlFieldReference"]
]
FieldSelection = Union[Sequence[str], Mapping[str, Sequence[str]], None]


@dataclass(frozen=True)
class SqlFieldReference:
    """Referenceable field. ``annotation`` is the expression type (``None``
    if the field can only be selected), ``column`` an attribute name on the
    source, a callable receiving the source or a column expression.
    ``access`` is an access rule (the rule of the field it is generated from
    if ``None``), after collection it is the resolved :class:`FieldUsage`."""

    name: str
    annotation: Any
    column: Any
    result_annotation: Any = None
    access: Rule[FieldUsage, Any] = None

    def resolve(self, source: Any) -> Any:
        return resolve_expression(self.column, source)


@dataclass(frozen=True)
class SqlModelInfo:
    """A queryable model: its name within queries, the (table) source, the
    referenceable fields, its :class:`ModelUsage` and its rows rule."""

    name: str
    model: type
    source: Any
    references: dict[str, SqlFieldReference]
    usage: ModelUsage = ModelUsage.ALL
    rows: Optional[RowsRule[Any, Any]] = None


@dataclass(frozen=True)
class SqlQueryContext:
    """Passed to the reference generators. ``access_context`` allows
    generating fields per context (e.g. per user)."""

    mapping: "SqlExpressionMapping"
    model_name: str
    use_alias_reference: bool = True
    access_context: Any = None


def model_references(
    context: SqlQueryContext,
    model: type,
    name: str,
    field_info: FieldInfo,
    annotation: Any,
) -> Iterator[SqlFieldReference]:
    """Default reference generator: every field of the model."""
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


def default_model_name(model: type) -> str:
    """The table name of the model, its lower case class name otherwise."""
    name = getattr(model, "__tablename__", None)
    return name if isinstance(name, str) else model.__name__.lower()


def is_selectable(source: Any) -> bool:
    return isinstance(source, FromClause) or hasattr(source, "__table__")


def _selection(selection: FieldSelection, name: str, base: str):
    if isinstance(selection, Mapping):
        return selection.get(name)
    return selection if name == base else None


def collect_references(
    context: SqlQueryContext,
    model: type[BaseModel],
    reference_generators: Sequence[ReferenceGenerator],
    exclude_fields: Optional[Sequence[str]] = None,
    include_fields: Optional[Sequence[str]] = None,
    rules: Optional[Mapping[str, Rule[FieldUsage, Any]]] = None,
) -> dict[str, SqlFieldReference]:
    """Collects the references of the (included) fields of the model from
    the generators. Their access rules are resolved for the access context
    of the generator context, references without any access are
    dropped."""
    rules = rules or {}
    references: dict[str, SqlFieldReference] = {}
    names = [
        name for name in model.model_fields
        if not (exclude_fields and name in exclude_fields)
        and not (include_fields and name not in include_fields)
    ]
    if not names:
        raise ValueError(f"{model.__name__} has no fields to query")
    for name in names:
        field_info = model.model_fields[name]
        annotation = unwrap_optional(field_info.annotation)
        field_rule = rules.get(name, declared_field_rule(field_info))
        for generator in reference_generators:
            for reference in generator(
                context, model, name, field_info, annotation
            ):
                if reference.name in references:
                    raise ValueError(
                        f"duplicate field reference {reference.name!r}")
                if reference.name in rules:
                    rule = rules[reference.name]
                elif reference.access is not None:
                    rule = reference.access
                else:
                    rule = field_rule
                access = resolve_rule(
                    rule, FieldUsage, context.access_context,
                    f"{context.model_name}.{reference.name}",
                )
                if not access:
                    continue
                references[reference.name] = replace(
                    reference,
                    access=access,
                    result_annotation=(
                        Optional[reference.annotation]
                        if reference.result_annotation is None
                        else reference.result_annotation
                    ),
                )
    return references


def collect_models(
    mapping: "SqlExpressionMapping",
    base_model: type[BaseModel],
    models: Union[Mapping[str, type], Sequence[type]],
    sources: Optional[Mapping[str, Any]],
    reference_generators: Sequence[ReferenceGenerator],
    exclude_fields: FieldSelection,
    include_fields: FieldSelection,
    use_alias_reference: bool,
    access: Optional[Mapping[str, SqlModelAccess[Any, Any]]] = None,
    context: Any = None,
) -> tuple[dict[str, SqlModelInfo], str]:
    """Collects the queryable models (base model first) accessible by the
    access context and returns them with the name of the base model.

    :raises SqlAccessDenied: The base model is not accessible.
    """
    if not isinstance(models, Mapping):
        models = {default_model_name(model): model for model in models}
    base_name = next(
        (name for name, model in models.items() if model is base_model),
        default_model_name(base_model),
    )
    models = {base_name: base_model} | {
        name: model for name, model in models.items() if name != base_name
    }

    infos = {}
    for name, model in models.items():
        source = (sources or {}).get(name, model)
        if not is_selectable(source):
            raise ValueError(
                f"model {name!r} is no table, pass a table or mapped class "
                "as source"
            )
        rules = declared_model_access(model, (access or {}).get(name))
        usage = resolve_rule(
            rules.usage, ModelUsage, context, repr(name))
        references = collect_references(
            SqlQueryContext(mapping, name, use_alias_reference, context),
            model,
            reference_generators,
            _selection(exclude_fields, name, base_name),
            _selection(include_fields, name, base_name),
            rules.fields,
        ) if usage else {}
        if not references:
            if name == base_name:
                raise SqlAccessDenied(f"model {name!r} is not accessible")
            continue
        infos[name] = SqlModelInfo(
            name, model, source, references, usage, rules.rows)
    return infos, base_name

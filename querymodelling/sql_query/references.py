"""Queryable models, their referenceable fields and the generators of the
fields."""
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Optional,
    Sequence, Union
)

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from sqlalchemy.sql.expression import FromClause

from ..sql_filter import resolve_expression, unwrap_optional

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
    source, a callable receiving the source or a column expression."""

    name: str
    annotation: Any
    column: Any
    result_annotation: Any = None

    def resolve(self, source: Any) -> Any:
        return resolve_expression(self.column, source)


@dataclass(frozen=True)
class SqlModelInfo:
    """A queryable model: its name within queries, the (table) source and
    the referenceable fields."""

    name: str
    model: type
    source: Any
    references: dict[str, SqlFieldReference]


@dataclass(frozen=True)
class SqlQueryContext:
    """Passed to the reference generators."""

    mapping: "SqlExpressionMapping"
    model_name: str
    use_alias_reference: bool = True


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
) -> dict[str, SqlFieldReference]:
    """Collects the references of the (included) fields of the model from
    the generators."""
    references: dict[str, SqlFieldReference] = {}
    for name, field_info in model.model_fields.items():
        if exclude_fields and name in exclude_fields:
            continue
        if include_fields and name not in include_fields:
            continue
        annotation = unwrap_optional(field_info.annotation)
        for generator in reference_generators:
            for reference in generator(
                context, model, name, field_info, annotation
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
        raise ValueError(f"{model.__name__} has no fields to query")
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
) -> tuple[dict[str, SqlModelInfo], str]:
    """Collects the queryable models (base model first) and returns them
    with the name of the base model."""
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
        infos[name] = SqlModelInfo(name, model, source, collect_references(
            SqlQueryContext(mapping, name, use_alias_reference),
            model,
            reference_generators,
            _selection(exclude_fields, name, base_name),
            _selection(include_fields, name, base_name),
        ))
    return infos, base_name

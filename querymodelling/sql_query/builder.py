"""Generation of the typed query models."""
import re
from typing import (
    Annotated, Any, Callable, Literal, Mapping, Optional, Sequence, Union
)

from pydantic import BaseModel, Field, create_model
from typing_extensions import TypeAliasType

from ..sql_filter import _alias, _python_name, _type_name
from .access import C, SqlModelAccess
from .expressions import (
    Expr, Label, Many, Query, SqlAliasReference, SqlExpressionFunction,
    SqlFieldExpression, SqlFunctionExpression
)
from .mapping import DEFAULT_SQL_EXPRESSION_MAPPING, SqlExpressionMapping
from .query import SqlJoin, SqlQuery, SqlSubquery
from .references import (
    DEFAULT_REFERENCE_GENERATORS, FieldSelection, ReferenceGenerator,
    SqlModelInfo, collect_models
)
from .scope import SqlQueryConfig
from .unions import (
    FIELD_TAG, LITERAL_TAG, QUERY_TAG, REF_TAG, tagged_union
)


JOIN_TYPES = ("inner", "left", "full")


def _camel(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(
        r"[^A-Za-z0-9]+", name))


def _optional(annotation: Any, description: str) -> tuple[Any, Any]:
    return Optional[annotation], Field(None, description=description)


def _alias_field() -> tuple[Any, Any]:
    return _optional(Label, "Name of the result column.")


def _as_field() -> tuple[Any, Any]:
    return (
        Optional[Label],
        Field(
            None,
            alias="as",
            description="Name of the model within the query (required to "
            "use a model more than once).",
        ),
    )


def _add(groups: dict[str, list], tag: str, member: Any):
    if member not in groups.setdefault(tag, []):
        groups[tag].append(member)


def _unique_type_names(types: Sequence[Any]) -> dict[Any, str]:
    names: dict[Any, str] = {}
    for annotation in types:
        name, index = _type_name(annotation), 2
        while name in names.values():
            name, index = f"{_type_name(annotation)}{index}", index + 1
        names[annotation] = name
    return names


class SqlQueryModelBuilder:
    """Builds the models of a query model:

    * per type: field models (per model), a subquery model, the function
      models and the tagged union ``<prefix><Type>Expression``,
    * ``<prefix>AnyExpression`` (entries of ``fields``),
      ``<prefix>GroupBy``, ``<prefix>Join``, ``<prefix>ExistsQuery`` and the
      query model ``<prefix>`` itself.
    """

    def __init__(
        self, prefix: str, mapping: SqlExpressionMapping,
        config: SqlQueryConfig,
    ):
        self.prefix = prefix
        self.mapping = mapping
        self.config = config
        self.types, self.functions = mapping.resolve(
            reference.annotation
            for info in config.models.values()
            for reference in info.references.values()
            if reference.annotation is not None
        )
        self.type_names = _unique_type_names(self.types)
        self.namespace: dict[str, Any] = {}
        self.created: list[type[BaseModel]] = []
        self.combined: dict[str, tuple] = {}
        self.field_models: dict[tuple, Optional[type]] = {}

    def build(self) -> type[SqlQuery]:
        self.function_models = self._function_models()
        self.query_models = {
            annotation: self._query_model(
                f"{self.type_names[annotation]}Query", SqlSubquery,
                fields=(
                    tuple[self._expressions_name((annotation,))],
                    Field(description="Exactly one field."),
                ),
                alias=_alias_field(),
            )
            for annotation in self.types
        }
        self._query_model(
            "ExistsQuery", SqlSubquery,
            fields=self._fields_field(),
        )
        self._model(
            "Join", SqlJoin,
            model=(self._model_names(), ...),
            as_=_as_field(),
            type=(Literal[JOIN_TYPES], Field("inner")),
            on=(self._expressions_name((bool,)), ...),
        )
        root = self._query_model(
            "", SqlQuery,
            model=(
                Optional[self._model_names()],
                Field(
                    None,
                    description="The queried model, defaults to "
                    f"{self.config.default_model!r}.",
                ),
            ),
            fields=self._fields_field(),
        )
        self.config.field_models = {
            name: self._field_model(None, name)
            for name in self.config.models
        }
        self._expression_aliases()
        # only the root is built eagerly, the other models are built lazily
        # (by pydantic) when used on their own
        for model in self.created:
            model.__pydantic_parent_namespace__ = self.namespace
        root.model_rebuild(force=True, _types_namespace=self.namespace)
        return root

    def _model_names(self) -> Any:
        return Literal[tuple(self.config.models)]

    def _model(self, name: str, base: type, **fields) -> type[BaseModel]:
        model = create_model(
            f"{self.prefix}{name}", __base__=base, __module__=__name__,
            **fields,
        )
        self.created.append(model)
        self.namespace[model.__name__] = model
        return model

    def _fields_field(self) -> tuple[Any, Any]:
        expression = self._expressions_name(())
        return (
            Optional[Annotated[list[expression], Field(min_length=1)]],
            Field(
                None,
                description="Result columns: constants, fields, functions "
                "or subqueries with an optional alias. All fields of the "
                "queried model if not set.",
            ),
        )

    def _query_model(self, name: str, base: type, **fields) -> type:
        model = self._model(
            name, base,
            **{
                "model": (self._model_names(), ...),
                "as_": _as_field(),
                "joins": _optional(
                    Annotated[
                        list[f"{self.prefix}Join"],
                        Field(max_length=self.config.max_joins),
                    ],
                    "Joined models.",
                ),
                "where": _optional(
                    self._expressions_name((bool,)), "Condition of the rows."),
                "group_by": _optional(
                    list[f"{self.prefix}GroupBy"],
                    "Expressions or aliases of fields to group by, an empty "
                    "list means no grouping.",
                ),
                **fields,
            },
        )
        model.__sql_query_config__ = self.config
        return model

    def _field_model(self, types: Optional[tuple], name: str):
        """Field model of a model restricted to fields of the types (all
        fields if ``types`` is ``None``)."""
        key = (types, name)
        if key not in self.field_models:
            names = tuple(
                reference.name
                for reference in self.config.models[name].references.values()
                if types is None or reference.annotation in types
            )
            self.field_models[key] = names and self._model(
                f"{_camel(name)}"
                f"{''.join(self.type_names[t] for t in types or ())}Field",
                SqlFieldExpression,
                model=(Literal[name], ...),
                field=(Literal[names], ...),
                as_=_as_field(),
                alias=_alias_field(),
            )
        return self.field_models[key]

    def _expressions_name(self, types: tuple) -> str:
        if not types:
            return f"{self.prefix}AnyExpression"
        name = "".join(self.type_names[t] for t in types)
        name = f"{self.prefix}{name}Expression"
        self.combined[name] = types
        return name

    def _argument_annotation(self, spec: Any) -> Any:
        if isinstance(spec, Many):
            name = self._expressions_name(spec.types)
            return Union[list[name], name]
        if isinstance(spec, Expr):
            return self._expressions_name(spec.types)
        if isinstance(spec, Query):
            return f"{self.prefix}ExistsQuery"
        return spec

    def _arguments_annotation(self, function: SqlExpressionFunction) -> Any:
        arguments = [
            self._argument_annotation(a) for a in function.arguments]
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
        return options[0] if len(options) == 1 else Union[tuple(options)]

    def _function_name(self, function: SqlExpressionFunction) -> str:
        same = [
            f for f in self.functions.values()
            if f.name == function.name
            and f.result_type is function.result_type
        ]
        if len(same) == 1:
            return (
                f"{_camel(function.name)}"
                f"{self.type_names[function.result_type]}Function"
            )
        argument = next(
            (s for s in function.arguments if isinstance(s, Expr)), None)
        types = argument.types if argument is not None else ()
        return (
            f"{_camel(function.name)}"
            f"{''.join(self.type_names[t] for t in types) or 'Any'}Function"
        )

    def _function_models(self) -> dict[Any, list[type]]:
        models: dict[Any, list[type]] = {t: [] for t in self.types}
        for function in self.functions.values():
            python_name = _python_name(function.name, {"alias"})
            model = self._model(
                self._function_name(function),
                SqlFunctionExpression,
                **{
                    python_name: (
                        self._arguments_annotation(function),
                        Field(
                            alias=_alias(python_name, function.name),
                            description=function.description,
                        ),
                    ),
                    "alias": _alias_field(),
                },
            )
            model.__sql_function__ = function
            model.__sql_argument_field__ = python_name
            models[function.result_type].append(model)
        return models

    def _with_compatible(self, types: tuple) -> list[Any]:
        result = list(types)
        for annotation in types:
            for compatible in self.mapping.compatible_types.get(
                    annotation, ()):
                if compatible in self.type_names and compatible not in result:
                    result.append(compatible)
        return result

    def _groups(self, types: Sequence[Any], fields: Callable) -> dict:
        groups: dict[str, list] = {}
        for annotation in types:
            _add(groups, LITERAL_TAG, self.mapping.literal_type(annotation))
        for name in self.config.models:
            if model := fields(name):
                _add(groups, FIELD_TAG, model)
        for annotation in types:
            _add(groups, QUERY_TAG, self.query_models[annotation])
            for model in self.function_models[annotation]:
                _add(groups, model.__sql_function__.name, model)
        return groups

    def _expression_aliases(self):
        for annotation in self.types:
            self._expressions_name((annotation,))
        for name, types in self.combined.items():
            types = self._with_compatible(types)
            groups = self._groups(
                types, lambda model: self._field_model(tuple(types), model))
            self.namespace[name] = TypeAliasType(name, tagged_union(groups))

        groups = self._groups(
            self.types, lambda model: self._field_model(None, model))
        name = f"{self.prefix}AnyExpression"
        self.namespace[name] = TypeAliasType(name, tagged_union(groups))

        groups = {
            tag: members for tag, members in groups.items()
            if tag != LITERAL_TAG
        } | {REF_TAG: [SqlAliasReference]}
        name = f"{self.prefix}GroupBy"
        self.namespace[name] = TypeAliasType(name, tagged_union(groups))


def build_query_model(
    infos: dict[str, SqlModelInfo],
    default_model: str,
    mapping: SqlExpressionMapping,
    prefix: str,
    max_joins: int,
    max_depth: int,
) -> type[SqlQuery]:
    """Builds the query model of collected models."""
    config = SqlQueryConfig(
        models=infos,
        default_model=default_model,
        max_joins=max_joins,
        max_depth=max_depth,
        result_name=f"{prefix}Result",
    )
    return SqlQueryModelBuilder(prefix, mapping, config).build()


def SqlQueryModel(
    base_model: type[BaseModel],
    *,
    models: Union[Mapping[str, type], Sequence[type]] = (),
    sources: Optional[Mapping[str, Any]] = None,
    mapping: Optional[SqlExpressionMapping] = None,
    reference_generators: Sequence[ReferenceGenerator] = (
        DEFAULT_REFERENCE_GENERATORS),
    exclude_fields: FieldSelection = None,
    include_fields: FieldSelection = None,
    use_alias_reference: bool = True,
    access: Optional[Mapping[str, SqlModelAccess[C, Any]]] = None,
    context: C = None,
    max_joins: int = 5,
    max_depth: int = 3,
    model_name: Optional[str] = None,
) -> type[SqlQuery]:
    """Generates a typed SQL query model (``model``, ``joins``, ``fields``,
    ``where`` and ``group_by``) for the base model and the models which may
    be joined or used within subqueries.

    :param base_model: The default queried model.
    :param models: Further models usable within ``joins`` and subqueries,
        either a mapping of their names or a list (named by their table
        name).
    :param sources: The table (or mapped class) per model name, defaults to
        the model itself.
    :param mapping: Maps types to SQL functions, defaults to
        :data:`DEFAULT_SQL_EXPRESSION_MAPPING`.
    :param reference_generators: Generators yielding the referenceable
        fields per field of a model.
    :param exclude_fields: Fields to exclude, a list (base model) or a
        mapping of model names to lists.
    :param include_fields: Fields to include, like ``exclude_fields``.
    :param use_alias_reference: Reference fields by their alias.
    :param access: Access rules per model name, they take precedence over
        the rules declared on the models (``__sql_access__``, fields
        annotated with ``SqlFieldAccess``).
    :param context: The access context (any object, e.g. the user) the
        access rules are evaluated for, ``None`` if not given. Use
        :class:`SqlQueryFactory` to create (cached) query models per
        context.
    :param max_joins: Maximum number of joins per (sub)query.
    :param max_depth: Maximum nesting depth of subqueries.
    :param model_name: Name of the model (prefix of all generated models),
        defaults to ``<BaseModel>SqlQuery``.
    :return: The query model.
    :raises SqlAccessDenied: The base model is not accessible.
    """
    mapping = DEFAULT_SQL_EXPRESSION_MAPPING if mapping is None else mapping
    infos, default_model = collect_models(
        mapping, base_model, models, sources, reference_generators,
        exclude_fields, include_fields, use_alias_reference, access,
        context,
    )
    return build_query_model(
        infos, default_model, mapping,
        model_name or f"{base_model.__name__}SqlQuery", max_joins, max_depth,
    )

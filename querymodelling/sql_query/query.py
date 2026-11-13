"""Runtime of the generated query models: validation, labels, compilation
into SQL and typed results. Subqueries share the structure of the query."""
import re
from typing import Any, ClassVar, NamedTuple

from pydantic import (
    BaseModel, ConfigDict, PrivateAttr, TypeAdapter, ValidationInfo,
    create_model, model_validator
)
from sqlalchemy import literal, literal_column, select
from sqlalchemy.orm import join as orm_join
from sqlalchemy.sql import Select

from ..sql_filter import _alias, _python_name
from .access import FieldUsage, ModelUsage
from .expressions import (
    LABEL_MAX_LENGTH, Expr, Query, SqlAliasReference, SqlExpressionBase,
    SqlFieldExpression, SqlFunctionExpression, SqlQueryExpression,
    contains_aggregate, contains_alias
)
from .results import SqlResultField, SqlResultInfo, function_nullable
from .scope import (
    Scopes, SqlInstance, SqlQueryConfig, reference_label, resolve_instance
)


ACCESS_CONTEXT_KEY = "sql_access_context"
"""Key of the access context within the validation context
(``model_validate(data, context={ACCESS_CONTEXT_KEY: context})``), passed
to the rows rules (``None`` if not given)."""

CLAUSES = {
    FieldUsage.SELECT: "fields",
    FieldUsage.FILTER: "where / on",
    FieldUsage.GROUP: "group_by",
    FieldUsage.AGGREGATE: "aggregate functions",
}


class SqlQueryColumn(NamedTuple):
    label: str
    annotation: Any
    expression: Any


class SqlJoin(BaseModel):
    """Base of the generated join models
    (``{"model", "as", "type", "on"}``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @property
    def instance_key(self) -> str:
        return self.as_ or self.model


class SqlQueryBase(SqlQueryExpression):
    """Base of the generated query and subquery models."""

    __sql_query_config__: ClassVar[SqlQueryConfig]

    _sql_context: Any = PrivateAttr(default=None)

    @property
    def model_name(self) -> str:
        return self.model or self.__sql_query_config__.default_model

    @property
    def instance_key(self) -> str:
        return self.as_ or self.model_name

    def instances(self) -> dict[str, SqlInstance]:
        """The models of the query: the queried model and the joins."""
        models = self.__sql_query_config__.models
        root = SqlInstance(self.instance_key, models[self.model_name])
        instances = {root.key: root}
        for join in self.joins or ():
            if join.instance_key in instances:
                raise ValueError(
                    f"{join.instance_key!r} is already part of the query, "
                    "use \"as\" to join it again"
                )
            if join.type == "full":
                for instance in instances.values():
                    instance.nullable = True
            instances[join.instance_key] = SqlInstance(
                join.instance_key, models[join.model],
                nullable=join.type in ("left", "full"),
            )
        return instances

    def selected(self) -> list[Any]:
        """The entries of ``fields`` (all selectable fields of the queried
        model if not set)."""
        if self.fields is not None:
            return list(self.fields)
        config = self.__sql_query_config__
        model = config.field_models[self.model_name]
        extra = {"as": self.as_} if self.as_ else {}
        return [
            model.model_validate(
                {"model": self.model_name, "field": name, **extra})
            for name, reference in config.models[
                self.model_name].references.items()
            if FieldUsage.SELECT in reference.access
        ]

    def aliases(self) -> dict[str, Any]:
        """The aliases of ``fields`` mapped to their entry."""
        return {
            item.alias: item for item in self.fields or ()
            if getattr(item, "alias", None) is not None
        }

    def grouped(self) -> list[Any]:
        """The entries of ``group_by``, aliases resolved."""
        aliases = self.aliases()
        return [
            aliases[item.ref] if isinstance(item, SqlAliasReference)
            else item
            for item in self.group_by or ()
        ]

    def labels(self) -> list[str]:
        """The labels of the result columns. Generated labels are made
        unique, aliases have to be unique."""
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

    def _auto_label(self, value: Any) -> str:
        if isinstance(value, SqlFieldExpression):
            return reference_label(value, self.instance_key)
        if isinstance(value, SqlFunctionExpression):
            return "_".join([value.__sql_function__.name] + [
                label for child in value.sql_children()
                if (label := self._auto_label(child))
            ])
        if isinstance(value, SqlQueryBase):
            return value.instance_key
        return ""

    def _key(self, value: Any) -> Any:
        """Structural key of an expression (used for grouping)."""
        if isinstance(value, SqlFieldExpression):
            return ("field", value.instance_key, value.field)
        if isinstance(value, SqlFunctionExpression):
            return (value.__sql_function__.name, tuple(
                (
                    tuple(map(self._key, argument))
                    if isinstance(argument, list) else self._key(argument)
                ) if isinstance(spec, (Expr, Query))
                else ("constant", repr(argument))
                for spec, argument in value.sql_arguments()
            ))
        if isinstance(value, SqlQueryBase):
            return ("query", value.model_dump_json(exclude={"alias"}))
        return ("constant", repr(value))

    def validate_query(
        self, outer: Scopes = (), depth: int = 0, context: Any = None
    ):
        """Validates the query and its subqueries within the scopes of the
        outer queries within the access context."""
        self._sql_context = context
        config = self.__sql_query_config__
        if depth > config.max_depth:
            raise ValueError(
                "subqueries must not be nested deeper than "
                f"{config.max_depth} levels"
            )
        if len(self.joins or ()) > config.max_joins:
            raise ValueError(f"at most {config.max_joins} joins are allowed")
        instances = self.instances()
        self._check_usage(instances, depth)
        keys = list(instances)
        for index, join in enumerate(self.joins or ()):
            visible = {key: instances[key] for key in keys[:index + 2]}
            self._check_condition(
                join.on, (visible, *outer), depth, "on", FieldUsage.FILTER)

        scopes = (instances, *outer)
        for item in self.fields or ():
            if contains_alias(item, nested_only=True):
                raise ValueError(
                    "alias is only allowed on the entries of fields")
            self._check_expression(item, scopes, depth, FieldUsage.SELECT)
        if depth == 0 and not self.selected():
            raise ValueError("there are no fields to select")
        self.labels()
        if self.where is not None:
            self._check_condition(
                self.where, scopes, depth, "where", FieldUsage.FILTER)

        aliases = self.aliases()
        for item in self.group_by or ():
            if isinstance(item, SqlAliasReference):
                if item.ref not in aliases:
                    raise ValueError(
                        f"group_by: {item.ref!r} is no alias of fields")
                self._check_expression(
                    aliases[item.ref], scopes, depth, FieldUsage.GROUP)
            else:
                self._check_condition(
                    item, scopes, depth, "group_by", FieldUsage.GROUP)
        grouped = self.grouped()
        if any(map(contains_aggregate, grouped)):
            raise ValueError(
                "aggregate functions are not allowed within group_by")
        selected = self.selected()
        if grouped or any(map(contains_aggregate, selected)):
            keys = {self._key(item) for item in grouped}
            for item in selected:
                self._check_grouped(item, keys, False)

    def _check_usage(self, instances: dict[str, SqlInstance], depth: int):
        root, *joined = instances.values()
        usage = ModelUsage.SUBQUERY if depth else ModelUsage.QUERY
        checks = [(root, usage)] + [(i, ModelUsage.JOIN) for i in joined]
        for instance, usage in checks:
            if usage not in instance.info.usage:
                where = {
                    ModelUsage.QUERY: "be queried",
                    ModelUsage.SUBQUERY: "be used within subqueries",
                    ModelUsage.JOIN: "be joined",
                }[usage]
                raise ValueError(
                    f"model {instance.info.name!r} must not {where}")

    def _check_condition(
        self, value, scopes: Scopes, depth: int, clause: str, usage: FieldUsage
    ):
        if contains_alias(value):
            raise ValueError(f"alias is not allowed within {clause}")
        if contains_aggregate(value):
            raise ValueError(
                f"aggregate functions are not allowed within {clause}")
        self._check_expression(value, scopes, depth, usage)

    def _check_expression(
        self, value: Any, scopes: Scopes, depth: int, usage: FieldUsage
    ):
        """Resolves the fields, checks their access and validates the
        subqueries. Fields within aggregate functions require
        ``AGGREGATE``."""
        if isinstance(value, SqlFieldExpression):
            instance = resolve_instance(value, scopes)
            access = instance.info.references[value.field].access
            if usage not in access:
                raise ValueError(
                    f"field {value.instance_key}.{value.field} must not be "
                    f"used within {CLAUSES[usage]}"
                )
        elif isinstance(value, SqlQueryBase):
            value.validate_query(scopes, depth + 1, self._sql_context)
        elif isinstance(value, SqlExpressionBase):
            if (
                isinstance(value, SqlFunctionExpression)
                and value.__sql_function__.aggregate
            ):
                usage = FieldUsage.AGGREGATE
            for child in value.sql_children():
                self._check_expression(child, scopes, depth, usage)

    def _check_grouped(self, value: Any, grouped: set, aggregated: bool):
        if not aggregated and self._key(value) in grouped:
            return
        if isinstance(value, SqlFunctionExpression):
            function = value.__sql_function__
            if aggregated and function.aggregate:
                raise ValueError(
                    f"aggregate function {function.name!r} must not be "
                    "nested within an aggregate function"
                )
            for child in value.sql_children():
                self._check_grouped(
                    child, grouped, aggregated or function.aggregate)
        elif isinstance(value, SqlFieldExpression) and not aggregated:
            raise ValueError(
                f"field {value.instance_key}.{value.field} must be part of "
                "group_by or be used within an aggregate function"
            )

    def result_info(self, value: Any, scopes: Scopes) -> SqlResultInfo:
        """Type, nullability and field information of the result of an
        expression (known without executing the query)."""
        if isinstance(value, SqlFieldExpression):
            instance = resolve_instance(value, scopes)
            reference = instance.info.references[value.field]
            return SqlResultInfo.of_annotation(
                reference.result_annotation,
                SqlResultField.from_field_info(reference.field_info),
                instance.nullable,
            )
        if isinstance(value, SqlFunctionExpression):
            function = value.__sql_function__
            arguments = [
                self.result_info(item, scopes)
                for spec, argument in value.sql_arguments()
                if isinstance(spec, Expr)
                for item in (
                    argument if isinstance(argument, list) else [argument])
            ]
            field = (
                function.result_field([a.field for a in arguments])
                if function.result_field else SqlResultField()
            )
            return SqlResultInfo(
                function.result_type,
                function_nullable(function.nullability, arguments),
                field,
            )
        if isinstance(value, SqlQueryBase):
            info = value.result_info(
                value.fields[0], (value.instances(), *scopes))
            # an aggregate without grouping always returns exactly one row
            single_row = not value.group_by and contains_aggregate(
                value.fields[0])
            return info if single_row else info.with_nullable()
        return SqlResultInfo(type(value), nullable=False)

    def annotation(self, value: Any, scopes: Scopes) -> Any:
        """The result type of an expression."""
        return self.result_info(value, scopes).result_annotation

    def compile_expression(self, value: Any, scopes: Scopes) -> Any:
        """Compiles an expression into a SQL expression."""
        if isinstance(value, SqlFieldExpression):
            instance = resolve_instance(value, scopes)
            return instance.info.references[value.field].resolve(
                instance.entity)
        if isinstance(value, SqlFunctionExpression):
            return value.__sql_function__.apply(*(
                self._compile_argument(spec, argument, scopes)
                for spec, argument in value.sql_arguments()
            ))
        if isinstance(value, SqlQueryBase):
            return value.to_select(scopes).scalar_subquery()
        return literal(value)

    def _compile_argument(self, spec: Any, argument: Any, scopes: Scopes):
        if isinstance(spec, Query):
            return argument.to_select(scopes)
        if not isinstance(spec, Expr):
            return argument
        if isinstance(argument, list):
            return [self.compile_expression(a, scopes) for a in argument]
        return self.compile_expression(argument, scopes)

    def scopes(self, outer: Scopes = ()) -> Scopes:
        """The scopes of the query with the SQL entities of its models."""
        instances = self.instances()
        for instance in instances.values():
            instance.entity = instance.create_entity(self._sql_context)
        return (instances, *outer)

    def to_select(self, outer: Scopes = ()) -> Select:
        """Builds the SQLAlchemy select statement."""
        scopes = self.scopes(outer)
        instances = list(scopes[0].values())
        if self.fields is None and outer:
            columns = [literal_column("1")]
        else:
            columns = [
                self.compile_expression(item, scopes).label(label)
                for item, label in zip(self.selected(), self.labels())
            ]
        source = instances[0].entity
        for join, instance in zip(self.joins or (), instances[1:]):
            source = orm_join(
                source,
                instance.entity,
                self.compile_expression(join.on, scopes),
                isouter=join.type == "left",
                full=join.type == "full",
            )
        statement = select(*columns).select_from(source)
        if self.where is not None:
            statement = statement.where(
                self.compile_expression(self.where, scopes))
        grouped = self.grouped()
        if grouped:
            statement = statement.group_by(*(
                self.compile_expression(item, scopes) for item in grouped
            ))
        if outer:
            statement = statement.correlate_except(
                *(instance.entity for instance in instances))
        return statement


class SqlSubquery(SqlQueryBase):
    """Base of the generated subquery models."""


class SqlQuery(SqlQueryBase):
    """Base of the generated (root) query models."""

    @model_validator(mode="after")
    def _validate_sql_query(self, info: ValidationInfo):
        context = info.context if isinstance(info.context, dict) else {}
        self.validate_query(
            context=context.get(ACCESS_CONTEXT_KEY))
        return self

    def columns(self) -> list[SqlQueryColumn]:
        """The result columns: label, annotation and SQL expression."""
        scopes = self.scopes()
        return [
            SqlQueryColumn(
                label,
                self.annotation(item, scopes),
                self.compile_expression(item, scopes),
            )
            for item, label in zip(self.selected(), self.labels())
        ]

    def result_infos(self) -> dict[str, SqlResultInfo]:
        """The result information of the columns by their label."""
        scopes = (self.instances(),)
        return {
            label: self.result_info(item, scopes)
            for item, label in zip(self.selected(), self.labels())
        }

    def result_model(self) -> type[BaseModel]:
        """Typed model of a result row, known without executing the query
        (cached per column signature)."""
        infos = self.result_infos()
        signature = tuple(
            (label, info.result_annotation, repr(info.field))
            for label, info in infos.items()
        )
        config = self.__sql_query_config__
        if signature not in config.result_models:
            fields, used = {}, set()
            for label, info in infos.items():
                python_name = _python_name(label, used)
                fields[python_name] = (
                    info.result_annotation,
                    info.field.field_info(_alias(python_name, label)),
                )
            config.result_models[signature] = create_model(
                config.result_name,
                __config__=ConfigDict(populate_by_name=True),
                **fields,
            )
        return config.result_models[signature]

    def result_schema(self, many: bool = True, **kwargs) -> dict[str, Any]:
        """JSON schema of the result rows (of a single row if not
        ``many``), known without executing the query."""
        model = self.result_model()
        if not many:
            return model.model_json_schema(by_alias=True, **kwargs)
        return TypeAdapter(list[model]).json_schema(by_alias=True, **kwargs)

    def fetch(self, session: Any) -> list[BaseModel]:
        """Executes the query and returns typed result rows."""
        model = self.result_model()
        result = session.execute(self.to_select())
        return [model.model_validate(dict(row._mapping)) for row in result]

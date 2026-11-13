"""Typed SQL query models with joins, subqueries, SQL functions,
conditions and grouping.

Generates a pydantic model describing a fully typed query::

    {
        "joins": [
            {
                "model": "author",
                "type": "left",
                "on": {"eq": [
                    {"model": "book", "field": "author_id"},
                    {"model": "author", "field": "id"}
                ]}
            }
        ],
        "fields": [
            {"model": "book", "field": "id"},
            {"lower": {"model": "author", "field": "name"},
             "alias": "author_name"},
            {"count": "*", "alias": "total"}
        ],
        "where": {"and": [
            {"gt": [{"model": "book", "field": "id"}, 123]},
            {"exists": {
                "model": "review",
                "where": {"eq": [
                    {"model": "review", "field": "book_id"},
                    {"model": "book", "field": "id"}
                ]}
            }}
        ]},
        "group_by": [{"model": "book", "field": "id"}, {"ref": "author_name"}]
    }

A plain JSON value (string, number, boolean) is always a constant, an
object is an expression identified by its keys: a field
(``{"model", "field"}``), a subquery (``{"model", ...}`` without ``field``),
an alias of ``fields`` (``{"ref"}``, ``group_by`` only) or a function (its
only key besides ``alias``). Conditions are functions returning ``bool``
(``eq``, ``and``, ``exists`` ...).

Expressions are typed per result type, so the JSON schema and the validation
reject e.g. ``{"lower": <int field>}`` or ``{"eq": [<int field>, "x"]}``.

Everything is assembled from generators:

* expression function generators ``(annotation) -> Iterable[
  SqlExpressionFunction]`` are called for every known type and yield SQL
  functions and conditions,
* reference generators ``(context, model, name, field_info, annotation) ->
  Iterable[SqlFieldReference]`` yield the referenceable fields.

Access rules restrict models (``__sql_access__ = SqlModelAccess(...)``) and
fields (``Annotated[str, SqlFieldAccess(...)]``) per access context (any
object, e.g. the user), see :mod:`.access` and :class:`SqlQueryFactory`.
Without rules everything is allowed.

Modules:

* :mod:`.access` - access rules of models and fields,
* :mod:`.factory` - query models per access context,
* :mod:`.expressions` - argument placeholders, function definitions and the
  base classes of the generated expression models,
* :mod:`.functions` - the default SQL functions,
* :mod:`.conditions` - the default conditions,
* :mod:`.mapping` - the registry of the function generators,
* :mod:`.references` - the queryable models and their fields,
* :mod:`.unions` - the discriminated unions of the expressions,
* :mod:`.scope` - model instances and the resolution of fields,
* :mod:`.query` - validation, compilation into SQL and typed results,
* :mod:`.results` - type, nullability and field information of results,
* :mod:`.builder` - generation of the query models.
"""
from .access import (
    INVISIBLE, VISIBLE, FieldUsage, ModelUsage, RowsRule, Rule,
    SqlAccessDenied, SqlFieldAccess, SqlModelAccess, resolve_rule
)
from .builder import SqlQueryModel, SqlQueryModelBuilder, build_query_model
from .conditions import (
    AND, CONTAINS, ENDSWITH, EXISTS, ICONTAINS, IENDSWITH, ILIKE, IS_NOT_NULL,
    IS_NULL, ISTARTSWITH, LIKE, NOT, NOT_LIKE, OR, STARTSWITH,
    DEFAULT_CONDITION_GENERATORS, comparison, comparison_conditions,
    logical_conditions, null_conditions, string_conditions
)
from .expressions import (
    LABEL_MAX_LENGTH, RESERVED_KEYS, Expr, Label, Many, Query,
    SqlAliasReference, SqlExpressionBase, SqlExpressionFunction,
    SqlFieldExpression, SqlFunctionExpression, SqlQueryExpression
)
from .factory import SqlQueryFactory, access_signature
from .functions import (
    CAST_TYPES, CASTS, CONCAT, COUNT, COUNT_DISTINCT, DATE, DAY,
    DEFAULT_VALUE_FUNCTION_GENERATORS, HOUR, LENGTH, LOWER, MINUTE, MONTH,
    NUMERIC_EXPRESSION_TYPES, ORDERED_EXPRESSION_TYPES, REPLACE, SUBSTRING,
    TRIM, UPPER, YEAR, ExpressionFunctionGenerator,
    aggregate_expression_functions, cast_expression_functions,
    generic_expression_functions, numeric_expression_functions,
    string_expression_functions, temporal_expression_functions
)
from .mapping import (
    DEFAULT_COMPATIBLE_TYPES, DEFAULT_EXPRESSION_FUNCTION_GENERATORS,
    DEFAULT_LITERAL_TYPES, DEFAULT_SQL_EXPRESSION_MAPPING, DEFAULT_TYPE_ORDER,
    SqlExpressionMapping, default_sql_expression_mapping, function_key,
    is_scalar_type
)
from .query import (
    ACCESS_CONTEXT_KEY, SqlJoin, SqlQuery, SqlQueryBase, SqlQueryColumn,
    SqlSubquery
)
from .references import (
    DEFAULT_REFERENCE_GENERATORS, ReferenceGenerator, SqlFieldReference,
    SqlModelInfo, SqlQueryContext, collect_models, collect_references,
    default_model_name, model_references
)
from .results import (
    DESCRIPTIVE_ATTRIBUTES, RESULT_FIELD_ATTRIBUTES, Nullability,
    ResultFieldRule, SqlResultField, SqlResultInfo, describe, inherit
)
from .scope import SqlInstance, SqlQueryConfig, resolve_instance

__all__ = [
    "INVISIBLE", "VISIBLE", "FieldUsage", "ModelUsage", "RowsRule", "Rule",
    "SqlAccessDenied", "SqlFieldAccess", "SqlModelAccess", "resolve_rule",
    "SqlQueryModel", "SqlQueryModelBuilder", "build_query_model", "AND",
    "CONTAINS", "ENDSWITH", "EXISTS", "ICONTAINS", "IENDSWITH", "ILIKE",
    "IS_NOT_NULL", "IS_NULL", "ISTARTSWITH", "LIKE", "NOT", "NOT_LIKE",
    "OR", "STARTSWITH", "DEFAULT_CONDITION_GENERATORS", "comparison",
    "comparison_conditions", "logical_conditions", "null_conditions",
    "string_conditions", "LABEL_MAX_LENGTH", "RESERVED_KEYS", "Expr",
    "Label", "Many", "Query", "SqlAliasReference", "SqlExpressionBase",
    "SqlExpressionFunction", "SqlFieldExpression", "SqlFunctionExpression",
    "SqlQueryExpression", "SqlQueryFactory", "access_signature",
    "CAST_TYPES", "CASTS", "CONCAT", "COUNT", "COUNT_DISTINCT", "DATE",
    "DAY", "DEFAULT_VALUE_FUNCTION_GENERATORS", "HOUR", "LENGTH", "LOWER",
    "MINUTE", "MONTH", "NUMERIC_EXPRESSION_TYPES",
    "ORDERED_EXPRESSION_TYPES", "REPLACE", "SUBSTRING", "TRIM", "UPPER",
    "YEAR", "ExpressionFunctionGenerator",
    "aggregate_expression_functions", "cast_expression_functions",
    "generic_expression_functions", "numeric_expression_functions",
    "string_expression_functions", "temporal_expression_functions",
    "DEFAULT_COMPATIBLE_TYPES", "DEFAULT_EXPRESSION_FUNCTION_GENERATORS",
    "DEFAULT_LITERAL_TYPES", "DEFAULT_SQL_EXPRESSION_MAPPING",
    "DEFAULT_TYPE_ORDER", "SqlExpressionMapping",
    "default_sql_expression_mapping", "function_key", "is_scalar_type",
    "ACCESS_CONTEXT_KEY", "SqlJoin", "SqlQuery", "SqlQueryBase",
    "SqlQueryColumn", "SqlSubquery", "DESCRIPTIVE_ATTRIBUTES",
    "RESULT_FIELD_ATTRIBUTES", "Nullability", "ResultFieldRule",
    "SqlResultField", "SqlResultInfo", "describe", "inherit",
    "DEFAULT_REFERENCE_GENERATORS", "ReferenceGenerator",
    "SqlFieldReference", "SqlModelInfo", "SqlQueryContext",
    "collect_models", "collect_references", "default_model_name",
    "model_references", "SqlInstance", "SqlQueryConfig", "resolve_instance",
]

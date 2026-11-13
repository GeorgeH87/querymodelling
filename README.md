# querymodelling
build consistent api query models for fastapi

## Requirements
At least you will need to have the pydantic package installed. Of course it make sense to have some kind of service supporting query models like fast api. Currently sql models are implemented - but feel free to create new ones.

## Installation

## Example


### Typed SQL query models

`SqlQueryModel` generates a fully typed query model (incl. JSON schema)
with `joins`, `fields`, `where`, `group_by` and subqueries, which is compiled
into a SQLAlchemy select statement. Only the registered models can be
queried.

```python
from sqlmodel import Session
from querymodelling.sql_query import SqlQueryModel

BookQuery = SqlQueryModel(Book, models=[Author, Review])

query = BookQuery.model_validate({
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
        {"model": "author", "field": "id", "alias": "author_id"},
        {"lower": {"model": "author", "field": "name"}, "alias": "author_name"}
    ],
    "where": {"and": [
        {"eq": [{"model": "book", "field": "id"}, 123]},
        {"startswith": [{"model": "author", "field": "name"}, "M"]}
    ]}
})
statement = query.to_select()      # SQLAlchemy select with labeled columns
rows = query.fetch(session)        # list of typed result rows
RowModel = query.result_model()    # typed row, without executing the query
```

```sql
SELECT book.id AS id, author.id AS author_id, lower(author.name) AS author_name
FROM book LEFT OUTER JOIN author ON book.author_id = author.id
WHERE book.id = 123 AND author.name LIKE 'M' || '%'
```

**One rule:** a plain JSON value (string, number, boolean) is always a
constant, an object is an expression identified by its keys:

| Object | Expression |
|---|---|
| `{"model": "book", "field": "id"}` | field (`"as"` selects a joined instance) |
| `{"model": "review", "fields": [...], "where": ...}` | subquery (no `field`) |
| `{"lower": ...}`, `{"eq": [...]}` | function (its only key besides `alias`) |
| `{"ref": "author_name"}` | alias of `fields` (`group_by` only) |

* **Query**: `model` (defaults to the base model), `as`, `joins`, `fields`
  (all fields of the queried model if not set), `where`, `group_by`.
  Subqueries have the same structure.
* **Joins**: `{"model", "as", "type", "on"}` with `type` `inner` (default),
  `left` or `full`. `on` may reference the queried model and the previous
  joins. A model joined twice needs `as`, it is referenced by
  `{"model": "author", "as": "reviewer", "field": "name"}`. Fields of the
  outer side of an outer join are `Optional` in the result model.
* **Conditions** (`where`, `on`) are functions returning `bool`:
  `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `between`, `in`, `not_in`
  (`{"in": [x, [1, 2]]}` or `{"in": [x, <subquery>]}`), `contains`,
  `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, `like`,
  `not_like`, `ilike`, `is_null`, `is_not_null`, `and`, `or`, `not` and
  `exists` (`{"exists": <subquery>}`).
* **Functions**: `lower`, `upper`, `trim`, `length`, `concat`, `substring`,
  `replace`, `abs`, `round`, `ceil`, `floor`, `add`, `sub`, `mul`, `div`,
  `date`, `year`, `month`, `day`, `hour`, `minute`, `coalesce`, `nullif`,
  `cast` (`{"cast": [x, "string"]}`) and the aggregates `count`
  (`{"count": "*"}` counts all rows), `count_distinct`, `sum`, `avg`, `min`,
  `max`.
* **Subqueries** used as value or in `in` return exactly one field, its type
  is the type of the expression. Subqueries may reference the models of the
  outer queries, inner models shadow outer ones with the same name (use
  `as`).
* **Typing**: all expressions are typed per result type, e.g.
  `{"lower": <int field>}` or `{"eq": [<int field>, "x"]}` are rejected by
  the JSON schema and the validation.
* **Grouping**: fields outside of aggregate functions must be part of
  `group_by`, aggregates must not be nested and are not allowed in `where`,
  `on` and `group_by`. An empty `group_by` means no grouping.
* **Limits**: `max_joins` (default 5) per (sub)query and `max_depth`
  (default 3) of nested subqueries.

```json
{
  "joins": [{"model": "author", "type": "left", "on": {"eq": [
    {"model": "book", "field": "author_id"}, {"model": "author", "field": "id"}
  ]}}],
  "fields": [
    {"lower": {"model": "author", "field": "name"}, "alias": "author_name"},
    {"count": "*", "alias": "books"},
    {"model": "review", "fields": [{"avg": {"model": "review", "field": "rating"}}],
     "where": {"eq": [{"model": "review", "field": "book_id"}, {"model": "book", "field": "id"}]},
     "alias": "avg_rating"}
  ],
  "where": {"exists": {"model": "review", "where": {"eq": [
    {"model": "review", "field": "book_id"}, {"model": "book", "field": "id"}
  ]}}},
  "group_by": [{"ref": "author_name"}, {"model": "book", "field": "id"}]
}
```

Models are registered by their table name (`models=[Author]`) or explicitly
(`models={"writer": Author}`). `sources` overrides the table of a model,
`include_fields` / `exclude_fields` restrict the fields (a list for the base
model or a mapping of model names to lists).

Functions and conditions are provided by generators `(annotation) ->
Iterable[SqlExpressionFunction]` which are called for every known type:

```python
from sqlalchemy import Integer, func
from querymodelling.sql_filter import without
from querymodelling.sql_query import (
    Expr, SqlExpressionFunction, default_sql_expression_mapping
)

def instr(annotation):
    if annotation is str:
        yield SqlExpressionFunction(
            "instr", int, (Expr(str), Expr(str)),
            lambda value, search: func.instr(value, search, type_=Integer()))

mapping = default_sql_expression_mapping()
mapping.function_generators = [
    without(generator, "cast") for generator in mapping.function_generators
]
mapping.add_functions(instr)

BookQuery = SqlQueryModel(Book, models=[Author], mapping=mapping)
```

Argument placeholders are `Expr(*types)` (an expression of the types, any
type if empty), `Many(type)` (a list or a subquery, see `in`) and `Query()`
(a subquery, see `exists`). Referenceable fields are provided by reference
generators (`reference_generators=...`, e.g. virtual fields based on SQL
expressions).

#### Result schema

The result of a validated query is known without executing it:
`query.result_model()` is the typed model of a result row,
`query.result_schema()` the JSON schema of the result rows
(`result_schema(many=False)` of a single row).

* Fields copy the information of their source field: type, `title`,
  `description`, `examples`, `json_schema_extra`, `deprecated` and the
  constraints (`max_length`, `ge`, ...). They may be `NULL` if their field is
  optional or on the outer side of an outer join.
* Constants are of their type and never `NULL`.
* Functions define their result: `result_type`, `nullable` and
  `result_field`:
  * `nullable` (`Nullability`): `PROPAGATE` (default, `NULL` if any argument
    may be `NULL`), `COALESCE` (if all arguments may be), `ALWAYS` (e.g.
    `sum`, `avg`, `min`, `max`, `nullif`, `div`) or `NEVER` (e.g. `count`,
    `is_null`, `exists`), `True` / `False` for always / never.
  * `result_field`: field information from the arguments, `inherit()` (all
    of the argument, e.g. `min`, `max`, `nullif`) or `describe()` (only its
    title and description, e.g. `lower`, `round`, `avg`, `coalesce`).
    Without a rule the result has no field information.
* Subqueries are the result of their single field, `NULL` if they may return
  no row (an aggregate without `group_by` always returns one row).

```python
SqlExpressionFunction(
    "initials", str, (Expr(str),), lambda value: func.substr(value, 1, 1),
    nullable=Nullability.PROPAGATE, result_field=describe(),
)
```

#### Access rules

Models and fields can be restricted per access context - any object of the
application, e.g. the user. Without rules everything is allowed. Rules are
fixed values or callables `(context) -> flags | bool` receiving the context
directly, `None` if no context is given. The declarations are generic in the
type of the context (`None` if not given, `SqlFieldAccess[User]`,
`SqlQueryFactory[User]`), `SqlModelAccess` also in the model of the rows
rule (`SqlModelAccess[User, Self]` within the class body, the model defaults
to `Any`).

```python
from typing import Annotated, Self
from querymodelling.sql_query import (
    INVISIBLE, FieldUsage, ModelUsage, SqlFieldAccess, SqlModelAccess,
    SqlQueryFactory
)

class Book(SQLModel, table=True):
    __sql_access__ = SqlModelAccess[User, Self](
        # rows of the user's tenant only - in every position
        rows=lambda user, book: book.tenant_id == user.tenant_id,
    )
    id: int | None = Field(default=None, primary_key=True)
    title: str
    cost: Annotated[float, SqlFieldAccess[User](lambda user: (
        FieldUsage.ALL if user.is_admin else FieldUsage.AGGREGATE))]
    tenant_id: Annotated[int, SqlFieldAccess(INVISIBLE)]

class Salary(SQLModel, table=True):
    __sql_access__ = SqlModelAccess[User](usage=lambda user: user.is_admin)
    ...

queries = SqlQueryFactory[User](Book, models=[Author, Salary])
queries.schema(user)                   # JSON schema of the user
query = queries.validate(data, user)   # validated for the user
query.fetch(session)                   # rows rules applied for the user
```

* Fields: `FieldUsage.SELECT` (`fields`), `FILTER` (`where`, `on`), `GROUP`
  (`group_by`), `AGGREGATE` (within aggregate functions), `ALL` / `NONE`
  (`VISIBLE` / `INVISIBLE`), declared by `Annotated[..., SqlFieldAccess(rule)]`
  or `SqlModelAccess(fields={"name": rule})`.
* Models: `ModelUsage.QUERY` (queried model), `JOIN`, `SUBQUERY`, declared
  by `__sql_access__ = SqlModelAccess(usage=rule)`.
* `rows`: `(context, entity) -> condition` restricts the rows of a model
  wherever it is used (queried model, joins incl. outer joins, subqueries),
  the model is replaced by a derived table of the permitted rows.
* `SqlQueryModel(..., access={"book": SqlModelAccess(...)})` overrides the
  declared rules, `SqlQueryModel(..., context=user)` evaluates them for a
  context.
* Models and fields without any access are removed from the query model and
  its JSON schema (and are reported as unknown), partial access is checked
  by the validation. `SqlQueryFactory` caches the query models per
  permission signature: contexts with the same permissions share them.
* Partial access may still reveal values: `FILTER` by comparisons,
  `AGGREGATE` by `min` / `max` of a single row.

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
RowModel = query.result_model()    # {"id": int | None, "author_id": ...}
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

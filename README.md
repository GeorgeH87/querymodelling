# querymodelling
build consistent api query models for fastapi

## Requirements
At least you will need to have the pydantic package installed. Of course it make sense to have some kind of service supporting query models like fast api. Currently sql models are implemented - but feel free to create new ones.

## Installation

## Example

### Typed SQL filter models

`SqlFilterModel` generates a nested, fully typed filter model (incl. JSON
schema) from a model and compiles it into a SQLAlchemy where clause.

```python
from sqlmodel import SQLModel, Field, select
from querymodelling.sql_filter import SqlFilterModel

class ExampleModel(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    description: str

ExampleFilter = SqlFilterModel(ExampleModel)

query = ExampleFilter.model_validate({
    "and": {
        "name": {"contains": "wurst", "startswith": "bump"},
        "description": {"lower": {"contains": "test"}},
    },
    "or": [{"id": {"in": [1, 2]}}, {"name": {"length": {"gt": 3}}}],
})
statement = query.apply_sql(select(ExampleModel))
# or: select(ExampleModel).where(query.to_sql_expression())
```

* Fields of the filter object are combined with `AND`.
* `and` / `or` accept an object (its conditions are combined) or a list of
  objects (each object is combined with `AND`, the list with the operator).
  `not` negates an object. Everything can be nested.
* Operators per type (default): `eq`, `ne`, `is_null`, `in`, `not_in`
  (scalars), `gt`, `gte`, `lt`, `lte`, `between` (numbers, dates, times),
  `contains`, `icontains`, `startswith`, `istartswith`, `endswith`,
  `iendswith`, `like`, `not_like`, `ilike` (strings).
* SQL functions per type (default): `lower`, `upper`, `trim`, `length`
  (strings), `abs` (numbers), `date`, `year`, `month`, `day`, `hour`,
  `minute` (dates / times). Functions return a filter of their result type,
  e.g. `{"name": {"length": {"gt": 3}}}`.

Everything is built from generators and can be extended or restricted:

```python
from querymodelling.sql_filter import (
    SqlOperator, SqlFunction, default_sql_filter_mapping, for_types, without
)

def parity(annotation):
    yield SqlOperator(
        "even", lambda column, value: (column % 2 == 0) == value,
        value_type=lambda annotation: bool)

mapping = default_sql_filter_mapping(name_prefix="Custom")
mapping.operator_generators = [
    without(generator, "like", "ilike")
    for generator in mapping.operator_generators
]
mapping.add_operators(for_types(parity, int, exclude=(bool,)))

ExampleFilter = SqlFilterModel(ExampleModel, mapping=mapping)
```

* operator generators `(annotation) -> Iterable[SqlOperator]`
* function generators `(annotation) -> Iterable[SqlFunction]`
* field generators `(context, model, name, field_info, annotation) ->
  Iterable[SqlFilterField]` (`field_generators=...`, e.g. virtual fields
  based on SQL expressions)
* logical generators `(model) -> Iterable[SqlLogicalOperator]`
  (`logical_generators=...`)

The filter can be used within existing query models as well:

```python
from querymodelling.pydantic import QueryField
from querymodelling.sql_filter import sql_filter_expression

class ExampleQuery(PageQuery, BaseModel):
    filter: ExampleFilter | None = QueryField(
        sql_filter_expression, default=None)
```


### Typed SQL query models

`SqlQueryModel` extends the filter by selectable `fields` (incl. SQL
functions), `where` (the filter above) and `group_by`. All expressions are
typed per result type and reflected in the JSON schema.

```python
from querymodelling.sql_query import SqlQueryModel

ExampleQuery = SqlQueryModel(ExampleModel)

query = ExampleQuery.model_validate({
    "fields": [
        "name",
        {"field": "id", "alias": "my_id"},
        {"count": "id", "alias": "count_id"},
        {"lower": "name"},
        {"concat": ["name", {"value": " - "}, "description"]},
    ],
    "where": {"name": {"contains": "wurst"}},
    "group_by": ["name", "id", "description"],
})
statement = query.to_select()      # SQLAlchemy select with labeled columns
rows = query.fetch(session)        # list of typed result rows
RowModel = query.result_model()    # e.g. {"name": str, "count_id": int, ...}
```

* An entry of `fields` is a field name or an object with exactly one
  function (or `field`) and an optional `alias`. Without `fields` all fields
  are selected. Labels without alias are generated (`lower_name`).
* Arguments are typed expressions: field names (strings are always field
  names), `{"field": ...}`, JSON numbers / booleans, `{"value": ...}` for
  other literals (e.g. strings) or nested functions. `{"lower": "amount"}`
  is rejected for an `int` field, `{"length": "name"}` is an `int`
  expression.
* Single arguments are passed directly (`{"lower": "name"}`), multiple ones
  as list (`{"substring": ["name", 1, 3]}`).
* Default functions:
  * string: `lower`, `upper`, `trim`, `length`, `concat`, `substring`,
    `replace`
  * numeric: `abs`, `round`, `ceil`, `floor`, `add`, `sub`, `mul`, `div`
  * date / time: `date`, `year`, `month`, `day`, `hour`, `minute`
  * generic: `coalesce`, `nullif`, `cast` (`{"cast": ["amount", "string"]}`)
  * aggregate: `count` (incl. `{"count": "*"}`), `count_distinct`, `sum`,
    `avg`, `min`, `max`
* Aggregates are validated: fields outside of aggregate functions must be
  part of `group_by`, aggregates must not be nested.

Functions are provided by generators `(annotation) -> Iterable[
SqlExpressionFunction]` which are called for every known type:

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

ExampleQuery = SqlQueryModel(ExampleModel, mapping=mapping)
```

Referenceable fields are provided by reference generators
(`reference_generators=...`, e.g. virtual fields based on SQL expressions).

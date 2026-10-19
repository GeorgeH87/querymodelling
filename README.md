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


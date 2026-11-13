Release History
===============

0.0.13
----------

-   sql_filter: add typed, nested and generator based SQL filter models
    (``SqlFilterModel``) with ``and`` / ``or`` / ``not``, typed operators,
    SQL functions and JSON schema support
-   sql_query: add typed, generator based SQL query models
    (``SqlQueryModel``) with ``fields`` (field names, SQL functions,
    aliases), ``where`` and ``group_by`` incl. typed result models
-   sql_query: ``group_by`` accepts aliases of ``fields`` and an empty
    list (no grouping)
-   sql_query: joins, subqueries (``exists``, ``in``, scalar) and conditions
    as typed expressions (``{"eq": [a, b]}``), fields are referenced by
    ``{"model", "field"}``, plain JSON values are constants
-   sql_query: access rules of models (usage, rows) and fields per user
    (``SqlModelAccess``, ``SqlFieldAccess``) and query models per user
    (``SqlQueryFactory``)
-   sql_query: result models and schemas of validated queries copy the
    information of the selected fields, functions define their nullability
    and result field (``Nullability``, ``inherit``, ``describe``)

0.0.12 (2025-10-27)

-   sql: add support for filtering boolean fields

0.0.11 (2025-09-22)
------------------

-   sql: adding support for query supplements

0.0.8 (2025-03-12)
------------------

-   redis_om: add support

0.0.6 (2025-03-04)
------------------

-   sql: remove dot name alias again
    since they are producing a lot of side effects

0.0.5 (2025-02-28)
------------------

-   sql: add name field to meta data

0.0.4 (2025-02-11)
------------------

-   sql: support for enums

0.0.2 (2024-01-15)
------------------

-   support of dot name alias
-   use a separate field for each operator

0.0.1 (2024-01-13)
------------------

-   Birth
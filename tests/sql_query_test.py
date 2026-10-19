import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ValidationError
from sqlalchemy import Integer, func
from sqlmodel import Field, Session, SQLModel, create_engine

from querymodelling.sql_filter import only, without
from querymodelling.sql_query import (
    Expr, SqlExpressionFunction, SqlFieldReference, SqlQuery, SqlQueryModel,
    CAST_TYPES, default_sql_expression_mapping, model_references
)


UTC = timezone.utc


class QueryExample(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None
    amount: int = 0
    price: float = 0
    created: Optional[datetime] = None


ROWS = [
    dict(id=1, name="bumpwurst", description="a test", amount=5, price=1.5,
         created=datetime(2024, 1, 5, 10, tzinfo=UTC)),
    dict(id=2, name="Bratwurst", description=None, amount=10, price=2.5,
         created=datetime(2024, 6, 1, 12, tzinfo=UTC)),
    dict(id=3, name="bump", description="x", amount=-20, price=0.0,
         created=None),
    dict(id=4, name="Bratwurst", description="Test", amount=3, price=4.0,
         created=datetime(2025, 3, 3, 8, tzinfo=UTC)),
]

ExampleQuery = SqlQueryModel(QueryExample)


class SqlQueryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(cls.engine)
        with Session(cls.engine) as session:
            session.add_all(QueryExample(**row) for row in ROWS)
            session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def fetch(self, data: dict, model: type[SqlQuery] = ExampleQuery):
        query = model.model_validate(data)
        with Session(self.engine) as session:
            return [
                row.model_dump(by_alias=True) for row in query.fetch(session)
            ]

    def test_fields_with_alias(self):
        rows = self.fetch({
            "fields": [
                "name",
                {"field": "id", "alias": "my_id"},
                {"lower": "name", "alias": "lowered"},
            ],
            "where": {"id": {"lte": 2}},
        })
        self.assertEqual(rows, [
            {"name": "bumpwurst", "my_id": 1, "lowered": "bumpwurst"},
            {"name": "Bratwurst", "my_id": 2, "lowered": "bratwurst"},
        ])

    def test_default_fields(self):
        query = ExampleQuery.model_validate({"where": {"id": {"eq": 3}}})
        self.assertEqual(
            query.labels(),
            ["id", "name", "description", "amount", "price", "created"],
        )
        self.assertEqual(self.fetch({"where": {"id": {"eq": 3}}}), [{
            "id": 3, "name": "bump", "description": "x", "amount": -20,
            "price": 0.0, "created": None,
        }])

    def test_aggregates_with_group_by(self):
        rows = self.fetch({
            "fields": [
                "name",
                {"count": "id", "alias": "count_id"},
                {"count": "*"},
                {"count": "description"},
                {"sum": "amount"},
                {"avg": "amount"},
                {"min": "price"},
                {"max": {"lower": "name"}},
            ],
            "where": {"name": {"contains": "wurst"}},
            "group_by": ["name"],
        })
        self.assertEqual(sorted(rows, key=lambda r: r["name"]), [
            {"name": "Bratwurst", "count_id": 2, "count": 2,
             "count_description": 1, "sum_amount": 13, "avg_amount": 6.5,
             "min_price": 2.5, "max_lower_name": "bratwurst"},
            {"name": "bumpwurst", "count_id": 1, "count": 1,
             "count_description": 1, "sum_amount": 5, "avg_amount": 5.0,
             "min_price": 1.5, "max_lower_name": "bumpwurst"},
        ])

    def test_aggregates_without_group_by(self):
        self.assertEqual(self.fetch({"fields": [
            {"count": "*", "alias": "total"},
            {"count_distinct": "name"},
            {"sum": {"mul": ["amount", 2]}},
        ]}), [{"total": 4, "count_distinct_name": 3, "sum_mul_amount": -4}])

    def test_group_by_expression(self):
        rows = self.fetch({
            "fields": [
                {"lower": "name", "alias": "key"},
                {"count": "*", "alias": "total"},
            ],
            "group_by": [{"lower": "name"}],
        })
        self.assertEqual(
            sorted((r["key"], r["total"]) for r in rows),
            [("bratwurst", 2), ("bump", 1), ("bumpwurst", 1)],
        )

    def test_string_functions(self):
        self.assertEqual(self.fetch({
            "fields": [
                {"upper": "name"},
                {"length": {"trim": "name"}},
                {"concat": ["name", {"value": " - "},
                            {"coalesce": ["description", {"value": "?"}]}]},
                {"substring": ["name", 2, 3]},
                {"substring": ["name", 3]},
                {"replace": ["name", {"value": "wurst"}, {"value": "!"}]},
            ],
            "where": {"id": {"eq": 2}},
        }), [{
            "upper_name": "BRATWURST",
            "length_trim_name": 9,
            "concat_name_coalesce_description": "Bratwurst - ?",
            "substring_name": "rat",
            "substring_name_2": "atwurst",
            "replace_name": "Brat!",
        }])

    def test_numeric_functions(self):
        self.assertEqual(self.fetch({
            "fields": [
                {"abs": "amount"},
                {"add": ["amount", 1]},
                {"sub": ["price", 0.5]},
                {"mul": ["amount", "amount"]},
                {"div": ["amount", 4]},
                {"round": [{"div": ["amount", 3]}, 2]},
                {"round": "price", "alias": "rounded"},
                {"ceil": "price"},
                {"floor": "price"},
            ],
            "where": {"id": {"eq": 3}},
        }), [{
            "abs_amount": 20, "add_amount": -19, "sub_price": -0.5,
            "mul_amount_amount": 400, "div_amount": -5.0,
            "round_div_amount": -6.67, "rounded": 0.0, "ceil_price": 0.0,
            "floor_price": 0.0,
        }])

    def test_temporal_generic_and_cast_functions(self):
        self.assertEqual(self.fetch({
            "fields": [
                {"date": "created"},
                {"year": "created"},
                {"month": {"date": "created"}},
                {"day": "created"},
                {"hour": "created"},
                {"minute": "created"},
                {"nullif": ["amount", 5]},
                {"cast": ["amount", "string"]},
                {"cast": ["price", "integer"], "alias": "price_int"},
            ],
            "where": {"id": {"eq": 1}},
        }), [{
            "date_created": date(2024, 1, 5), "year_created": 2024,
            "month_date_created": 1, "day_created": 5, "hour_created": 10,
            "minute_created": 0, "nullif_amount": None,
            "cast_amount": "5", "price_int": 1,
        }])

    def test_typed_result_model(self):
        def annotations(data):
            model = ExampleQuery.model_validate(data).result_model()
            return {
                name: field.annotation
                for name, field in model.model_fields.items()
            }

        query = ExampleQuery.model_validate({"fields": [
            "id", "name", {"add": ["amount", 1]}, {"add": ["price", 1]},
            {"cast": ["amount", "decimal"]}, {"year": "created"},
            {"date": "created"},
        ]})
        self.assertEqual(annotations(query.model_dump(by_alias=True)), {
            "id": Optional[int],
            "name": str,
            "add_amount": Optional[int],
            "add_price": Optional[float],
            "cast_amount": Optional[Decimal],
            "year_created": Optional[int],
            "date_created": Optional[date],
        })
        self.assertEqual(annotations({"fields": [
            {"avg": "amount"}, {"sum": "price"}, {"count": "*"},
            {"max": "created"},
        ]}), {
            "avg_amount": Optional[float],
            "sum_price": Optional[float],
            "count": int,
            "max_created": Optional[datetime],
        })
        self.assertIs(
            query.result_model(),
            ExampleQuery.model_validate(query.model_dump(
                by_alias=True, exclude_none=True)).result_model(),
        )

    def test_auto_labels_are_unique(self):
        query = ExampleQuery.model_validate({"fields": [
            "name", {"field": "name", "alias": "name_2"}, {"field": "name"},
            {"lower": "name"}, {"lower": "name"},
        ]})
        self.assertEqual(
            query.labels(),
            ["name", "name_2", "name_3", "lower_name", "lower_name_2"],
        )

    def test_sql(self):
        query = ExampleQuery.model_validate({
            "fields": [{"count": "id", "alias": "count_id"}, "name"],
            "where": {"name": {"startswith": "b"}},
            "group_by": ["name"],
        })
        self.assertEqual(
            " ".join(str(query.to_select()).split()),
            "SELECT count(queryexample.id) AS count_id, queryexample.name "
            "AS name FROM queryexample WHERE (queryexample.name LIKE "
            ":name_1 || '%' ESCAPE '/') GROUP BY queryexample.name",
        )

    def test_count_without_columns_selects_from_source(self):
        query = ExampleQuery.model_validate({"fields": [{"count": "*"}]})
        self.assertIn("FROM queryexample", str(query.to_select()))

    def test_validation(self):
        invalid = {
            "type mismatch": {"fields": [{"lower": "amount"}]},
            "unknown field": {"fields": ["unknown"]},
            "unknown function": {"fields": [{"unknown": "name"}]},
            "two functions": {"fields": [{"lower": "name", "upper": "name"}]},
            "too few arguments": {"fields": [{"concat": ["name"]}]},
            "too many arguments": {"fields": [{"lower": ["name", "name"]}]},
            "string literal without value": {
                "fields": [{"concat": ["name", " "]}]},
            "wrong literal": {"fields": [{"add": ["amount", True]}]},
            "wrong value": {"fields": [{"year": {"value": "x"}}]},
            "invalid cast": {"fields": [{"cast": ["amount", "blob"]}]},
            "literal as field": {"fields": [5]},
            "empty fields": {"fields": []},
            "invalid alias": {"fields": [{"field": "id", "alias": "1a"}]},
            "nested alias": {
                "fields": [{"lower": {"upper": "name", "alias": "x"}}]},
            "duplicate alias": {"fields": [
                {"field": "id", "alias": "a"},
                {"field": "name", "alias": "a"},
            ]},
            "not grouped": {"fields": ["name", {"count": "id"}]},
            "not grouped expression": {
                "fields": [{"upper": "name"}],
                "group_by": [{"lower": "name"}],
            },
            "nested aggregate": {"fields": [{"sum": {"count": "id"}}]},
            "aggregate in group_by": {
                "fields": ["name"], "group_by": [{"count": "id"}]},
            "alias in group_by": {
                "fields": ["name"],
                "group_by": [{"field": "name", "alias": "x"}]},
            "unknown key": {"fields": ["name"], "order": "name"},
        }
        for case, data in invalid.items():
            with self.subTest(case):
                with self.assertRaises(ValidationError) as context:
                    ExampleQuery.model_validate(data)
                # one error per matching overload at most
                self.assertLessEqual(
                    context.exception.error_count(), len(CAST_TYPES))

    def test_valid_grouping(self):
        ExampleQuery.model_validate({
            "fields": [
                "name",
                {"add": [{"length": "name"}, {"count": "*"}]},
                {"upper": {"lower": "name"}},
            ],
            "group_by": ["name", {"lower": "name"}],
        })
        ExampleQuery.model_validate(
            {"fields": [{"field": "name"}], "group_by": ["name"]})

    def test_ambiguous_numeric_expressions_are_resolved_narrow_first(self):
        query = ExampleQuery.model_validate({"fields": [
            {"add": ["amount", 1]},
            {"add": ["price", 1]},
            {"add": ["amount", "price"]},
        ]})
        self.assertEqual(
            [type(f).__sql_function__.result_type for f in query.fields],
            [int, float, float],
        )

    def test_json_schema(self):
        schema = ExampleQuery.model_json_schema()
        definitions = schema["$defs"]
        self.assertEqual(
            set(schema["properties"]), {"fields", "where", "group_by"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["fields"]["anyOf"][0]["items"],
            {"$ref": "#/$defs/QueryExampleSqlQueryColumn"},
        )
        self.assertEqual(
            schema["properties"]["where"]["anyOf"][0],
            {"$ref": "#/$defs/QueryExampleSqlQueryWhere"},
        )
        column = definitions["QueryExampleSqlQueryColumn"]["oneOf"]
        self.assertEqual(column[0]["enum"], [
            "id", "name", "description", "amount", "price", "created"])
        self.assertEqual(
            column[1], {"$ref": "#/$defs/QueryExampleSqlQueryFieldReference"})

        lower = definitions["QueryExampleSqlQueryLowerStrFunction"]
        self.assertEqual(lower["required"], ["lower"])
        self.assertFalse(lower["additionalProperties"])
        self.assertEqual(
            lower["properties"]["lower"],
            {"$ref": "#/$defs/QueryExampleSqlQueryStrExpression",
             "description": "SQL LOWER()"},
        )
        self.assertIn("alias", lower["properties"])

        string = definitions["QueryExampleSqlQueryStrExpression"]["oneOf"]
        self.assertEqual(string[0]["enum"], ["name", "description"])

        concat = definitions["QueryExampleSqlQueryConcatStrFunction"]
        self.assertEqual(concat["properties"]["concat"]["minItems"], 2)

        count = definitions["QueryExampleSqlQueryCountIntFunction"]
        self.assertIn({"const": "*", "type": "string"},
                      count["properties"]["count"]["anyOf"])

        cast = definitions["QueryExampleSqlQueryCastStrFunction"]
        self.assertEqual(
            cast["properties"]["cast"]["prefixItems"][1],
            {"const": "string", "type": "string"},
        )

    def test_python_construction(self):
        query = ExampleQuery(fields=["name"], group_by=["name"])
        self.assertEqual(query.labels(), ["name"])
        query = ExampleQuery.model_validate(ExampleQuery.model_validate(
            {"fields": [{"count": "*", "alias": "c"}, "name"],
             "group_by": ["name"]}).model_dump(by_alias=True))
        self.assertEqual(query.labels(), ["c", "name"])

    def test_include_exclude_alias_and_source(self):
        class Aliased(BaseModel):
            name: str = Field(alias="title")
            amount: int
            data: dict

        model = SqlQueryModel(
            Aliased,
            exclude_fields=["amount"],
            source={
                "name": QueryExample.name,
                "data": QueryExample.description,
            },
        )
        self.assertEqual(
            list(model.__sql_query_references__), ["title", "data"])
        query = model.model_validate({
            "fields": ["title", {"lower": "title"}],
            "where": {"title": {"eq": "bump"}},
        })
        with Session(self.engine) as session:
            rows = query.fetch(session)
        self.assertEqual(
            [row.model_dump(by_alias=True) for row in rows],
            [{"title": "bump", "lower_title": "bump"}],
        )
        self.assertIsNone(model.__sql_query_references__["data"].annotation)
        model.model_validate({"fields": ["data", {"count": "data"}],
                              "group_by": ["data"]})
        with self.assertRaises(ValidationError):
            model.model_validate({"fields": [{"lower": "data"}]})

    def test_source_override(self):
        alias = QueryExample.__table__.alias("other")
        query = ExampleQuery.model_validate({
            "fields": [{"upper": "name"}], "where": {"id": {"eq": 1}}})
        statement = str(query.to_select(alias))
        self.assertIn("upper(other.name)", statement)
        self.assertIn("other.id =", statement)

    def test_custom_function_generators(self):
        def sign(annotation):
            if annotation is int:
                yield SqlExpressionFunction(
                    "sign", int, (Expr(int),),
                    lambda v: func.min(func.max(v, -1), 1),
                )

        def instr(annotation):
            if annotation is str:
                yield SqlExpressionFunction(
                    "instr", int, (Expr(str), Expr(str)),
                    lambda v, search: func.instr(v, search, type_=Integer()),
                    description="Position of the search string (1-based).",
                )

        mapping = default_sql_expression_mapping()
        mapping.function_generators = [
            without(generator, "cast", "replace")
            for generator in mapping.function_generators
        ]
        mapping.add_functions(sign, instr)
        model = SqlQueryModel(
            QueryExample, mapping=mapping, model_name="CustomQuery")

        self.assertEqual(self.fetch({
            "fields": [
                {"sign": "amount"},
                {"instr": ["name", {"value": "wurst"}], "alias": "position"},
            ],
            "where": {"id": {"in": [1, 3]}},
        }, model), [
            {"sign_amount": 1, "position": 5},
            {"sign_amount": -1, "position": 0},
        ])
        definitions = model.model_json_schema()["$defs"]
        self.assertIn("CustomQueryInstrIntFunction", definitions)
        self.assertNotIn("CustomQueryCastStrFunction", definitions)
        self.assertNotIn("CustomQueryReplaceStrFunction", definitions)
        with self.assertRaises(ValidationError):
            model.model_validate({"fields": [{"cast": ["amount", "string"]}]})

    def test_restricted_functions(self):
        mapping = default_sql_expression_mapping()
        mapping.function_generators = [
            only(generator, "lower", "count")
            for generator in mapping.function_generators
        ]
        model = SqlQueryModel(
            QueryExample, mapping=mapping, model_name="RestrictedQuery")
        self.assertEqual(
            {
                name for name in model.model_json_schema()["$defs"]
                if name.endswith("Function")
            },
            {"RestrictedQueryLowerStrFunction",
             "RestrictedQueryCountIntFunction"},
        )
        model.model_validate({"fields": [{"lower": "name"}]})
        with self.assertRaises(ValidationError):
            model.model_validate({"fields": [{"upper": "name"}]})

    def test_custom_reference_generator(self):
        def with_summary(context, model, name, field_info, annotation):
            yield from model_references(
                context, model, name, field_info, annotation)
            if name == "name":
                yield SqlFieldReference(
                    "summary", str,
                    lambda source: func.concat(
                        source.name, ":", func.coalesce(
                            source.description, "")),
                    result_annotation=str,
                )

        model = SqlQueryModel(
            QueryExample,
            reference_generators=[with_summary],
            include_fields=["id", "name"],
            model_name="SummaryQuery",
        )
        self.assertEqual(self.fetch({
            "fields": ["summary", {"upper": "summary"}],
            "where": {"id": {"eq": 1}},
        }, model), [{"summary": "bumpwurst:a test",
                     "upper_summary": "BUMPWURST:A TEST"}])

    def test_reserved_function_names(self):
        def invalid(annotation):
            yield SqlExpressionFunction(
                "alias", str, (Expr(str),), lambda v: v)

        with self.assertRaises(ValueError):
            SqlQueryModel(
                QueryExample,
                mapping=default_sql_expression_mapping().add_functions(
                    invalid),
                model_name="InvalidQuery",
            )

    def test_duplicate_references_raise(self):
        def duplicate(context, model, name, field_info, annotation):
            yield from model_references(
                context, model, name, field_info, annotation)
            yield from model_references(
                context, model, name, field_info, annotation)

        with self.assertRaises(ValueError):
            SqlQueryModel(QueryExample, reference_generators=[duplicate])

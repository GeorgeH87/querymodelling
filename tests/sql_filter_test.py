import unittest
from datetime import datetime, timezone
from enum import Enum
from typing import Iterator, Optional

from pydantic import BaseModel, ValidationError
from pydantic import Field as PydanticField
from sqlalchemy import func, or_
from sqlmodel import Field, Session, SQLModel, create_engine, select

from querymodelling.model import PageQuery
from querymodelling.pydantic import QueryField
from querymodelling.sql import retrieve_paged_entries
from querymodelling.sql_filter import (
    DEFAULT_SQL_FILTER_MAPPING, SqlFilter, SqlFilterField, SqlFilterModel,
    SqlFunction, SqlLogicalOperator, SqlOperator, column_fields,
    default_sql_filter_mapping, for_types, items, join_conditions,
    logical_operators, only, sql_filter_expression, without
)

UTC = timezone.utc


class Color(str, Enum):
    red = "red"
    blue = "blue"


class FilterExample(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None
    amount: int = 0
    color: Color = Color.red
    active: bool = True
    created: Optional[datetime] = None


ROWS = [
    dict(id=1, name="bumpwurst", description="a test", amount=5,
         color=Color.red, active=True, created=datetime(2024, 1, 5, 10, tzinfo=UTC)),
    dict(id=2, name="Bratwurst", description="other", amount=10,
         color=Color.blue, active=False, created=datetime(2023, 6, 1, 12, tzinfo=UTC)),
    dict(id=3, name="bump", description=None, amount=-20,
         color=Color.blue, active=True, created=None),
    dict(id=4, name="100%_wurst", description="Test", amount=0,
         color=Color.red, active=True, created=datetime(2025, 3, 3, 8, tzinfo=UTC)),
]

ExampleFilter = SqlFilterModel(FilterExample)


class SqlFilterTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(cls.engine)
        with Session(cls.engine) as session:
            session.add_all(FilterExample(**row) for row in ROWS)
            session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def ids(self, data: dict, model: type[SqlFilter] = ExampleFilter):
        sql_filter = model.model_validate(data)
        with Session(self.engine) as session:
            statement = sql_filter.apply_sql(select(FilterExample.id))
            return sorted(session.exec(statement).all())

    def test_example_from_spec(self):
        self.assertEqual(self.ids({
            "and": {
                "name": {"contains": "wurst", "startswith": "bump"},
                "description": {"contains": "test"},
            }
        }), [1])

    def test_empty_filter_matches_all(self):
        self.assertEqual(self.ids({}), [1, 2, 3, 4])
        self.assertEqual(self.ids({"and": [], "name": {}}), [1, 2, 3, 4])
        self.assertEqual(
            str(ExampleFilter().to_sql_expression()), "true")

    def test_top_level_fields_are_combined_with_and(self):
        self.assertEqual(
            self.ids({"name": {"contains": "wurst"}, "amount": {"gt": 5}}),
            [2],
        )

    def test_or_object_combines_all_conditions(self):
        self.assertEqual(self.ids({
            "or": {"name": {"eq": "bump", "endswith": "Bratwurst"}}
        }), [2, 3])

    def test_or_list_and_nesting(self):
        self.assertEqual(self.ids({
            "or": [
                {"amount": {"lt": 0}},
                {"and": {"color": {"eq": "red"}, "active": {"eq": True}},
                 "created": {"year": {"gte": 2025}}},
            ]
        }), [3, 4])

    def test_not(self):
        self.assertEqual(
            self.ids({"not": {"color": {"in": ["red"]}}}), [2, 3])

    def test_like_wildcards_are_escaped(self):
        self.assertEqual(self.ids({"name": {"contains": "%_"}}), [4])
        self.assertEqual(self.ids({"name": {"like": "%wurst"}}), [1, 2, 4])

    def test_operators(self):
        self.assertEqual(self.ids({"amount": {"between": [0, 5]}}), [1, 4])
        self.assertEqual(self.ids({"amount": {"not_in": [0, 5]}}), [2, 3])
        self.assertEqual(self.ids({"description": {"is_null": True}}), [3])
        self.assertEqual(
            self.ids({"description": {"is_null": False}}), [1, 2, 4])
        self.assertEqual(
            self.ids({"name": {"icontains": "BRAT"}}), [2])
        self.assertEqual(self.ids({"active": {"ne": True}}), [2])
        self.assertEqual(
            self.ids({"created": {"gte": "2024-01-01T00:00:00Z"}}), [1, 4])

    def test_functions(self):
        self.assertEqual(
            self.ids({"name": {"lower": {"startswith": "bra"}}}), [2])
        self.assertEqual(
            self.ids({"name": {"upper": {"eq": "BUMP"}}}), [3])
        self.assertEqual(
            self.ids({"name": {"length": {"lte": 4}}}), [3])
        self.assertEqual(self.ids({"amount": {"abs": {"gt": 10}}}), [3])
        self.assertEqual(
            self.ids({"created": {"date": {"eq": "2024-01-05"}}}), [1])
        self.assertEqual(
            self.ids({"created": {"date": {"month": {"eq": 3}}}}), [4])
        self.assertEqual(
            self.ids({"description": {"trim": {"lower": {"eq": "test"}}}}),
            [4],
        )

    def test_validation_is_typed(self):
        invalid = [
            {"name": {"gt": "x"}},
            {"name": {"unknown": "x"}},
            {"amount": {"eq": "abc"}},
            {"amount": {"contains": 1}},
            {"color": {"eq": "green"}},
            {"amount": {"between": [1]}},
            {"unknown": {"eq": 1}},
            {"and": 1},
        ]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(ValidationError):
                    ExampleFilter.model_validate(data)

    def test_json_schema(self):
        schema = ExampleFilter.model_json_schema()
        definitions = schema["$defs"]
        self.assertEqual(schema["$ref"], "#/$defs/FilterExampleSqlFilter")
        self.assertEqual(
            set(definitions),
            {
                "FilterExampleSqlFilter", "SqlStrFilter", "SqlIntFilter",
                "SqlColorFilter", "SqlBoolFilter", "SqlDatetimeFilter",
                "SqlDateFilter", "Color",
            },
        )
        root = definitions["FilterExampleSqlFilter"]
        self.assertFalse(root["additionalProperties"])
        self.assertEqual(
            set(root["properties"]),
            {"id", "name", "description", "amount", "color", "active",
             "created", "and", "or", "not"},
        )
        self.assertEqual(
            root["properties"]["and"]["anyOf"][:2],
            [
                {"$ref": "#/$defs/FilterExampleSqlFilter"},
                {"type": "array",
                 "items": {"$ref": "#/$defs/FilterExampleSqlFilter"}},
            ],
        )
        string = definitions["SqlStrFilter"]["properties"]
        self.assertIn("contains", string)
        self.assertIn("in", string)
        self.assertNotIn("gt", string)
        self.assertEqual(
            string["lower"]["anyOf"][0], {"$ref": "#/$defs/SqlStrFilter"})
        self.assertEqual(
            string["length"]["anyOf"][0], {"$ref": "#/$defs/SqlIntFilter"})
        self.assertEqual(
            string["in"]["anyOf"][0],
            {"type": "array", "items": {"type": "string"}},
        )
        self.assertEqual(
            set(definitions["SqlBoolFilter"]["properties"]),
            {"eq", "ne", "is_null"},
        )
        self.assertEqual(
            definitions["SqlColorFilter"]["properties"]["eq"]["anyOf"][0],
            {"$ref": "#/$defs/Color"},
        )

    def test_python_construction_by_name(self):
        sql_filter = ExampleFilter(
            and_=[ExampleFilter(amount={"in_": [5, 10]})])
        self.assertEqual(
            sql_filter.model_dump(by_alias=True, exclude_none=True),
            {"and": [{"amount": {"in": [5, 10]}}]},
        )

    def test_include_exclude_and_alias(self):
        class Aliased(BaseModel):
            name: str = PydanticField(alias="title")
            amount: int
            data: dict

        model = SqlFilterModel(
            Aliased, exclude_fields=["amount"],
            source={"name": FilterExample.name},
        )
        properties = model.model_json_schema()["$defs"]["AliasedSqlFilter"][
            "properties"]
        self.assertEqual(set(properties), {"title", "and", "or", "not"})
        self.assertEqual(self.ids({"title": {"eq": "bump"}}, model), [3])

        included = SqlFilterModel(
            FilterExample, include_fields=["name"], model_name="OnlyName")
        self.assertEqual(
            set(included.model_fields), {"name", "and_", "or_", "not_"})

    def test_source_override(self):
        sql_filter = ExampleFilter.model_validate({"name": {"eq": "x"}})
        table = FilterExample.__table__
        self.assertIn(
            "examplealias.name",
            str(sql_filter.to_sql_expression(table.alias("examplealias"))),
        )

    def test_custom_mapping_with_generators(self):
        def parity_operators(annotation) -> Iterator[SqlOperator]:
            yield SqlOperator(
                "even", lambda column, value: (column % 2 == 0) == value,
                value_type=lambda annotation: bool)

        def sign_function(annotation) -> Iterator[SqlFunction]:
            yield SqlFunction(
                "sign",
                lambda column: func.min(func.max(column, -1), 1),
                result_type=lambda annotation: int,
            )

        mapping = default_sql_filter_mapping()
        mapping.operator_generators = [
            without(generator, "like", "not_like", "ilike")
            for generator in mapping.operator_generators
        ]
        mapping.function_generators = [
            only(generator, "lower")
            for generator in mapping.function_generators
        ]
        mapping.add_operators(
            for_types(parity_operators, int, exclude=(bool,)))
        mapping.add_functions(for_types(sign_function, int, exclude=(bool,)))
        model = SqlFilterModel(
            FilterExample, mapping=mapping, model_name="CustomFilter")

        self.assertEqual(self.ids({"amount": {"even": True}}, model), [2, 3, 4])
        self.assertEqual(
            self.ids({"amount": {"sign": {"eq": -1}}}, model), [3])
        string = {
            field.alias or name
            for name, field in mapping.filter_model(str).model_fields.items()
        }
        self.assertNotIn("like", string)
        self.assertNotIn("upper", string)
        self.assertIn("lower", string)
        self.assertNotIn("even", mapping.filter_model(bool).model_fields)
        self.assertIsNot(mapping.filter_model(str),
                         DEFAULT_SQL_FILTER_MAPPING.filter_model(str))

    def test_type_models_are_cached(self):
        self.assertIs(
            DEFAULT_SQL_FILTER_MAPPING.filter_model(str),
            ExampleFilter.model_fields["name"].annotation.__args__[0],
        )
        self.assertIsNone(DEFAULT_SQL_FILTER_MAPPING.filter_model(dict))

    def test_custom_field_and_logical_generators(self):
        def search_field(context, model, name, field_info, annotation):
            yield from column_fields(
                context, model, name, field_info, annotation)
            if name == "name":
                yield SqlFilterField(
                    name="search",
                    annotation=context.filter_model(str),
                    column=lambda source: func.coalesce(
                        source.name, "") + " " + func.coalesce(
                        source.description, ""),
                    description="name and description",
                )

        def nor_operator(model):
            yield from logical_operators(model)
            yield SqlLogicalOperator(
                "nor",
                lambda model: list[model],
                lambda value, conditions: ~or_(*(
                    join_conditions(lambda *c: c[0] & c[1], conditions(v))
                    for v in value)) if value else None,
            )

        model = SqlFilterModel(
            FilterExample,
            field_generators=[search_field],
            logical_generators=[nor_operator],
            model_name="SearchFilter",
        )
        self.assertEqual(
            self.ids({"search": {"icontains": "wurst a"}}, model), [1])
        self.assertEqual(self.ids({"nor": [
            {"color": {"eq": "red"}}, {"amount": {"lt": 0}}
        ]}, model), [2])

    def test_duplicate_fields_raise(self):
        def duplicate(context, model, name, field_info, annotation):
            yield from column_fields(
                context, model, name, field_info, annotation)
            yield from column_fields(
                context, model, name, field_info, annotation)

        with self.assertRaises(ValueError):
            SqlFilterModel(FilterExample, field_generators=[duplicate])

    def test_custom_operator_values(self):
        mapping = default_sql_filter_mapping().add_operators(items(SqlOperator(
            "eq", lambda column, value: column == value.upper())))
        model = SqlFilterModel(
            FilterExample, mapping=mapping, include_fields=["name"],
            model_name="UpperFilter")
        self.assertEqual(self.ids({"name": {"eq": "bump"}}, model), [])

    def test_existing_query_model_integration(self):
        class ExampleQuery(PageQuery, BaseModel):
            filter: Optional[ExampleFilter] = QueryField(
                sql_filter_expression, default=None)

        query = ExampleQuery.model_validate({
            "size": 2,
            "filter": {"or": [{"amount": {"gte": 5}}, {"color": {"eq": "blue"}}]},
        })
        with Session(self.engine) as session:
            total, entries = retrieve_paged_entries(
                query, session, FilterExample, FilterExample.id)
        self.assertEqual(total, 3)
        self.assertEqual([entry.id for entry in entries], [1, 2])
        self.assertEqual(str(sql_filter_expression(None)), "true")


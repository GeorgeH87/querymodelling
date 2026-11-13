import unittest
from typing import Optional

from sqlalchemy import Integer, func
from sqlalchemy.orm import registry
from sqlmodel import Field, Session, SQLModel, create_engine

from querymodelling.sql_query import (
    Expr, Nullability, SqlExpressionFunction, SqlQueryModel,
    default_sql_expression_mapping, describe
)


class ResultBase(SQLModel, registry=registry()):
    pass


class ResultAuthor(ResultBase, table=True):
    __tablename__ = "author"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(description="Full name")
    nickname: Optional[str] = None


class ResultBook(ResultBase, table=True):
    __tablename__ = "book"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(
        title="Book title", description="The title", max_length=200,
        schema_extra={"examples": ["Momo"]})
    price: float = Field(ge=0, description="Price in EUR")
    author_id: Optional[int] = Field(default=None, foreign_key="author.id")


Query = SqlQueryModel(
    ResultBook, models=[ResultAuthor], model_name="ResultQuery")


def book(name: str) -> dict:
    return {"model": "book", "field": name}


def author(name: str) -> dict:
    return {"model": "author", "field": name}


AUTHOR_JOIN = {
    "model": "author", "type": "left",
    "on": {"eq": [book("author_id"), author("id")]},
}


def annotations(data: dict, model=Query) -> dict:
    query = model.model_validate(data)
    return {
        name: field.annotation
        for name, field in query.result_model().model_fields.items()
    }


def properties(data: dict) -> dict:
    return Query.model_validate(data).result_schema(many=False)[
        "properties"]


class SqlResultTest(unittest.TestCase):

    def test_nullability_is_propagated(self):
        self.assertEqual(annotations({"fields": [
            {"lower": book("title"), "alias": "a"},
            {"length": book("title"), "alias": "b"},
            {"gt": [book("price"), 10], "alias": "c"},
            {"add": [book("price"), 1], "alias": "d"},
            {"add": [book("author_id"), 1], "alias": "e"},
            {"concat": [book("title"), "-", book("title")], "alias": "f"},
        ]}), {
            "a": str, "b": int, "c": bool, "d": float,
            "e": Optional[int], "f": str,
        })

    def test_nullability_of_outer_joins(self):
        self.assertEqual(annotations({
            "joins": [AUTHOR_JOIN],
            "fields": [
                {"upper": author("name"), "alias": "a"},
                {"coalesce": [author("name"), "unknown"], "alias": "b"},
                {"coalesce": [author("name"), author("nickname")],
                 "alias": "c"},
                {"is_null": author("name"), "alias": "d"},
            ],
        }), {"a": Optional[str], "b": str, "c": Optional[str], "d": bool})

    def test_nullability_of_aggregates_and_functions(self):
        self.assertEqual(annotations({"fields": [
            {"count": "*", "alias": "a"},
            {"count_distinct": book("author_id"), "alias": "b"},
            {"sum": book("price"), "alias": "c"},
            {"max": book("title"), "alias": "d"},
            {"div": [{"sum": book("price")}, 2], "alias": "e"},
            {"nullif": [{"max": book("price")}, 0], "alias": "f"},
        ]}), {
            "a": int, "b": int, "c": Optional[float], "d": Optional[str],
            "e": Optional[float], "f": Optional[float],
        })

    def test_nullability_of_subqueries(self):
        count = {
            "model": "author",
            "fields": [{"count": "*"}],
            "where": {"eq": [author("id"), book("author_id")]},
        }
        name = {**count, "fields": [author("name")]}
        grouped = {**count, "group_by": [author("name")]}
        self.assertEqual(annotations({"fields": [
            {**count, "alias": "a"},
            {**name, "alias": "b"},
            {**grouped, "alias": "c"},
        ]}), {"a": int, "b": Optional[str], "c": Optional[int]})

    def test_field_information_is_copied(self):
        self.assertEqual(properties({"fields": [
            book("title"), {**book("price"), "alias": "cost"}]}), {
            "title": {
                "title": "Book title", "description": "The title",
                "examples": ["Momo"], "maxLength": 200, "type": "string",
            },
            "cost": {
                "title": "Cost", "description": "Price in EUR",
                "minimum": 0, "type": "number",
            },
        })

    def test_functions_define_their_result_field(self):
        result = properties({"fields": [
            {"lower": book("title"), "alias": "lowered"},
            {"length": book("title"), "alias": "size"},
            {"max": book("price"), "alias": "highest"},
            {"avg": book("price"), "alias": "average"},
        ], "group_by": [book("title")]})
        self.assertEqual(result["lowered"], {
            "title": "Book title", "description": "The title",
            "type": "string"})
        self.assertEqual(result["size"], {"title": "Size", "type": "integer"})
        self.assertEqual(result["highest"], {
            "anyOf": [{"minimum": 0, "type": "number"}, {"type": "null"}],
            "title": "Highest", "description": "Price in EUR"})
        self.assertEqual(result["average"], {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "title": "Average", "description": "Price in EUR"})

    def test_result_schema_of_the_rows(self):
        query = Query.model_validate({"fields": [book("id")]})
        schema = query.result_schema()
        self.assertEqual(schema["type"], "array")
        self.assertEqual(
            schema["items"], {"$ref": "#/$defs/ResultQueryResult"})
        self.assertEqual(
            query.result_schema(many=False)["title"], "ResultQueryResult")

    def test_custom_function_defines_its_result(self):
        def initials(annotation):
            if annotation is str:
                yield SqlExpressionFunction(
                    "initials", str, (Expr(str),),
                    lambda value: func.substr(value, 1, 1),
                    nullable=Nullability.NEVER,
                    result_field=describe(),
                )
                yield SqlExpressionFunction(
                    "position", int, (Expr(str), Expr(str)),
                    lambda value, search: func.instr(
                        value, search, type_=Integer()),
                    nullable=True,
                )

        model = SqlQueryModel(
            ResultBook, models=[ResultAuthor], model_name="CustomResult",
            mapping=default_sql_expression_mapping().add_functions(initials))
        data = {"fields": [
            {"initials": book("title"), "alias": "i"},
            {"position": [book("title"), "o"], "alias": "p"},
        ]}
        self.assertEqual(
            annotations(data, model), {"i": str, "p": Optional[int]})
        self.assertEqual(
            model.model_validate(data).result_schema(many=False)[
                "properties"]["i"]["description"],
            "The title",
        )

    def test_rows_are_validated_with_the_result_model(self):
        engine = create_engine("sqlite://")
        ResultBase.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(ResultBook(title="Momo", price=10.0))
            session.commit()
            rows = Query.model_validate({"fields": [
                book("title"), {"lower": book("title"), "alias": "lowered"},
            ]}).fetch(session)
        engine.dispose()
        self.assertEqual(
            [row.model_dump() for row in rows],
            [{"title": "Momo", "lowered": "momo"}])

import unittest
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import ValidationError
from sqlalchemy import Integer, func
from sqlmodel import Field, Session, SQLModel, create_engine

from querymodelling.sql_filter import only, without
from querymodelling.sql_query import (
    Expr, SqlExpressionFunction, SqlFieldReference, SqlQuery, SqlQueryModel,
    default_sql_expression_mapping, model_references
)


UTC = timezone.utc


class Genre(str, Enum):
    novel = "novel"
    poem = "poem"


class QueryAuthor(SQLModel, table=True):
    __tablename__ = "author"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str


class QueryBook(SQLModel, table=True):
    __tablename__ = "book"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    genre: Genre = Genre.novel
    price: float = 0
    author_id: Optional[int] = Field(default=None, foreign_key="author.id")
    reviewer_id: Optional[int] = Field(
        default=None, foreign_key="author.id")
    published: Optional[datetime] = None


class QueryReview(SQLModel, table=True):
    __tablename__ = "review"

    id: Optional[int] = Field(default=None, primary_key=True)
    book_id: int = Field(foreign_key="book.id")
    rating: int


AUTHORS = [
    dict(id=1, name="Michael Ende"),
    dict(id=2, name="Astrid"),
    dict(id=3, name="Max"),
]
BOOKS = [
    dict(id=1, title="Momo", author_id=1, reviewer_id=2, price=10.0,
         published=datetime(2020, 1, 1, tzinfo=UTC)),
    dict(id=123, title="Jim Knopf", author_id=1, reviewer_id=3, price=12.5),
    dict(id=200, title="Pippi", author_id=2, price=8.0, genre=Genre.poem),
    dict(id=300, title="Anonym", price=5.0),
]
REVIEWS = [
    dict(id=1, book_id=1, rating=5),
    dict(id=2, book_id=1, rating=3),
    dict(id=3, book_id=123, rating=4),
    dict(id=4, book_id=200, rating=2),
]

BookQuery = SqlQueryModel(
    QueryBook, models=[QueryAuthor, QueryReview], model_name="BookSqlQuery")


def book(field: str) -> dict:
    return {"model": "book", "field": field}


def author(field: str, instance: Optional[str] = None) -> dict:
    return {"model": "author", "field": field} | (
        {"as": instance} if instance else {})


def review(field: str) -> dict:
    return {"model": "review", "field": field}


def nested_exists(depth: int) -> dict:
    query = {"model": "book"}
    for _ in range(depth - 1):
        query = {"model": "book", "where": {"exists": query}}
    return {"exists": query}


AUTHOR_JOIN = {
    "model": "author",
    "type": "left",
    "on": {"eq": [book("author_id"), author("id")]},
}


class SqlQueryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(cls.engine)
        with Session(cls.engine) as session:
            session.add_all(QueryAuthor(**row) for row in AUTHORS)
            session.add_all(QueryBook(**row) for row in BOOKS)
            session.add_all(QueryReview(**row) for row in REVIEWS)
            session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def fetch(self, data: dict, model: type[SqlQuery] = BookQuery):
        query = model.model_validate(data)
        with Session(self.engine) as session:
            return [
                row.model_dump(by_alias=True) for row in query.fetch(session)
            ]

    def sql(self, data: dict, model: type[SqlQuery] = BookQuery) -> str:
        statement = model.model_validate(data).to_select()
        return " ".join(str(statement).split())

    def test_example_with_join(self):
        data = {
            "joins": [AUTHOR_JOIN],
            "fields": [
                book("id"),
                {"model": "author", "field": "id", "alias": "author_id"},
                {"lower": author("name"), "alias": "author_name"},
            ],
            "where": {"and": [
                {"eq": [book("id"), 123]},
                {"startswith": [author("name"), "M"]},
            ]},
        }
        self.assertEqual(
            self.sql(data),
            "SELECT book.id AS id, author.id AS author_id, "
            "lower(author.name) AS author_name FROM book LEFT OUTER JOIN "
            "author ON book.author_id = author.id WHERE book.id = :param_1 "
            "AND (author.name LIKE :name_1 || '%' ESCAPE '/')",
        )
        self.assertEqual(self.fetch(data), [
            {"id": 123, "author_id": 1, "author_name": "michael ende"}])

    def test_default_fields_and_model(self):
        self.assertEqual(
            self.fetch({"where": {"eq": [book("id"), 300]}}),
            [{"id": 300, "title": "Anonym", "genre": Genre.novel,
              "price": 5.0, "author_id": None, "reviewer_id": None,
              "published": None}],
        )
        self.assertEqual(self.fetch({
            "model": "author",
            "where": {"icontains": [author("name"), "M"]},
        }), [{"id": 1, "name": "Michael Ende"}, {"id": 3, "name": "Max"}])

    def test_constants(self):
        self.assertEqual(self.fetch({
            "fields": ["text", 1, 1.5, True, {"upper": "abc"}],
            "where": {"eq": [book("id"), 1]},
        }), [{"column": "text", "column_2": 1, "column_3": 1.5,
              "column_4": True, "upper": "ABC"}])

    def test_self_join_with_as(self):
        self.assertEqual(self.fetch({
            "joins": [
                AUTHOR_JOIN,
                {
                    "model": "author",
                    "as": "reviewer",
                    "type": "left",
                    "on": {"eq": [
                        book("reviewer_id"), author("id", "reviewer")]},
                },
            ],
            "fields": [book("title"), author("name"),
                       author("name", "reviewer")],
            "where": {"in": [book("id"), [1, 200]]},
        }), [
            {"title": "Momo", "author_name": "Michael Ende",
             "reviewer_name": "Astrid"},
            {"title": "Pippi", "author_name": "Astrid",
             "reviewer_name": None},
        ])

    def test_join_types(self):
        inner = {**AUTHOR_JOIN, "type": "inner"}
        self.assertEqual(len(self.fetch(
            {"joins": [inner], "fields": [book("id")]})), 3)
        self.assertEqual(len(self.fetch(
            {"joins": [AUTHOR_JOIN], "fields": [book("id")]})), 4)
        self.assertNotIn("OUTER", self.sql({
            "joins": [{"model": "author",
                       "on": {"eq": [book("author_id"), author("id")]}}],
        }))
        full = {**AUTHOR_JOIN, "type": "full"}
        self.assertIn("FULL OUTER JOIN", self.sql({"joins": [full]}))

    def test_result_types_of_outer_joins(self):
        def annotations(join_type):
            query = BookQuery.model_validate({
                "joins": [{**AUTHOR_JOIN, "type": join_type}],
                "fields": [book("title"), author("name"), {"count": "*"}],
                "group_by": [book("title"), author("name")],
            })
            return {
                name: field.annotation
                for name, field in query.result_model().model_fields.items()
            }

        self.assertEqual(annotations("inner"), {
            "title": str, "author_name": str, "count": int})
        self.assertEqual(annotations("left"), {
            "title": str, "author_name": Optional[str], "count": int})
        self.assertEqual(annotations("full"), {
            "title": Optional[str], "author_name": Optional[str],
            "count": int})

    def test_exists_subquery(self):
        rows = self.fetch({
            "fields": [book("title")],
            "where": {"exists": {
                "model": "review",
                "where": {"and": [
                    {"eq": [review("book_id"), book("id")]},
                    {"gte": [review("rating"), 4]},
                ]},
            }},
        })
        self.assertEqual(rows, [{"title": "Momo"}, {"title": "Jim Knopf"}])

    def test_in_subquery(self):
        data = {
            "fields": [book("title")],
            "where": {"in": [book("author_id"), {
                "model": "author",
                "fields": [author("id")],
                "where": {"startswith": [author("name"), "M"]},
            }]},
        }
        self.assertIn(
            "book.author_id IN (SELECT author.id AS id FROM author",
            self.sql(data),
        )
        self.assertEqual(
            self.fetch(data), [{"title": "Momo"}, {"title": "Jim Knopf"}])

    def test_scalar_subquery(self):
        data = {
            "fields": [book("title"), {
                "model": "review",
                "fields": [{"avg": review("rating")}],
                "where": {"eq": [review("book_id"), book("id")]},
                "alias": "avg_rating",
            }],
            "where": {"gt": [{
                "model": "review",
                "fields": [{"count": "*"}],
                "where": {"eq": [review("book_id"), book("id")]},
            }, 0]},
        }
        self.assertEqual(self.fetch(data), [
            {"title": "Momo", "avg_rating": 4.0},
            {"title": "Jim Knopf", "avg_rating": 4.0},
            {"title": "Pippi", "avg_rating": 2.0},
        ])
        self.assertEqual(
            BookQuery.model_validate(data).result_model().model_fields[
                "avg_rating"].annotation,
            Optional[float],
        )

    def test_correlated_subquery_on_same_model(self):
        self.assertEqual(self.fetch({
            "fields": [book("title")],
            "where": {"exists": {
                "model": "book",
                "as": "other",
                "where": {"and": [
                    {"eq": [{"model": "book", "as": "other",
                             "field": "author_id"}, book("author_id")]},
                    {"ne": [{"model": "book", "as": "other", "field": "id"},
                            book("id")]},
                ]},
            }},
        }), [{"title": "Momo"}, {"title": "Jim Knopf"}])

    def test_inner_scope_shadows_outer(self):
        statement = self.sql({
            "fields": [book("id")],
            "where": {"exists": {
                "model": "book",
                "where": {"eq": [book("id"), 1]},
            }},
        })
        self.assertIn("EXISTS (SELECT 1 FROM book WHERE book.id", statement)

    def test_group_by_with_ref(self):
        rows = self.fetch({
            "joins": [AUTHOR_JOIN],
            "fields": [
                {"lower": author("name"), "alias": "author_name"},
                {"count": "*", "alias": "books"},
                {"sum": book("price"), "alias": "total"},
            ],
            "group_by": [{"ref": "author_name"}],
        })
        self.assertEqual(rows, [
            {"author_name": None, "books": 1, "total": 5.0},
            {"author_name": "astrid", "books": 1, "total": 8.0},
            {"author_name": "michael ende", "books": 2, "total": 22.5},
        ])

    def test_empty_group_by_means_no_grouping(self):
        data = {"fields": [{"count": book("author_id")}], "group_by": []}
        self.assertEqual(self.fetch(data), [{"count_author_id": 3}])
        self.assertNotIn("GROUP BY", self.sql(data))

    def test_conditions(self):
        def ids(where):
            return [
                row["id"] for row in
                self.fetch({"fields": [book("id")], "where": where})
            ]

        self.assertEqual(ids({"eq": [book("genre"), "poem"]}), [200])
        self.assertEqual(ids({"ne": [book("genre"), "poem"]}), [1, 123, 300])
        self.assertEqual(ids({"in": [book("id"), [1, 300, 999]]}), [1, 300])
        self.assertEqual(ids({"not_in": [book("id"), [1, 300]]}), [123, 200])
        self.assertEqual(ids({"between": [book("price"), 8, 10]}), [1, 200])
        self.assertEqual(ids({"lt": [book("price"), 8]}), [300])
        self.assertEqual(ids({"is_null": book("author_id")}), [300])
        self.assertEqual(
            ids({"is_not_null": book("reviewer_id")}), [1, 123])
        self.assertEqual(
            ids({"gte": [book("published"), "2019-12-31T00:00:00Z"]}), [1])
        self.assertEqual(ids({"contains": [book("title"), "%"]}), [])
        self.assertEqual(ids({"like": [book("title"), "%i%"]}), [123, 200])
        self.assertEqual(ids({"or": [
            {"endswith": [book("title"), "mo"]},
            {"not": {"gt": [book("price"), 5]}},
        ]}), [1, 300])
        self.assertEqual(
            ids({"eq": [{"length": book("title")}, 4]}), [1])
        self.assertEqual(ids(True), [1, 123, 200, 300])

    def test_functions(self):
        self.assertEqual(self.fetch({
            "fields": [
                {"concat": [book("title"), " (", {"cast": [
                    book("price"), "string"]}, ")"], "alias": "label"},
                {"round": [{"div": [book("price"), 3]}, 2]},
                {"add": [book("id"), 1]},
                {"year": book("published")},
                {"coalesce": [book("author_id"), 0]},
            ],
            "where": {"eq": [book("id"), 1]},
        }), [{"label": "Momo (10.0)", "round_div_price": 3.33,
              "add_id": 2, "year_published": 2020,
              "coalesce_author_id": 1}])

    def test_validation(self):
        invalid = {
            "constant of wrong type": {"where": {"eq": [book("id"), "x"]}},
            "function of wrong type": {"fields": [{"lower": book("price")}]},
            "unknown field": {"fields": [book("name")]},
            "unknown model": {"fields": [{"model": "user", "field": "id"}]},
            "model not joined": {"fields": [author("name")]},
            "join refers to later join": {"joins": [
                {"model": "author",
                 "on": {"eq": [book("reviewer_id"),
                               author("id", "reviewer")]}},
                {"model": "author", "as": "reviewer",
                 "on": {"eq": [book("id"), book("id")]}},
            ]},
            "model joined twice": {"joins": [AUTHOR_JOIN, AUTHOR_JOIN]},
            "as of other model": {"joins": [{
                "model": "author", "as": "other",
                "on": {"eq": [book("author_id"),
                              {"model": "book", "as": "other",
                               "field": "id"}]},
            }]},
            "invalid join type": {"joins": [{**AUTHOR_JOIN, "type": "outer"}]},
            "too many joins": {"joins": [
                {**AUTHOR_JOIN, "as": f"a{index}"} for index in range(6)]},
            "where is no condition": {"where": book("title")},
            "aggregate in where": {"where": {"gt": [{"count": "*"}, 1]}},
            "aggregate in on": {"joins": [{
                **AUTHOR_JOIN, "on": {"gt": [{"count": "*"}, 1]}}]},
            "two functions": {"where": {"eq": [1, 1], "ne": [1, 2]}},
            "scalar subquery with two fields": {"fields": [{
                "model": "review", "fields": [review("id"), review("rating")],
            }]},
            "scalar subquery without fields": {
                "where": {"gt": [{"model": "review"}, 1]}},
            "unknown ref": {"fields": [book("title")],
                            "group_by": [{"ref": "x"}]},
            "constant in group_by": {"fields": [book("title")],
                                     "group_by": ["title"]},
            "ref outside of group_by": {"fields": [{"ref": "x"}]},
            "not grouped": {"fields": [book("title"), {"count": "*"}]},
            "nested aggregate": {"fields": [{"sum": {"count": "*"}}]},
            "aggregate in group_by": {
                "fields": [{"count": "*", "alias": "c"}],
                "group_by": [{"ref": "c"}]},
            "alias in where": {"where": {"eq": [
                {**book("id"), "alias": "x"}, 1]}},
            "nested alias": {"fields": [
                {"lower": {**book("title"), "alias": "x"}}]},
            "duplicate alias": {"fields": [
                {**book("id"), "alias": "a"},
                {**book("title"), "alias": "a"},
            ]},
            "invalid alias": {"fields": [{**book("id"), "alias": "1a"}]},
            "unknown key": {"order_by": []},
            "subquery too deep": {"where": nested_exists(4)},
        }
        for case, data in invalid.items():
            with self.subTest(case):
                with self.assertRaises(ValidationError):
                    BookQuery.model_validate(data)

    def test_subquery_depth(self):
        BookQuery.model_validate({"where": nested_exists(3)})
        with self.assertRaises(ValidationError) as context:
            BookQuery.model_validate({"where": nested_exists(4)})
        self.assertIn("deeper than 3 levels", str(context.exception))

    def test_error_messages(self):
        with self.assertRaises(ValidationError) as context:
            BookQuery.model_validate({"fields": [author("name")]})
        self.assertIn("model 'author' is not part of the query",
                      str(context.exception))
        with self.assertRaises(ValidationError) as context:
            BookQuery.model_validate({"fields": [book("name")]})
        self.assertEqual(context.exception.error_count(), 1)
        with self.assertRaises(ValidationError) as context:
            BookQuery.model_validate(
                {"fields": [book("title"), {"count": "*"}]})
        self.assertIn("field book.title must be part of group_by",
                      str(context.exception))

    def test_json_schema(self):
        schema = BookQuery.model_json_schema()
        definitions = schema["$defs"]
        self.assertEqual(
            set(schema["properties"]),
            {"model", "as", "joins", "fields", "where", "group_by"},
        )
        self.assertFalse(schema["additionalProperties"])
        join = definitions["BookSqlQueryJoin"]
        self.assertEqual(join["required"], ["model", "on"])
        self.assertEqual(
            join["properties"]["type"]["enum"], ["inner", "left", "full"])
        self.assertEqual(
            join["properties"]["model"]["enum"], ["book", "author", "review"])

        field = definitions["BookSqlQueryBookIntField"]
        self.assertEqual(field["properties"]["field"]["enum"], [
            "id", "author_id", "reviewer_id"])
        self.assertEqual(field["properties"]["model"]["const"], "book")
        self.assertFalse(field["additionalProperties"])

        integer = definitions["BookSqlQueryIntExpression"]["oneOf"]
        self.assertEqual(integer[0], {"type": "integer"})
        self.assertEqual(integer[1]["discriminator"]["propertyName"], "model")

        subquery = definitions["BookSqlQueryIntQuery"]
        self.assertEqual(subquery["required"], ["model", "fields"])
        self.assertEqual(subquery["properties"]["fields"]["maxItems"], 1)
        exists = definitions["BookSqlQueryExistsQuery"]
        self.assertEqual(exists["required"], ["model"])

        lower = definitions["BookSqlQueryLowerStrFunction"]
        self.assertEqual(
            lower["properties"]["lower"]["$ref"],
            "#/$defs/BookSqlQueryStrExpression",
        )
        self.assertIn("BookSqlQueryEqIntFunction", definitions)
        self.assertIn("BookSqlQueryEqGenreFunction", definitions)
        group_by = definitions["BookSqlQueryGroupBy"]["oneOf"]
        self.assertNotIn({"type": "string"}, group_by)
        self.assertIn({"$ref": "#/$defs/SqlAliasReference"}, group_by)

    def test_python_construction(self):
        query = BookQuery.model_validate({
            "fields": [{"lower": book("title"), "alias": "t"}],
            "where": {"exists": {"model": "review"}},
        })
        copy = BookQuery.model_validate(
            query.model_dump(by_alias=True, exclude_none=True))
        self.assertEqual(copy.labels(), ["t"])
        self.assertEqual(str(copy.to_select()), str(query.to_select()))

    def test_sources_and_include_fields(self):
        model = SqlQueryModel(
            QueryBook,
            models={"writer": QueryAuthor},
            sources={"writer": QueryAuthor.__table__},
            include_fields={"book": ["id", "author_id"], "writer": ["id"]},
            model_name="SourceQuery",
        )
        self.assertEqual(
            set(model.__sql_query_config__.models["writer"].references),
            {"id"})
        data = {
            "joins": [{"model": "writer", "as": "w", "on": {"eq": [
                book("author_id"), {"model": "writer", "as": "w",
                                    "field": "id"}]}}],
            "fields": [{"model": "writer", "as": "w", "field": "id"}],
            "where": {"eq": [book("id"), 200]},
        }
        self.assertIn("JOIN author AS w", self.sql(data, model))
        self.assertEqual(self.fetch(data, model), [{"w_id": 2}])
        with self.assertRaises(ValidationError):
            model.model_validate({"fields": [book("title")]})

    def test_custom_function_generators(self):
        def instr(annotation):
            if annotation is str:
                yield SqlExpressionFunction(
                    "instr", int, (Expr(str), Expr(str)),
                    lambda value, search: func.instr(
                        value, search, type_=Integer()),
                )

        mapping = default_sql_expression_mapping()
        mapping.function_generators = [
            without(generator, "cast", "like")
            for generator in mapping.function_generators
        ]
        mapping.add_functions(instr)
        model = SqlQueryModel(
            QueryBook, mapping=mapping, model_name="CustomQuery")
        self.assertEqual(self.fetch({
            "fields": [{"instr": [book("title"), "o"], "alias": "position"}],
            "where": {"gt": [{"instr": [book("title"), "o"]}, 1]},
        }, model), [{"position": 2}, {"position": 7}, {"position": 3}])
        definitions = model.model_json_schema()["$defs"]
        self.assertIn("CustomQueryInstrIntFunction", definitions)
        self.assertNotIn("CustomQueryCastStrFunction", definitions)
        self.assertNotIn("CustomQueryLikeFunction", definitions)

    def test_restricted_functions(self):
        mapping = default_sql_expression_mapping()
        mapping.function_generators = [
            only(generator, "eq", "and")
            for generator in mapping.function_generators
        ]
        model = SqlQueryModel(
            QueryBook, mapping=mapping, model_name="RestrictedQuery")
        model.model_validate({"where": {"and": [{"eq": [book("id"), 1]}]}})
        with self.assertRaises(ValidationError):
            model.model_validate({"where": {"gt": [book("id"), 1]}})

    def test_custom_reference_generator(self):
        def with_label(context, model, name, field_info, annotation):
            yield from model_references(
                context, model, name, field_info, annotation)
            if context.model_name == "book" and name == "title":
                yield SqlFieldReference(
                    "label", str,
                    lambda source: func.concat(
                        source.title, ":", source.price),
                    result_annotation=str,
                )

        model = SqlQueryModel(
            QueryBook, reference_generators=[with_label],
            model_name="LabelQuery")
        self.assertEqual(self.fetch({
            "fields": [book("label"), {"upper": book("label")}],
            "where": {"eq": [book("id"), 1]},
        }, model), [{"label": "Momo:10.0", "upper_label": "MOMO:10.0"}])

    def test_reserved_function_names(self):
        def invalid(annotation):
            yield SqlExpressionFunction(
                "model", str, (Expr(str),), lambda v: v)

        with self.assertRaises(ValueError):
            SqlQueryModel(
                QueryBook,
                mapping=default_sql_expression_mapping().add_functions(
                    invalid),
                model_name="InvalidQuery",
            )

    def test_models_without_table_are_rejected(self):
        from pydantic import BaseModel

        class Plain(BaseModel):
            name: str

        with self.assertRaises(ValueError):
            SqlQueryModel(Plain)

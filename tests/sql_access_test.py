import unittest
from dataclasses import dataclass
from typing import Annotated, Optional

from pydantic import ValidationError
from sqlmodel import Field, Session, SQLModel, create_engine

from querymodelling.sql_query import (
    INVISIBLE, ACCESS_CONTEXT_KEY, FieldUsage, ModelUsage, SqlAccessDenied,
    SqlFieldAccess, SqlFieldReference, SqlModelAccess, SqlQueryFactory,
    SqlQueryModel, model_references
)


@dataclass(frozen=True)
class User:
    tenant_id: int
    is_admin: bool = False


ADMIN = User(1, is_admin=True)
READER = User(1)
OTHER = User(2)


class AclAuthor(SQLModel, table=True):
    __tablename__ = "acl_author"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    email: Annotated[str, SqlFieldAccess(lambda user: user.is_admin)]


class AclBook(SQLModel, table=True):
    __tablename__ = "acl_book"
    __sql_access__ = SqlModelAccess(
        rows=lambda user, book: book.tenant_id == user.tenant_id)

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    cost: Annotated[float, SqlFieldAccess(lambda user: (
        FieldUsage.ALL if user.is_admin else FieldUsage.AGGREGATE))]
    author_id: Optional[int] = Field(
        default=None, foreign_key="acl_author.id")
    tenant_id: Annotated[int, SqlFieldAccess(INVISIBLE)]


class AclReview(SQLModel, table=True):
    __tablename__ = "acl_review"
    __sql_access__ = SqlModelAccess(usage=lambda user: (
        ModelUsage.ALL if user.is_admin else ModelUsage.SUBQUERY))

    id: Optional[int] = Field(default=None, primary_key=True)
    book_id: int = Field(foreign_key="acl_book.id")
    reviewer: Annotated[str, FieldUsage.SELECT]


class AclSecret(SQLModel, table=True):
    __tablename__ = "acl_secret"
    __sql_access__ = SqlModelAccess(usage=lambda user: user.is_admin)

    id: Optional[int] = Field(default=None, primary_key=True)
    value: str


MODELS = {
    "book": AclBook,
    "author": AclAuthor,
    "review": AclReview,
    "secret": AclSecret,
}


def field(model: str, name: str) -> dict:
    return {"model": model, "field": name}


AUTHOR_JOIN = {
    "model": "author",
    "type": "left",
    "on": {"eq": [field("book", "author_id"), field("author", "id")]},
}


def exists_review(condition: dict) -> dict:
    return {"exists": {"model": "review", "where": {"and": [
        {"eq": [field("review", "book_id"), field("book", "id")]},
        condition,
    ]}}}


class SqlAccessTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(cls.engine, tables=[
            model.__table__ for model in MODELS.values()])
        with Session(cls.engine) as session:
            session.add_all([
                AclAuthor(id=1, name="Ende", email="ende@example.com"),
                AclAuthor(id=2, name="Lindgren", email="l@example.com"),
                AclAuthor(id=3, name="Unused", email="u@example.com"),
                AclBook(id=1, title="Momo", cost=4.0, author_id=1,
                        tenant_id=1),
                AclBook(id=2, title="Pippi", cost=3.0, author_id=2,
                        tenant_id=1),
                AclBook(id=3, title="Ronja", cost=3.5, author_id=2,
                        tenant_id=2),
                AclReview(id=1, book_id=1, reviewer="anna"),
                AclReview(id=2, book_id=3, reviewer="carl"),
                AclSecret(id=1, value="secret"),
            ])
            session.commit()
        cls.queries = SqlQueryFactory(
            AclBook, models=MODELS, model_name="AclQuery")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def fetch(self, data: dict, user: User = READER) -> list[dict]:
        query = self.queries.validate(data, user)
        with Session(self.engine) as session:
            return [
                row.model_dump(by_alias=True) for row in query.fetch(session)
            ]

    def assertRejected(self, data: dict, user: User = READER, message=""):
        with self.assertRaises(ValidationError) as context:
            self.queries.validate(data, user)
        self.assertIn(message, str(context.exception))

    def schema_names(self, user: User) -> dict[str, list[str]]:
        schema = self.queries.schema(user)
        models = schema["properties"]["model"]["anyOf"][0]["enum"]
        return {
            model: schema["$defs"][f"AclQuery{model.title()}Field"][
                "properties"]["field"]["enum"]
            for model in models
        }

    def test_schema_per_user(self):
        self.assertEqual(self.schema_names(ADMIN), {
            "book": ["id", "title", "cost", "author_id"],
            "author": ["id", "name", "email"],
            "review": ["id", "book_id", "reviewer"],
            "secret": ["id", "value"],
        })
        self.assertEqual(self.schema_names(READER), {
            "book": ["id", "title", "cost", "author_id"],
            "author": ["id", "name"],
            "review": ["id", "book_id", "reviewer"],
        })

    def test_query_models_are_cached_per_permissions(self):
        self.assertIs(
            self.queries.query_model(READER), self.queries.query_model(OTHER))
        self.assertIsNot(
            self.queries.query_model(READER),
            self.queries.query_model(ADMIN))

    def test_invisible_fields_and_models_are_unknown(self):
        self.assertRejected(
            {"fields": [field("book", "tenant_id")]}, ADMIN,
            "Input should be 'id', 'title', 'cost' or 'author_id'")
        self.assertRejected(
            {"joins": [AUTHOR_JOIN], "fields": [field("author", "email")]},
            message="Input should be 'id' or 'name'")
        self.assertRejected(
            {"model": "secret"},
            message="Input should be 'book', 'author' or 'review'")
        self.assertEqual(
            self.fetch({"model": "secret"}, ADMIN),
            [{"id": 1, "value": "secret"}])

    def test_field_access_per_position(self):
        cost = field("book", "cost")
        self.assertRejected(
            {"fields": [cost]}, message="book.cost must not be used within "
            "fields")
        self.assertRejected(
            {"fields": [{"upper": {"cast": [cost, "string"]}}]},
            message="within fields")
        self.assertRejected(
            {"where": {"gt": [cost, 1]}}, message="within where / on")
        self.assertRejected(
            {"joins": [{**AUTHOR_JOIN, "on": {"gt": [cost, 1]}}]},
            message="within where / on")
        self.assertRejected(
            {"fields": [{"count": "*"}], "group_by": [cost]},
            message="within group_by")
        self.assertRejected(
            {"fields": [{"model": "book", "as": "b", "fields": [cost]}]},
            message="within fields")
        self.assertEqual(self.fetch({"fields": [
            {"sum": cost, "alias": "total"},
            {"max": {"add": [cost, 1]}, "alias": "highest"},
        ]}), [{"total": 7.0, "highest": 5.0}])
        self.assertEqual(
            self.fetch({"fields": [cost], "where": {"gt": [cost, 3]}}, ADMIN),
            [{"cost": 4.0}])

    def test_group_by_alias_requires_group_access(self):
        mapping = {"book": SqlModelAccess(fields={"title": FieldUsage.SELECT})}
        model = SqlQueryModel(
            AclBook, models=MODELS, access=mapping, context=READER,
            model_name="GroupQuery")
        with self.assertRaises(ValidationError) as context:
            model.model_validate({
                "fields": [{**field("book", "title"), "alias": "t"},
                           {"count": "*"}],
                "group_by": [{"ref": "t"}],
            }, context={ACCESS_CONTEXT_KEY: READER})
        self.assertIn("within group_by", str(context.exception))

    def test_select_only_field_in_subquery_filter(self):
        self.assertRejected(
            {"where": exists_review(
                {"eq": [field("review", "reviewer"), "anna"]})},
            ADMIN, "review.reviewer must not be used within where / on")
        self.assertEqual(self.fetch({
            "fields": [field("book", "title"), {
                "model": "review",
                "fields": [{"max": field("review", "id")}],
                "where": {"eq": [field("review", "book_id"),
                                 field("book", "id")]},
                "alias": "review",
            }],
        }), [{"title": "Momo", "review": 1},
             {"title": "Pippi", "review": None}])

    def test_default_fields_only_contain_selectable_fields(self):
        self.assertEqual(self.fetch({}), [
            {"id": 1, "title": "Momo", "author_id": 1},
            {"id": 2, "title": "Pippi", "author_id": 2},
        ])
        model = SqlQueryModel(
            AclBook, models=MODELS, context=READER, model_name="NoSelectQuery",
            access={"book": SqlModelAccess(fields={
                "id": FieldUsage.FILTER, "title": FieldUsage.FILTER,
                "author_id": FieldUsage.FILTER})})
        with self.assertRaises(ValidationError) as context:
            model.model_validate({}, context={ACCESS_CONTEXT_KEY: READER})
        self.assertIn("no fields to select", str(context.exception))

    def test_model_usage(self):
        review_join = {"model": "review", "on": {"eq": [
            field("review", "book_id"), field("book", "id")]}}
        self.assertRejected(
            {"joins": [review_join]}, message="'review' must not be joined")
        self.assertRejected(
            {"model": "review"}, message="'review' must not be queried")
        self.assertEqual(
            self.fetch({"fields": [field("book", "title")],
                        "where": exists_review(True)}),
            [{"title": "Momo"}])
        self.assertEqual(len(self.fetch(
            {"joins": [review_join], "fields": [field("book", "id")]},
            ADMIN)), 1)
        model = SqlQueryModel(
            AclBook, models=MODELS, context=READER, model_name="SubQuery",
            access={"author": SqlModelAccess(usage=ModelUsage.SUBQUERY)})
        with self.assertRaises(ValidationError) as context:
            model.model_validate(
                {"joins": [AUTHOR_JOIN]}, context={ACCESS_CONTEXT_KEY: READER})
        self.assertIn("'author' must not be joined", str(context.exception))

    def test_rows_of_the_queried_model(self):
        data = {"fields": [field("book", "title")]}
        self.assertEqual(
            self.fetch(data, READER), [{"title": "Momo"}, {"title": "Pippi"}])
        self.assertEqual(self.fetch(data, OTHER), [{"title": "Ronja"}])

    def test_rows_in_subqueries_and_correlation(self):
        model = SqlQueryModel(
            AclAuthor, models=MODELS, context=OTHER, model_name="AuthorQuery")
        data = {
            "fields": [field("author", "name"), {
                "model": "book",
                "fields": [{"count": "*"}],
                "where": {"eq": [field("book", "author_id"),
                                 field("author", "id")]},
                "alias": "books",
            }],
            "where": {"exists": {"model": "book", "where": {"eq": [
                field("book", "author_id"), field("author", "id")]}}},
        }
        query = model.model_validate(data, context={ACCESS_CONTEXT_KEY: OTHER})
        with Session(self.engine) as session:
            rows = [row.model_dump() for row in query.fetch(session)]
        self.assertEqual(rows, [{"name": "Lindgren", "books": 1}])

    def test_rows_of_outer_joins_do_not_leak(self):
        model = SqlQueryModel(
            AclAuthor, models=MODELS, context=OTHER, model_name="JoinQuery")
        book_join = {"model": "book", "on": {"eq": [
            field("book", "author_id"), field("author", "id")]}}
        for join_type, expected in (
            ("left", [("Ende", None), ("Lindgren", "Ronja"),
                      ("Unused", None)]),
            ("full", [("Ende", None), ("Lindgren", "Ronja"),
                      ("Unused", None)]),
        ):
            with self.subTest(join_type):
                query = model.model_validate({
                    "joins": [{**book_join, "type": join_type}],
                    "fields": [field("author", "name"),
                               field("book", "title")],
                }, context={ACCESS_CONTEXT_KEY: OTHER})
                with Session(self.engine) as session:
                    rows = sorted(
                        tuple(row.model_dump().values())
                        for row in query.fetch(session)
                    )
                self.assertEqual(rows, expected)

    def test_rules_receive_none_without_context(self):
        received = []

        def usage(context):
            received.append(context)
            return True

        model = SqlQueryModel(
            AclBook, models={"book": AclBook}, model_name="NoContextQuery",
            access={"book": SqlModelAccess(
                usage=usage,
                rows=lambda context, book: book.tenant_id == (
                    2 if context is None else 1),
                fields={"cost": lambda context: context is None},
            )},
        )
        self.assertEqual(received, [None])
        self.assertIn("cost", model.__sql_query_config__.models[
            "book"].references)
        query = model.model_validate(
            {"fields": [field("book", "title"), field("book", "cost")]})
        with Session(self.engine) as session:
            rows = [row.model_dump() for row in query.fetch(session)]
        self.assertEqual(rows, [{"title": "Ronja", "cost": 3.5}])

    def test_rules_of_a_context_type_fail_without_context(self):
        with self.assertRaises(AttributeError):
            SqlQueryModel(AclBook, models=MODELS, model_name="FailQuery")

    def test_base_model_without_access(self):
        with self.assertRaises(SqlAccessDenied):
            SqlQueryModel(
                AclBook, models=MODELS, context=READER,
                access={"book": SqlModelAccess(usage=False)})
        with self.assertRaises(SqlAccessDenied):
            SqlQueryFactory(
                AclSecret, models=MODELS).query_model(READER)

    def test_access_overrides_declarations(self):
        model = SqlQueryModel(
            AclBook, models=MODELS, context=READER, model_name="OverrideQuery",
            access={
                "author": SqlModelAccess(fields={"email": True}),
                "secret": SqlModelAccess(usage=True),
                "book": SqlModelAccess(fields={"title": INVISIBLE}),
            })
        models = model.__sql_query_config__.models
        self.assertIn("email", models["author"].references)
        self.assertIn("secret", models)
        self.assertNotIn("title", models["book"].references)
        self.assertIsNotNone(models["book"].rows)

    def test_rules_of_generated_references(self):
        def generator(context, model, name, field_info, annotation):
            yield from model_references(
                context, model, name, field_info, annotation)
            if context.model_name == "book" and name == "title":
                yield SqlFieldReference(
                    "label", str, lambda book: book.title,
                    result_annotation=str,
                    access=lambda user: user.is_admin)

        queries = SqlQueryFactory(
            AclBook, models={"book": AclBook},
            reference_generators=[generator],
            model_name="GeneratedQuery")
        self.assertNotIn(
            "label", queries.query_model(READER).__sql_query_config__.models[
                "book"].references)
        query = queries.validate({"fields": [field("book", "label")]}, ADMIN)
        with Session(self.engine) as session:
            self.assertEqual(
                [row.label for row in query.fetch(session)],
                ["Momo", "Pippi"])

    def test_invalid_rules(self):
        with self.assertRaises(TypeError):
            SqlQueryModel(
                AclBook, models=MODELS, context=READER,
                model_name="InvalidQuery",
                access={"book": SqlModelAccess(usage=FieldUsage.SELECT)})

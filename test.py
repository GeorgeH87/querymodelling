"""Complete example of typed SQL query models with access rules: data
models, query models per user, JSON schema, validation and SQL generation.
The access rules are typed by the context type ``User``.

Run: python test.py
"""
import json
from dataclasses import dataclass
from typing import Annotated, Optional, Self

from pydantic import ValidationError
from sqlalchemy.orm import registry
from sqlmodel import Field, Session, SQLModel, create_engine

from querymodelling.sql_query import (
    INVISIBLE, FieldUsage, ModelUsage, SqlAccessDenied, SqlFieldAccess,
    SqlModelAccess, SqlQueryFactory, SqlQueryModel
)


# --------------------------------------------------------------------------
# Access context: any object, the access rules receive it directly
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class User:
    name: str
    tenant_id: int
    is_admin: bool = False


ADMIN = User("admin", tenant_id=1, is_admin=True)
READER = User("reader", tenant_id=1)
OTHER_TENANT = User("other", tenant_id=2)


# --------------------------------------------------------------------------
# Data models with access rules (without rules everything is allowed)
# --------------------------------------------------------------------------

class ExampleModel(SQLModel, registry=registry()):
    """Own metadata, keeps the example tables apart from other models."""


class Author(ExampleModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    # only admins may use the email in any way
    email: Annotated[str, SqlFieldAccess[User](lambda user: user.is_admin)]


class Book(ExampleModel, table=True):
    # every user only sees the books of his tenant - in every position:
    # queried model, joins and subqueries
    __sql_access__ = SqlModelAccess(
        rows=lambda user, book:
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    price: float
    # purchase cost: admins may do everything, others only aggregate it
    cost: Annotated[float, SqlFieldAccess[User](lambda user: (
        FieldUsage.ALL if user.is_admin else FieldUsage.AGGREGATE))]
    author_id: Optional[int] = Field(default=None, foreign_key="author.id")
    # never visible, only used by the rows rule
    tenant_id: Annotated[int, SqlFieldAccess(INVISIBLE)]


class Review(ExampleModel, table=True):
    # admins may use reviews everywhere, others only within subqueries
    __sql_access__ = SqlModelAccess[User](
        usage=lambda user: (
            ModelUsage.ALL if user.is_admin else ModelUsage.SUBQUERY),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    book_id: int = Field(foreign_key="book.id")
    rating: int
    # only selectable, filtering would reveal the reviewer indirectly
    reviewer: Annotated[str, SqlFieldAccess(FieldUsage.SELECT)]


class Salary(ExampleModel, table=True):
    # only admins know that this model exists
    __sql_access__ = SqlModelAccess[User](
        usage=lambda user: user.is_admin)

    id: Optional[int] = Field(default=None, primary_key=True)
    author_id: int = Field(foreign_key="author.id")
    amount: float


class Note(ExampleModel, table=True):
    # without a context type the rules receive None as context
    __sql_access__ = SqlModelAccess[None, Self](
        rows=lambda _, note: note.public,
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    text: str
    public: Annotated[bool, SqlFieldAccess(lambda context: context is None)]


# --------------------------------------------------------------------------
# Query models per user (cached per permission signature)
# --------------------------------------------------------------------------

queries = SqlQueryFactory[User](Book, models=[Author, Review, Salary])
engine = create_engine("sqlite://")


def create_data():
    ExampleModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Author(id=1, name="Michael Ende", email="ende@example.com"),
            Author(id=2, name="Astrid Lindgren", email="astrid@example.com"),
            Book(id=1, title="Momo", price=10.0, cost=4.0, author_id=1,
                 tenant_id=1),
            Book(id=2, title="Jim Knopf", price=12.5, cost=5.0,
                 author_id=1, tenant_id=1),
            Book(id=3, title="Pippi", price=8.0, cost=3.0, author_id=2,
                 tenant_id=1),
            Book(id=4, title="Ronja", price=9.0, cost=3.5, author_id=2,
                 tenant_id=2),
            Review(book_id=1, rating=5, reviewer="anna"),
            Review(book_id=1, rating=3, reviewer="ben"),
            Review(book_id=2, rating=4, reviewer="anna"),
            Review(book_id=4, rating=5, reviewer="carl"),
            Salary(author_id=1, amount=5000.0),
            Note(text="visible", public=True),
            Note(text="hidden", public=False),
        ])
        session.commit()


def section(title: str):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def field(model: str, name: str, **extra) -> dict:
    return {"model": model, "field": name, **extra}


def run(user: User, data: dict):
    """Validates the query of the user, prints its SQL and its rows."""
    try:
        query = queries.validate(data, user)
    except ValidationError as error:
        message = error.errors()[0]["msg"].removeprefix("Value error, ")
        print(f"[{user.name}] rejected: {message}")
        return
    print(f"[{user.name}] SQL:\n{query.to_select()}")
    with Session(engine) as session:
        for row in query.fetch(session):
            print(f"  {row.model_dump(by_alias=True)}")


def schema_per_user():
    section("1. Schema per user: models and fields")
    for user in (ADMIN, READER):
        schema = queries.schema(user)
        models = schema["properties"]["model"]["anyOf"][0]["enum"]
        print(f"[{user.name}] models: {models}")
        for model in models:
            definition = schema["$defs"][f"BookSqlQuery{model.title()}Field"]
            print(f"    {model}: {definition['properties']['field']['enum']}")

    same = queries.query_model(READER) is queries.query_model(OTHER_TENANT)
    print(f"\nsame query model for the same permissions: {same}")

    schema = queries.schema(READER)
    print("\nJSON schema of the reader (excerpt):")
    print(json.dumps({
        "properties.model": schema["properties"]["model"],
        "$defs.BookSqlQueryAuthorField": schema["$defs"][
            "BookSqlQueryAuthorField"],
    }, indent=2))


def query_with_join_and_subqueries():
    section("2. Query of the reader: join, functions, subqueries")
    run(READER, {
        "joins": [{
            "model": "author",
            "type": "left",
            "on": {"eq": [field("book", "author_id"), field("author", "id")]},
        }],
        "fields": [
            field("book", "title"),
            {"upper": field("author", "name"), "alias": "author"},
            {
                "model": "review",
                "fields": [{"avg": field("review", "rating")}],
                "where": {"eq": [
                    field("review", "book_id"), field("book", "id")]},
                "alias": "avg_rating",
            },
        ],
        "where": {"and": [
            {"gte": [field("book", "price"), 9]},
            {"exists": {
                "model": "review",
                "where": {"eq": [
                    field("review", "book_id"), field("book", "id")]},
            }},
        ]},
    })


def aggregates():
    section("3. Aggregates: the reader may aggregate the cost")
    run(READER, {
        "joins": [{"model": "author", "on": {
            "eq": [field("book", "author_id"), field("author", "id")]}}],
        "fields": [
            field("author", "name", alias="author"),
            {"count": "*", "alias": "books"},
            {"sum": field("book", "cost"), "alias": "total_cost"},
        ],
        "group_by": [{"ref": "author"}],
    })


def rows_per_tenant():
    section("4. Rows rule: same query, different tenants")
    for user in (READER, OTHER_TENANT):
        run(user, {"fields": [field("book", "title")]})


def denied_queries():
    section("5. Denied for the reader, allowed for the admin")
    author_join = {"model": "author", "on": {
        "eq": [field("book", "author_id"), field("author", "id")]}}
    review_join = {"model": "review", "on": {
        "eq": [field("review", "book_id"), field("book", "id")]}}
    reviewed_by_anna = {"exists": {"model": "review", "where": {
        "eq": [field("review", "reviewer"), "anna"]}}}
    cases = {
        "select the email": {
            "joins": [author_join], "fields": [field("author", "email")]},
        "select the cost": {"fields": [field("book", "cost")]},
        "filter by the cost": {
            "fields": [field("book", "title")],
            "where": {"gt": [field("book", "cost"), 4]}},
        "join reviews": {"joins": [review_join]},
        "filter by the reviewer (denied for everyone)": {
            "fields": [field("book", "title")], "where": reviewed_by_anna},
        "use the salary model": {"model": "salary"},
        "use the tenant (denied for everyone)": {
            "fields": [field("book", "tenant_id")]},
    }
    for case, data in cases.items():
        print(f"\n--- {case}")
        run(READER, data)
        run(ADMIN, data)


def without_context():
    section("6. Without a context: the rules receive None")
    query = SqlQueryModel(Note).model_validate({})
    print(f"SQL:\n{query.to_select()}")
    with Session(engine) as session:
        for row in query.fetch(session):
            print(f"  {row.model_dump()}")


def without_access_to_the_queried_model():
    section("7. A user without access to the queried model")
    try:
        SqlQueryModel(
            Book,
            access={"book": SqlModelAccess[User](
                usage=lambda user: user.is_admin)},
            context=READER,
        )
    except SqlAccessDenied as error:
        print(f"SqlAccessDenied: {error}")


def main():
    create_data()
    schema_per_user()
    query_with_join_and_subqueries()
    aggregates()
    rows_per_tenant()
    denied_queries()
    without_context()
    without_access_to_the_queried_model()


if __name__ == "__main__":
    main()

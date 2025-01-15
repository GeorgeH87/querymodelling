from datetime import datetime
from sqlmodel import Session, select, func
from sqlalchemy.orm.attributes import InstrumentedAttribute
from typing import TypeVar, Sequence, Type, Callable, ParamSpec
from pydantic import AliasChoices

from .base import get_functions, DefaultSort
from .fields import QueryField, SortField
from .model import PageQuery


T = TypeVar("T")
PK = ParamSpec("PK")
Q = TypeVar("Q", bound=PageQuery)

def sortable_by(field):
    def wrapper(order: DefaultSort):
        if order == "desc":
            return field.desc()
        if order == "asc":
            return field.asc()
    return wrapper

def retrieve_entries(
    query: Q,
    session: Session,
    t: Type[T],
    t_index: InstrumentedAttribute
) -> tuple[int, Sequence[T]]:
    search_clause = get_functions(query, "query")
    order_clause = get_functions(query, "sort")
    total_elements = session.exec(select(func.count()).where(
        *search_clause).select_from(t)).first()

    sub_statement = select(t_index)
    if search_clause:
        sub_statement = sub_statement.where(*search_clause)
    if order_clause:
        sub_statement = sub_statement.order_by(*order_clause)
    sub_statement = sub_statement.limit(query.size).offset(
        query.page * query.size)
    sub_query = sub_statement.subquery()
    sub_query_id = sub_query.c.__getattr__(t_index.key)

    statement = select(t).join(
        sub_query, t_index == sub_query_id)

    return total_elements, session.exec(statement).all()

def create_query_fields(
    base_field: any,
    field_name: str,
    annotation: any,
    operator_mapping: dict[str, Callable],
    json_schema_extra: dict
):   
    for operator, operator_function in operator_mapping.items():
        validation_alias = None
        alias = None
        if operator is None:
            name = field_name
        else:
            name = f"{field_name}_{operator}"
            alias = f"{field_name}.{operator}"
            validation_alias = AliasChoices(name, alias)
        yield (
            name,
            QueryField(base_field)(
                operator_function,
                alias=alias,
                validation_alias=validation_alias,
                default=None,
                json_schema_extra=json_schema_extra | {
                    "query.backend": "sql",
                    "query.operator": operator
                }
            ),
            annotation
        )

def create_callback(
    base_field,
    copy_field_properties: list[str] = None,
    schema_extra: dict = None
):
    def auto_create_callback(
        source_type: type,
        field_name: str,
        field,
        field_info,
        annotation
    ):
        if copy_field_properties is None and schema_extra is None:
            json_schema_extra = field_info.json_schema_extra
        json_schema_extra = schema_extra or {}
        if copy_field_properties is not None:
            for property_name in copy_field_properties:
                json_schema_extra[property_name] = field_info[
                    property_name]

        if annotation == str:
            operator_mapping = {
                None: lambda value: field == value,
                "startswith": lambda value: field.like(f"{value}%"),
                "endswith": lambda value: field.like(f"%{value}"),
                "contains": lambda value: field.like(f"%{value}%")
            }
            yield from create_query_fields(
                base_field,
                field_name,
                annotation,
                operator_mapping,
                json_schema_extra
            )
        elif annotation in (datetime, int):
            operator_mapping = {
                None: lambda value: field == value,
                "from": lambda value: field >= value,
                "to": lambda value: field <= value
            }
            yield from create_query_fields(
                base_field,
                field_name,
                annotation,
                operator_mapping,
                json_schema_extra
            ) 
        sort_name = f"sort_{field_name}"
        sort_name_dot = f"sort.{field_name}"
        yield (
            sort_name_dot,
            SortField(base_field)(
                sortable_by(source_type.__dict__[field_name]),
                alias=sort_name_dot,
                validation_alias=AliasChoices(sort_name, sort_name_dot),
                default=None,
                json_schema_extra=json_schema_extra | {
                    "query.backend": "sql"
                }
            ),
            DefaultSort
        )
    return auto_create_callback

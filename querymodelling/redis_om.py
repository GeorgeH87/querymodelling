from datetime import datetime
from typing import TypeVar, Sequence, Type, Callable, ParamSpec

from .__helper__ import is_enum
from .base import get_functions, DefaultSort
from .fields import QueryField, SortField
from .model import PageQuery


T = TypeVar("T")
PK = ParamSpec("PK")
Q = TypeVar("Q", bound=PageQuery)

def sortable_by(field, field_name):
    def wrapper(order: DefaultSort):
        if order == "desc":
            return f"-{field_name}"
        if order == "asc":
            return field_name
    return wrapper

def create_statement(
    query: Q,
    t: Type[T]
):
    search_clause = get_functions(query, "query")
    order_clause = get_functions(query, "sort")
    statement = t.find(*search_clause)
    if order_clause:
        for order_clause in order_clause:
            statement = statement.sort_by(order_clause)
    return statement

def retrieve_paged_entries(
    query: Q,
    t: Type[T],
) -> tuple[int, Sequence[T]]:
    statement = create_statement(query, t)
    
    statement = statement.copy(
        offset=query.page * query.size, limit=query.size)

    entries = statement.execute(exhaust_results=False)
    total_elements = statement.count()
    
    return total_elements, entries

def retrieve_entries(
    query: Q,
    t: Type[T]
) -> tuple[int, Sequence[T]]:
    statement = create_statement(query, t)
    return statement.all()

def create_query_fields(
    base_field: any,
    field_name: str,
    annotation: any,
    operator_mapping: dict[str, Callable],
    json_schema_extra: dict
):   
    for operator, operator_function in operator_mapping.items():
        if operator is None:
            name = field_name
        else:
            name = f"{field_name}_{operator}"
        yield (
            name,
            QueryField(base_field)(
                operator_function,
                default=None,
                json_schema_extra=json_schema_extra | {
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
            json_schema_extra = field_info.json_schema_extra or {}
        else:
            json_schema_extra = schema_extra or {}
        if copy_field_properties is not None:
            for property_name in copy_field_properties:
                json_schema_extra[property_name] = field_info[
                    property_name]

        json_schema_extra = json_schema_extra | {
            "query.backend": "redis",
            "query.field": field_name
        }

        if annotation == str:
            operator_mapping = {
                None: lambda value: field == value,
                "not": lambda value: field != value,
                "startswith": lambda value: field.startswith(value),
                "endswith": lambda value: field.endswith(value),
                "contains": lambda value: field % value
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
                "to": lambda value: field <= value,
                "not": lambda value: field != value
            }
            yield from create_query_fields(
                base_field,
                field_name,
                annotation,
                operator_mapping,
                json_schema_extra
            )
        elif is_enum(annotation):
            operator_mapping = {
                None: lambda value: field == value,
                "not": lambda value: field != value
            }
            yield from create_query_fields(
                base_field,
                field_name,
                annotation,
                operator_mapping,
                json_schema_extra
            )
        yield (
            f"sort_{field_name}",
            SortField(base_field)(
                sortable_by(source_type.__dict__[field_name], field_name),
                default=None,
                json_schema_extra=json_schema_extra
            ),
            DefaultSort
        )
    return auto_create_callback

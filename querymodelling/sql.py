from datetime import datetime
from sqlmodel import Session, select, func
from sqlalchemy.orm.attributes import InstrumentedAttribute
from typing import TypeVar, Sequence, Type, Callable, Concatenate, ParamSpec

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
    return wrapper

def ExtendedTextSearch(BaseField) -> Callable[
        Concatenate[Callable, PK], T]:
    def field_wrapper(field, *args, **kwargs):
        def query(value):
            if value.startswith("startswith:"):
                return field.like(f"{value[11:]}%")
            if value.startswith("endswith:"):
                return field.like(f"%{value[9:]}")
            if value.startswith("contains:"):
                return field.like(f"%{value[9:]}%")
            return field == value
        return QueryField(BaseField)(
            query,
            *args,
            pattern="^(?:(?:startswith:|endswith:|contains))?.+",
            **kwargs
        )
    return field_wrapper

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

def create_callback(base_field):
    def auto_create_callback(
        source_type: type,
        field_name: str,
        field,
        field_info,
        annotation
    ):
        if annotation == str:
            yield (
                field_name,
                ExtendedTextSearch(base_field)(
                    source_type.__dict__[field_name],
                    default=None
                ),
                annotation
            )
        elif annotation in (datetime, int):
            yield (
                f"from_{field_name}",
                QueryField(base_field)(
                    lambda value: source_type.__dict__[field_name] >= value,
                    alias=f"{field_name}.from",
                    default=None
                ),
                annotation
            )
            yield (
                f"to_{field_name}",
                QueryField(base_field)(
                    lambda value: source_type.__dict__[field_name] <= value,
                    alias=f"{field_name}.to",
                    default=None
                ),
                annotation
            )
        yield (
            f"sort_{field_name}",
            SortField(base_field)(
                sortable_by(source_type.__dict__[field_name]),
                alias=f"sort_{field_name}",
                default=None
            ),
            DefaultSort
        )
    return auto_create_callback

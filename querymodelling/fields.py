from pydantic import BaseModel
from typing import TypeVar, ParamSpec, Callable, Concatenate


T = TypeVar("T", bound=BaseModel)
PK = ParamSpec("PK")

def Field(BaseField: Callable[PK, T]):
    def field_wrapper(
        *args,
        query_function = None,
        sort_function = None,
        query_type: str = None,
        json_schema_extra: dict = None,
        **kwargs
    ):
        field_info = BaseField(
            *args,
            query_type=query_type,
            json_schema_extra=(json_schema_extra or {}) | {
                "query.type": query_type
            },
            **kwargs,
        )
        if query_function:
            field_info.metadata.append(("query_function", query_function))
        if sort_function:
            field_info.metadata.append(("sort_function", sort_function))
        return field_info
    return field_wrapper

def QueryField(BaseField: Callable[PK, T]) -> Callable[
        Concatenate[Callable, PK], T]:
    def field_wrapper(
        query_function,
        *args,
        **kwargs
    ):
        field_info = Field(BaseField)(
            *args,
            query_function=query_function,
            query_type="query",
            **kwargs,
        )
        return field_info
    return field_wrapper

def SortField(BaseField: Callable[PK, T]) -> Callable[
        Concatenate[Callable, PK], T]:
    def field_wrapper(
        sort_function,
        *args,
        **kwargs
    ):
        field_info = Field(BaseField)(
            *args,
            sort_function=sort_function,
            query_type="sort",
            **kwargs,
        )
        return field_info
    return field_wrapper

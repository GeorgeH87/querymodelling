from pydantic import BaseModel, create_model, Field
from pydantic.fields import FieldInfo
from typing import (
    TypeVar, Literal, Type, Callable, Dict, Tuple, Any, get_origin, Optional, Union, get_args, Generic, ParamSpecArgs, TypeVarTuple, Unpack, Self
)

from .model import PageQuery


def get_function(field: FieldInfo, function_type: str):
    for meta in field.metadata:
        if isinstance(meta, tuple):
            if meta[0] == f"{function_type}_function":
                return meta[1]

def get_functions(model: BaseModel, function_type: str):
    queries = []
    for name in model.model_fields_set:
        field = model.model_fields[name]
        if function := get_function(field, function_type):
            queries.append(function(getattr(model, name)))
    return queries

A = TypeVar("A", )

def optional_fields(
    base_model: BaseModel,
    exclude_fields: list[str] = None,
    include_fields: list[str] = None,
    additional_fields: Dict[str, Tuple[Type, Any]] = None,
):
    fields = additional_fields or {}
    for name, field in base_model.model_fields.items():
        if (
            isinstance(field, FieldInfo)
            and (not exclude_fields or name not in exclude_fields)
            and (not include_fields or name in include_fields)
        ):
            if (
                get_origin(field.annotation) is Union 
                and type(None) in get_args(field.annotation)
            ):
                annotation = field.annotation
            else:
                annotation = Optional[field.annotation]
            fields[name] = (annotation, None)
    return fields

T = TypeVar("T", bound=BaseModel)
Ts = TypeVarTuple("Ts")
C = TypeVar("C", bound=Union[PageQuery, BaseModel])


def parse_field(input_field: FieldInfo) -> FieldInfo:
    data = {
        key: getattr(input_field, key)
        for key in dir(input_field)
        if not key.startswith("_")
        if key in dir(FieldInfo)
    }
    return Field(**data)

def AutoQueryModel(
    base_models: Type[A] | list[Type[A]],
    base_query_models: Type[C] | list[Type[C]],
    callback: Callable[[type, str, any, FieldInfo], FieldInfo],
    exclude_fields: list[str] = None,
    include_fields: list[str] = None
) -> Type[A]:
    """_summary_

    :param base_models: _description_
    :type base_models: Type[A] | list[Type[A]]
    :param base_query_models: _description_
    :type base_query_models: Type[C] | list[Type[C]]
    :param callback: _description_
    :type callback: Callable[[type, str, any, FieldInfo], FieldInfo]
    :param exclude_fields: _description_, defaults to None
    :type exclude_fields: list[str], optional
    :param include_fields: _description_, defaults to None
    :type include_fields: list[str], optional
    :return: _description_
    :rtype: Type[A]
    """    
    if not isinstance(base_models, list):
        base_models = [base_models]
    if not isinstance(base_query_models, list):
        base_query_models = [base_query_models]

    class_name = ""

    fields = {}
    for base_query_model in base_query_models:
        class_name += base_query_model.__name__
        for name, field in base_query_model.model_fields.items():
            value = base_query_model.__dict__[name]
            if (
                isinstance(field, FieldInfo)
                and (not exclude_fields or name not in exclude_fields)
                and (not include_fields or name in include_fields)
            ):
                pure_annotation = field.annotation
                if (
                    get_origin(field.annotation) is Union
                    and type(None) in get_args(field.annotation)
                ):
                    for annotation in get_args(field.annotation):
                        if annotation is not type(None):
                            pure_annotation = annotation

                generator = callback(
                    base_query_model, name, value, field, pure_annotation)
                if generator:
                    for name, field_info, annotation in generator:
                        fields[name] = (Optional[annotation], field_info)

    model = create_model(
        class_name + "QueryModel",
        __base__=tuple(base_models),
        **fields
    )
    return model

DefaultSort = Literal["asc", "desc"]
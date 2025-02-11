from enum import Enum


def is_enum(annotation):
    try:
        return issubclass(annotation, Enum)
    except TypeError:
        return False

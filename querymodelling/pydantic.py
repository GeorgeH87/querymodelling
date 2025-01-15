from pydantic import Field

from .fields import QueryField as BaseQueryField
from .fields import SortField as BaseSortField


QueryField = BaseQueryField(Field)
SortField = BaseSortField(Field)

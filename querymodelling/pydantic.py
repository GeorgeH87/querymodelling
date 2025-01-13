from pydantic import Field

from .fields import QueryField as BaseQueryField
from .fields import SortField as BaseSortField
from .sql import ExtendedTextSearch as BaseExtendedSqlTextSearch


QueryField = BaseQueryField(Field)
SortField = BaseSortField(Field)

ExtendedSqlTextSearch = BaseExtendedSqlTextSearch(Field)

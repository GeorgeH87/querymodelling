"""Query models per access context (e.g. per user): the access rules are
evaluated per context and the query models are cached per resulting access
signature, so contexts with the same permissions share a query model (and
its JSON schema)."""
from threading import Lock
from typing import Any, Generic, Mapping, Optional, Sequence, Union

from pydantic import BaseModel

from .access import C, SqlModelAccess
from .builder import build_query_model
from .mapping import DEFAULT_SQL_EXPRESSION_MAPPING, SqlExpressionMapping
from .query import ACCESS_CONTEXT_KEY, SqlQuery
from .references import (
    DEFAULT_REFERENCE_GENERATORS, FieldSelection, ReferenceGenerator,
    SqlModelInfo, collect_models
)


def access_signature(infos: dict[str, SqlModelInfo]) -> tuple:
    """Everything of the collected models which affects the query model.
    Rows rules are evaluated per query and are not part of it."""
    return tuple(
        (
            name,
            info.usage,
            tuple(
                (reference, info.references[reference].access)
                for reference in info.references
            ),
        )
        for name, info in infos.items()
    )


class SqlQueryFactory(Generic[C]):
    """Creates query models per access context, see :func:`SqlQueryModel`
    for the parameters::

        queries = SqlQueryFactory[User](Book, models=[Author])
        queries.schema(user)
        query = queries.validate(data, user)
        query.fetch(session)
    """

    def __init__(
        self,
        base_model: type[BaseModel],
        *,
        models: Union[Mapping[str, type], Sequence[type]] = (),
        sources: Optional[Mapping[str, Any]] = None,
        mapping: Optional[SqlExpressionMapping] = None,
        reference_generators: Sequence[ReferenceGenerator] = (
            DEFAULT_REFERENCE_GENERATORS),
        exclude_fields: FieldSelection = None,
        include_fields: FieldSelection = None,
        use_alias_reference: bool = True,
        access: Optional[Mapping[str, SqlModelAccess[C, Any]]] = None,
        max_joins: int = 5,
        max_depth: int = 3,
        model_name: Optional[str] = None,
    ):
        self.base_model = base_model
        self.models = models
        self.sources = sources
        self.mapping = (
            DEFAULT_SQL_EXPRESSION_MAPPING if mapping is None else mapping)
        self.reference_generators = reference_generators
        self.exclude_fields = exclude_fields
        self.include_fields = include_fields
        self.use_alias_reference = use_alias_reference
        self.access = access
        self.max_joins = max_joins
        self.max_depth = max_depth
        self.model_name = model_name or f"{base_model.__name__}SqlQuery"
        self._models: dict[tuple, type[SqlQuery]] = {}
        self._lock = Lock()

    def query_model(self, context: C) -> type[SqlQuery]:
        """The query model of the access context.

        :raises SqlAccessDenied: The base model is not accessible.
        """
        infos, default_model = collect_models(
            self.mapping, self.base_model, self.models, self.sources,
            self.reference_generators, self.exclude_fields,
            self.include_fields, self.use_alias_reference, self.access,
            context,
        )
        signature = access_signature(infos)
        with self._lock:
            if signature not in self._models:
                self._models[signature] = build_query_model(
                    infos, default_model, self.mapping, self.model_name,
                    self.max_joins, self.max_depth,
                )
            return self._models[signature]

    def schema(self, context: C, **kwargs) -> dict[str, Any]:
        """The JSON schema of the query model of the access context."""
        return self.query_model(context).model_json_schema(**kwargs)

    def validate(self, data: Any, context: C) -> SqlQuery:
        """Validates the query within the access context."""
        return self.query_model(context).model_validate(
            data, context={ACCESS_CONTEXT_KEY: context})

    def validate_json(
        self, data: Union[str, bytes], context: C
    ) -> SqlQuery:
        """Validates the JSON query within the access context."""
        return self.query_model(context).model_validate_json(
            data, context={ACCESS_CONTEXT_KEY: context})

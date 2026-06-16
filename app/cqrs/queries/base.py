from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session

from app.cqrs.common import PaginatedResult, PaginationParams
from app.models.base import BaseModel
from app.repositories.base import Repository

ModelT = TypeVar("ModelT", bound=BaseModel[Any])
IdT = TypeVar("IdT")
RepoT = TypeVar("RepoT", bound=Repository[Any, Any])


class BaseQueryHandler(Generic[ModelT, IdT, RepoT]):
    """Read-side CQRS handler with standard lookups by base model fields."""

    def __init__(self, session: Session, repository: RepoT) -> None:
        self._session = session
        self._repository = repository

    @property
    def repository(self) -> RepoT:
        return self._repository

    def get_by_id(self, entity_id: IdT) -> ModelT | None:
        return self._repository.get_by_id(entity_id)

    def exists_by_id(self, entity_id: IdT) -> bool:
        return self.get_by_id(entity_id) is not None

    def list_paginated(self, params: PaginationParams | None = None) -> PaginatedResult[ModelT]:
        pagination = params or PaginationParams()
        items = self._repository.list_all(offset=pagination.offset, limit=pagination.limit)
        total = self._repository.count()
        return PaginatedResult(
            items=items,
            total=total,
            offset=pagination.offset,
            limit=pagination.limit,
        )

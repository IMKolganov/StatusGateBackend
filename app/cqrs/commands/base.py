from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session

from app.models.base import BaseModel
from app.repositories.base import Repository

ModelT = TypeVar("ModelT", bound=BaseModel[Any])
IdT = TypeVar("IdT")
RepoT = TypeVar("RepoT", bound=Repository[Any, Any])


class BaseCommandHandler(Generic[ModelT, IdT, RepoT]):
    """Write-side CQRS handler for create, update and delete operations."""

    def __init__(self, session: Session, repository: RepoT, *, auto_commit: bool = True) -> None:
        self._session = session
        self._repository = repository
        self._auto_commit = auto_commit

    @property
    def repository(self) -> RepoT:
        return self._repository

    def _should_commit(self, commit: bool | None) -> bool:
        return self._auto_commit if commit is None else commit

    def create(self, entity: ModelT, *, commit: bool | None = None) -> ModelT:
        created = self._repository.add(entity)
        if self._should_commit(commit):
            self._repository.commit()
        return created

    def update(self, entity: ModelT, *, commit: bool | None = None) -> ModelT:
        self._session.flush()
        self._session.refresh(entity)
        if self._should_commit(commit):
            self._repository.commit()
        return entity

    def delete(self, entity: ModelT, *, commit: bool | None = None) -> None:
        self._repository.delete(entity)
        if self._should_commit(commit):
            self._repository.commit()

    def commit(self) -> None:
        self._repository.commit()

    def delete_by_id(self, entity_id: IdT, *, commit: bool | None = None) -> bool:
        entity = self._repository.get_by_id(entity_id)
        if entity is None:
            return False

        self._repository.delete(entity)
        if self._should_commit(commit):
            self._repository.commit()
        return True

from typing import Any, Generic, TypeVar

from sqlalchemy.orm import Session

from app.models.base import BaseModel
from app.repositories.base import Repository

ModelT = TypeVar("ModelT", bound=BaseModel[Any])
IdT = TypeVar("IdT")
RepoT = TypeVar("RepoT", bound=Repository[Any, Any])


class BaseCommandHandler(Generic[ModelT, IdT, RepoT]):
    """Write-side CQRS handler for create, update and delete operations."""

    def __init__(self, session: Session, repository: RepoT) -> None:
        self._session = session
        self._repository = repository

    @property
    def repository(self) -> RepoT:
        return self._repository

    def create(self, entity: ModelT) -> ModelT:
        created = self._repository.add(entity)
        self._repository.commit()
        return created

    def update(self, entity: ModelT) -> ModelT:
        self._session.flush()
        self._session.refresh(entity)
        self._repository.commit()
        return entity

    def delete(self, entity: ModelT) -> None:
        self._repository.delete(entity)

    def commit(self) -> None:
        self._repository.commit()

    def delete_by_id(self, entity_id: IdT) -> bool:
        entity = self._repository.get_by_id(entity_id)
        if entity is None:
            return False

        self._repository.delete(entity)
        self._repository.commit()
        return True

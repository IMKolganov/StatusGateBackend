from typing import Any, Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.base import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel[Any])
IdT = TypeVar("IdT")


class Repository(Generic[ModelT, IdT]):
    def __init__(self, session: Session, model: type[ModelT]) -> None:
        self.session = session
        self.model = model

    def get_by_id(self, entity_id: IdT) -> ModelT | None:
        return self.session.get(self.model, entity_id)

    def count(self) -> int:
        stmt = select(func.count()).select_from(self.model)
        return self.session.scalar(stmt) or 0

    def list_all(self, *, offset: int = 0, limit: int = 100) -> list[ModelT]:
        stmt = (
            select(self.model)
            .order_by(self.model.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self.session.scalars(stmt).all())

    def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        self.session.flush()
        self.session.refresh(entity)
        return entity

    def delete(self, entity: ModelT) -> None:
        self.session.delete(entity)
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

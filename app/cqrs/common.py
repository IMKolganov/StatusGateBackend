from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PaginationParams:
    offset: int = 0
    limit: int = 100

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("offset must be >= 0")
        if self.limit < 1 or self.limit > 1000:
            raise ValueError("limit must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class PaginatedResult(Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int

    @property
    def has_next(self) -> bool:
        return self.offset + self.limit < self.total

    @property
    def has_previous(self) -> bool:
        return self.offset > 0

from typing import TypeVar

from pydantic import BaseModel

from app.cqrs.common import PaginatedResult

T = TypeVar("T")
S = TypeVar("S", bound=BaseModel)


def to_paginated_response(result: PaginatedResult[T], mapper) -> dict:
    return {
        "items": [mapper(item) for item in result.items],
        "total": result.total,
        "offset": result.offset,
        "limit": result.limit,
        "has_next": result.has_next,
        "has_previous": result.has_previous,
    }

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiErrorData(BaseModel):
    status_code: int
    detail: str | None = None
    trace_id: str | None = None


class ApiResponse(BaseModel, Generic[T]):
    success: bool
    message: str = ""
    data: T | None = None

    @classmethod
    def success_response(cls, data: T, message: str = "Success") -> "ApiResponse[T]":
        return cls(success=True, message=message, data=data)

    @classmethod
    def error_response(
        cls,
        message: str,
        *,
        status_code: int,
        detail: str | None = None,
        trace_id: str | None = None,
    ) -> "ApiResponse[ApiErrorData | None]":
        return cls(
            success=False,
            message=message,
            data=ApiErrorData(status_code=status_code, detail=detail, trace_id=trace_id),
        )


def is_api_response_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and "success" in payload and "message" in payload

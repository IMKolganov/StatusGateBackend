"""Request/worker-scoped audit context for entity change logs."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

_audit_actor_id: ContextVar[UUID | None] = ContextVar("audit_actor_id", default=None)
_audit_source: ContextVar[str] = ContextVar("audit_source", default="system")
_audit_batch_id: ContextVar[UUID | None] = ContextVar("audit_batch_id", default=None)
_audit_trace_id: ContextVar[str | None] = ContextVar("audit_trace_id", default=None)
_audit_enabled: ContextVar[bool] = ContextVar("audit_enabled", default=True)


@dataclass(frozen=True)
class AuditContext:
    actor_account_id: UUID | None
    source: str
    batch_id: UUID | None
    trace_id: str | None
    enabled: bool


def get_audit_context() -> AuditContext:
    return AuditContext(
        actor_account_id=_audit_actor_id.get(),
        source=_audit_source.get(),
        batch_id=_audit_batch_id.get(),
        trace_id=_audit_trace_id.get(),
        enabled=_audit_enabled.get(),
    )


@contextmanager
def audit_scope(
    *,
    source: str | None = None,
    actor_account_id: UUID | None = None,
    batch_id: UUID | None = None,
    trace_id: str | None = None,
    enabled: bool | None = None,
) -> Iterator[AuditContext]:
    tokens = []
    if source is not None:
        tokens.append((_audit_source, _audit_source.set(source)))
    if actor_account_id is not None:
        tokens.append((_audit_actor_id, _audit_actor_id.set(actor_account_id)))
    if batch_id is not None:
        tokens.append((_audit_batch_id, _audit_batch_id.set(batch_id)))
    if trace_id is not None:
        tokens.append((_audit_trace_id, _audit_trace_id.set(trace_id)))
    if enabled is not None:
        tokens.append((_audit_enabled, _audit_enabled.set(enabled)))
    try:
        yield get_audit_context()
    finally:
        for var, token in reversed(tokens):
            var.reset(token)

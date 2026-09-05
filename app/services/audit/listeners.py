"""SQLAlchemy listeners that append entity_change_logs for config entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import event, inspect
from sqlalchemy.orm import Mapper, Session, UOWTransaction

from app.models.account import Account
from app.models.component_group import ComponentGroup
from app.models.component_kind import ComponentKind
from app.models.datagate_integration import DatagateIntegration
from app.models.entity_change_log import EntityChangeLog
from app.models.incident import Incident
from app.models.incident_update import IncidentUpdate
from app.models.monitored_component import MonitoredComponent
from app.models.monitoring_settings import MonitoringSettings
from app.models.project import Project
from app.services.audit.context import get_audit_context

_AUDITED_TYPES: dict[type, str] = {
    Project: "project",
    ComponentKind: "component_kind",
    ComponentGroup: "component_group",
    MonitoredComponent: "monitored_component",
    DatagateIntegration: "datagate_integration",
    Incident: "incident",
    IncidentUpdate: "incident_update",
    MonitoringSettings: "monitoring_settings",
    Account: "account",
}

_SECRET_KEYS = frozenset({"client_secret", "password_hash", "totp_secret", "email_verification_token"})
_SENSITIVE_PAYLOAD_KEYS = frozenset({"check_config", "config_text"})
_SKIP_KEYS = frozenset({"created_at", "updated_at"})

_pending: dict[int, list[EntityChangeLog]] = {}


def clear_pending_for_session(session: Session) -> None:
    """Drop queued audit rows for a session (failed flush / rollback)."""
    _pending.pop(id(session), None)


def _entity_type(obj: object) -> str | None:
    return _AUDITED_TYPES.get(type(obj))


def _entity_id(obj: object) -> str:
    if isinstance(obj, DatagateIntegration):
        return str(obj.project_id)
    if isinstance(obj, MonitoringSettings):
        return str(getattr(obj, "id", "singleton"))
    return str(getattr(obj, "id", ""))


def _project_id(obj: object) -> UUID | None:
    if isinstance(obj, Project):
        return obj.id
    if isinstance(obj, DatagateIntegration):
        return obj.project_id
    value = getattr(obj, "project_id", None)
    return value if isinstance(value, UUID) else None


def _redact_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in _SECRET_KEYS or key in _SENSITIVE_PAYLOAD_KEYS:
        if key == "check_config" and isinstance(value, dict):
            return {"redacted": True, "keys": sorted(value.keys())}
        return "***"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _serialize(obj: object) -> dict[str, Any]:
    state = inspect(obj)
    data: dict[str, Any] = {}
    for attr in state.mapper.column_attrs:
        key = attr.key
        if key in _SKIP_KEYS:
            continue
        data[key] = _redact_value(key, getattr(obj, key, None))
    return data


def _diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    if before is None and after is None:
        return None
    if before is None:
        return {"added": after}
    if after is None:
        return {"removed": before}
    changed = {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
    return changed or None


def _action_for(obj: object, default: str) -> str:
    if default == "update" and isinstance(obj, MonitoredComponent):
        hist = inspect(obj).attrs.is_active.history
        if hist.has_changes() and obj.is_active is False and True in (hist.deleted or []):
            return "deactivate"
    return default


def _queue(session: Session, entry: EntityChangeLog) -> None:
    _pending.setdefault(id(session), []).append(entry)


def _before_flush(session: Session, _flush_context: UOWTransaction, _instances: Any) -> None:
    # Always start a fresh queue for this flush so a prior failed flush cannot leak.
    _pending[id(session)] = []

    ctx = get_audit_context()
    if not ctx.enabled:
        return

    # Avoid FK violations: do not reference projects that are deleted in this unit of work.
    deleted_project_ids = {obj.id for obj in session.deleted if isinstance(obj, Project)}

    def project_id_for(obj: object) -> UUID | None:
        pid = _project_id(obj)
        if pid is not None and pid in deleted_project_ids:
            return None
        return pid

    for obj in session.new:
        entity_type = _entity_type(obj)
        if entity_type is None or isinstance(obj, EntityChangeLog):
            continue
        after = _serialize(obj)
        _queue(
            session,
            EntityChangeLog(
                actor_account_id=ctx.actor_account_id,
                source=ctx.source,
                batch_id=ctx.batch_id,
                entity_type=entity_type,
                entity_id=_entity_id(obj),
                project_id=project_id_for(obj),
                action="create",
                before=None,
                after=after,
                diff=_diff(None, after),
                trace_id=ctx.trace_id,
                request_id=ctx.trace_id,
            ),
        )

    for obj in session.dirty:
        entity_type = _entity_type(obj)
        if entity_type is None or isinstance(obj, EntityChangeLog):
            continue
        if not session.is_modified(obj, include_collections=False):
            continue
        state = inspect(obj)
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for attr in state.mapper.column_attrs:
            key = attr.key
            if key in _SKIP_KEYS:
                continue
            hist = state.attrs[key].history
            current = getattr(obj, key, None)
            if hist.has_changes():
                old = hist.deleted[0] if hist.deleted else None
                before[key] = _redact_value(key, old)
                after[key] = _redact_value(key, current)
        if not before and not after:
            continue
        action = _action_for(obj, "update")
        _queue(
            session,
            EntityChangeLog(
                actor_account_id=ctx.actor_account_id,
                source=ctx.source,
                batch_id=ctx.batch_id,
                entity_type=entity_type,
                entity_id=_entity_id(obj),
                project_id=project_id_for(obj),
                action=action,
                before=before or None,
                after=after or None,
                diff=_diff(before or None, after or None),
                trace_id=ctx.trace_id,
                request_id=ctx.trace_id,
            ),
        )

    for obj in session.deleted:
        entity_type = _entity_type(obj)
        if entity_type is None or isinstance(obj, EntityChangeLog):
            continue
        before = _serialize(obj)
        _queue(
            session,
            EntityChangeLog(
                actor_account_id=ctx.actor_account_id,
                source=ctx.source,
                batch_id=ctx.batch_id,
                entity_type=entity_type,
                entity_id=_entity_id(obj),
                project_id=project_id_for(obj),
                action="delete",
                before=before,
                after=None,
                diff=_diff(before, None),
                trace_id=ctx.trace_id,
                request_id=ctx.trace_id,
            ),
        )


def _after_flush(session: Session, _flush_context: UOWTransaction) -> None:
    entries = _pending.pop(id(session), [])
    for entry in entries:
        session.add(entry)


def _after_rollback(session: Session, _transaction: Any = None) -> None:
    clear_pending_for_session(session)


def register_audit_listeners() -> None:
    if getattr(register_audit_listeners, "_registered", False):
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_flush", _after_flush)
    event.listen(Session, "after_rollback", _after_rollback)
    event.listen(Session, "after_soft_rollback", _after_rollback)
    register_audit_listeners._registered = True  # type: ignore[attr-defined]


# Silence unused Mapper import for type checkers that expect it nearby events.
_ = Mapper

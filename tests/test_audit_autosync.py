"""Unit tests for audit serialization helpers, pending flush safety, and auto-sync errors."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.component_kind import OPENVPN_COMPONENT_KIND_ID
from app.models.datagate_integration import DatagateIntegration
from app.models.monitored_component import MonitoredComponent
from app.schemas.datagate import DatagateImportRequest
from app.services.audit import listeners
from app.services.audit.listeners import _before_flush, _redact_value, _serialize, clear_pending_for_session
from app.services.datagate.client import DataGateApiError
from app.services.datagate.import_service import DatagateIntegrationService, public_error_message
from app.worker import __main__ as worker_main


def test_serialize_redacts_client_secret():
    integration = DatagateIntegration(
        project_id=uuid4(),
        base_url="https://api.datagateapp.com",
        client_id="cid",
        client_secret="super-secret-value",
        monitor_cn_prefix="statusgate",
    )
    data = _serialize(integration)
    assert data["client_secret"] == "***"
    assert "super-secret-value" not in str(data)


def test_serialize_redacts_check_config():
    component = MonitoredComponent(
        id=uuid4(),
        project_id=uuid4(),
        component_kind_id=OPENVPN_COMPONENT_KIND_ID,
        name="VPN",
        slug="vpn",
        check_url="https://probe.example",
        check_type="openvpn",
        check_config={"config_text": "-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----"},
        timeout_seconds=60,
    )
    data = _serialize(component)
    assert data["check_config"] == {"redacted": True, "keys": ["config_text"]}
    assert "PRIVATE KEY" not in str(data)
    assert "SECRET" not in str(data)


def test_redact_value_config_text():
    assert _redact_value("config_text", "client\n...") == "***"


def test_before_flush_resets_stale_pending():
    session = MagicMock()
    session.new = []
    session.dirty = []
    session.deleted = []
    stale = MagicMock()
    listeners._pending[id(session)] = [stale]
    _before_flush(session, None, None)
    assert listeners._pending[id(session)] == []


def test_after_soft_rollback_accepts_transaction_arg():
    session = MagicMock()
    listeners._pending[id(session)] = [MagicMock()]
    listeners._after_rollback(session, object())
    assert id(session) not in listeners._pending


def test_clear_pending_for_session():
    session = MagicMock()
    listeners._pending[id(session)] = [MagicMock()]
    clear_pending_for_session(session)
    assert id(session) not in listeners._pending


def test_auto_sync_payload_never_sets_delete_removed():
    session = MagicMock()
    service = DatagateIntegrationService(session)
    project_id = uuid4()
    integration = DatagateIntegration(
        project_id=project_id,
        base_url="https://api.datagateapp.com",
        client_id="cid",
        client_secret="enc",
        monitor_cn_prefix="statusgate",
        is_enabled=True,
        auto_sync_import_new=True,
        auto_sync_deactivate_removed=True,
    )
    service.require_integration = MagicMock(return_value=integration)  # type: ignore[method-assign]
    captured: dict = {}

    def fake_import(pid, payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return MagicMock(batch_id=uuid4(), created=0, updated=0, errors=0)

    service.import_servers = fake_import  # type: ignore[method-assign]
    service.run_auto_sync(project_id)
    payload: DatagateImportRequest = captured["payload"]
    assert payload.delete_removed is False
    assert payload.import_new is True
    assert payload.deactivate_removed is True
    assert payload.sync_names is True
    assert payload.refresh_configs is True
    assert captured["kwargs"]["source"] == "worker"
    assert captured["kwargs"]["record_sync_status"] is True


def test_import_request_delete_wins():
    payload = DatagateImportRequest(deactivate_removed=True, delete_removed=True)
    assert payload.delete_removed is True
    assert payload.deactivate_removed is False


def test_public_error_message_datagate_and_generic():
    assert "boom" in public_error_message(DataGateApiError("boom"))
    assert public_error_message(HTTPException(status_code=404, detail="missing")) == "missing"
    assert "ValueError" in public_error_message(ValueError()) or public_error_message(ValueError()) == "ValueError"


def test_should_reset_vpn_probe_url():
    from app.services.datagate.import_service import _should_reset_vpn_probe_url

    assert _should_reset_vpn_probe_url(None, "https://s1-nor.datagateapp.com/") is True
    assert _should_reset_vpn_probe_url("https://s1-nor.datagateapp.com/", "https://s1-nor.datagateapp.com/") is True
    assert _should_reset_vpn_probe_url("https://s1-nor.datagateapp.com", "https://s1-nor.datagateapp.com/") is True
    assert _should_reset_vpn_probe_url("https://ifconfig.me/ip", "https://s1-nor.datagateapp.com/") is False


def test_import_servers_unexpected_error_returns_http_500():
    session = MagicMock()
    service = DatagateIntegrationService(session)
    service._safe_rollback = MagicMock()  # type: ignore[method-assign]
    service._persist_sync_failure = MagicMock()  # type: ignore[method-assign]
    service._import_servers_inner = MagicMock(side_effect=RuntimeError("explode"))  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc_info:
        service.import_servers(uuid4(), DatagateImportRequest(), record_sync_status=True)

    assert exc_info.value.status_code == 500
    assert "explode" in str(exc_info.value.detail)
    service._safe_rollback.assert_called_once()
    service._persist_sync_failure.assert_called_once()


def test_import_servers_datagate_api_error_returns_http_502():
    session = MagicMock()
    service = DatagateIntegrationService(session)
    service._safe_rollback = MagicMock()  # type: ignore[method-assign]
    service._import_servers_inner = MagicMock(  # type: ignore[method-assign]
        side_effect=DataGateApiError("upstream down", status_code=503)
    )

    with pytest.raises(HTTPException) as exc_info:
        service.import_servers(uuid4(), DatagateImportRequest())

    assert exc_info.value.status_code == 502
    assert "upstream down" in str(exc_info.value.detail)
    service._safe_rollback.assert_called_once()


def test_preview_wraps_unexpected_errors():
    session = MagicMock()
    service = DatagateIntegrationService(session)
    service.require_integration = MagicMock(side_effect=RuntimeError("broken"))  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc_info:
        service.preview(uuid4())

    assert exc_info.value.status_code == 500
    assert "preview failed" in str(exc_info.value.detail).lower()


def test_test_connection_wraps_datagate_api_error():
    session = MagicMock()
    service = DatagateIntegrationService(session)
    service.require_integration = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
    service._client = MagicMock(side_effect=DataGateApiError("auth failed", status_code=401))  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc_info:
        service.test_connection(uuid4())

    assert exc_info.value.status_code == 502
    assert "auth failed" in str(exc_info.value.detail)


def test_list_servers_wraps_unexpected_errors():
    session = MagicMock()
    service = DatagateIntegrationService(session)
    service.require_integration = MagicMock(side_effect=RuntimeError("nope"))  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc_info:
        service.list_servers(uuid4())

    assert exc_info.value.status_code == 500
    assert "list" in str(exc_info.value.detail).lower()


def test_after_flush_does_not_replay_cleared_pending():
    session = MagicMock()
    listeners._pending[id(session)] = [MagicMock()]
    clear_pending_for_session(session)
    listeners._after_flush(session, None)
    session.add.assert_not_called()


def test_worker_autosync_failure_uses_fresh_session_and_rollback():
    project_id = uuid4()
    sync_session = MagicMock()
    err_session = MagicMock()
    sessions = iter([sync_session, err_session])

    class _Ctx:
        def __init__(self, session):
            self.session = session

        def __enter__(self):
            return self.session

        def __exit__(self, *args):
            return False

    def session_factory():
        return _Ctx(next(sessions))

    failing_service = MagicMock()
    failing_service.run_auto_sync.side_effect = RuntimeError("sync boom")

    ok_integration = DatagateIntegration(
        project_id=project_id,
        base_url="https://api.datagateapp.com",
        client_id="cid",
        client_secret="enc",
        monitor_cn_prefix="statusgate",
    )
    recovery_service = MagicMock()
    recovery_service.get_integration.return_value = ok_integration

    audit_cm = MagicMock()
    audit_cm.__enter__ = MagicMock(return_value=None)
    audit_cm.__exit__ = MagicMock(return_value=False)

    with (
        patch.object(worker_main, "SessionLocal", side_effect=session_factory),
        patch.object(
            worker_main,
            "DatagateIntegrationService",
            side_effect=[failing_service, recovery_service],
        ),
        patch.object(worker_main, "clear_pending_for_session") as clear_pending,
        patch.object(worker_main, "audit_scope", return_value=audit_cm),
    ):
        worker_main._run_one_autosync(project_id)

    sync_session.rollback.assert_called_once()
    clear_pending.assert_called_once_with(sync_session)
    assert ok_integration.last_sync_status == "error"
    assert "sync boom" in (ok_integration.last_sync_error or "")
    recovery_service._integration_commands.save.assert_called_once()

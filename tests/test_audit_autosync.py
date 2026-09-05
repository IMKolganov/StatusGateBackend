"""Unit tests for audit serialization helpers and auto-sync policy."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from app.models.datagate_integration import DatagateIntegration
from app.schemas.datagate import DatagateImportRequest
from app.services.audit.listeners import _serialize
from app.services.datagate.import_service import DatagateIntegrationService


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

"""API / import_service tests for DataGate integration (mocked outbound HTTP)."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.component_kind import OPENVPN_COMPONENT_KIND_ID
from app.models.datagate_integration import DatagateIntegration
from app.models.monitored_component import MonitoredComponent
from app.models.project import Project
from app.schemas.datagate import DatagateImportRequest
from app.services.datagate.client import DataGateApiError, DataGateServer
from app.services.datagate.import_service import DatagateIntegrationService
from app.services.datagate.secrets import decrypt_client_secret, encrypt_client_secret, is_encrypted_secret


def _data(response):
    body = response.json()
    assert body["success"] is True, body
    return body["data"]


def _create_project(client: TestClient, slug: str = "datagate") -> dict:
    response = client.post(
        "/api/admin/projects",
        json={"name": "DataGate", "slug": slug, "description": None, "is_active": True},
    )
    assert response.status_code == 201, response.text
    return _data(response)


class TestDatagateIntegrationApi:
    def test_upsert_masks_secret_and_rejects_bad_host(
        self,
        client: TestClient,
        admin_headers: dict,
        db_session: Session,
    ) -> None:
        project = _create_project(client)
        bad = client.put(
            f"/api/admin/projects/{project['id']}/datagate",
            json={
                "base_url": "https://evil.example",
                "client_id": "cid",
                "client_secret": "super-secret",
                "monitor_cn_prefix": "statusgate",
                "is_enabled": True,
            },
        )
        assert bad.status_code == 422

        ok = client.put(
            f"/api/admin/projects/{project['id']}/datagate",
            json={
                "base_url": "https://api.datagateapp.com",
                "client_id": "cid",
                "client_secret": "super-secret",
                "monitor_cn_prefix": "statusgate",
                "is_enabled": True,
            },
        )
        assert ok.status_code == 200, ok.text
        body = _data(ok)
        assert body["client_id"] == "cid"
        assert body["client_secret_set"] is True
        assert "client_secret" not in body
        assert "super-secret" not in ok.text

        stored = db_session.get(DatagateIntegration, project["id"])
        assert stored is not None
        assert is_encrypted_secret(stored.client_secret)
        assert "super-secret" not in stored.client_secret
        assert decrypt_client_secret(stored.client_secret) == "super-secret"

        got = client.get(f"/api/admin/projects/{project['id']}/datagate")
        assert got.status_code == 200
        again = _data(got)
        assert again["client_secret_set"] is True
        assert "super-secret" not in got.text

    def test_import_requires_configured_integration(self, client: TestClient, admin_headers: dict) -> None:
        project = _create_project(client, slug="no-integration")
        response = client.post(
            f"/api/admin/projects/{project['id']}/datagate/import",
            json={"sync_names": True, "refresh_configs": True, "import_new": True},
        )
        assert response.status_code == 404


class TestDatagateImportService:
    def test_partial_import_does_not_reassign_linked_component(self, db_session: Session) -> None:
        project = Project(name="DataGate", slug="dg-relink", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()
        db_session.add(
            DatagateIntegration(
                project_id=project.id,
                base_url="https://api.datagateapp.com",
                client_id="cid",
                client_secret=encrypt_client_secret("sec"),
            )
        )
        local = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Helsinki 1 openvpn tcp",
            slug="helsinki-1",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto tcp\nremote hel.example.com 443\n"},
            timeout_seconds=60,
            connection_mode="persistent",
            datagate_server_id=1,
            datagate_common_name="statusgate-dg-relink-1",
        )
        db_session.add(local)
        db_session.commit()

        selected = DataGateServer(
            id=2,
            server_type=0,
            server_name="Helsinki 1",
            host="hel.example.com",
            port=443,
            proto="tcp",
            api_url="https://b.example/",
        )
        mock_client = MagicMock()
        mock_client.list_servers.return_value = [
            DataGateServer(id=1, server_type=0, server_name="Helsinki old", host="hel.example.com", proto="tcp"),
            selected,
        ]
        mock_client.enrich_server.side_effect = lambda s: s
        mock_client.ensure_config_text.return_value = "client\nproto tcp\n"

        service = DatagateIntegrationService(db_session)
        service._client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

        result = service.import_servers(
            project.id,
            DatagateImportRequest(
                sync_names=True,
                refresh_configs=True,
                import_new=True,
                server_ids=[2],
            ),
        )
        assert result.updated == 0
        assert result.created == 1
        db_session.refresh(local)
        assert local.datagate_server_id == 1
        created = db_session.query(MonitoredComponent).filter_by(datagate_server_id=2).one()
        assert created.id != local.id

    def test_partial_import_matches_only_selected_servers(self, db_session: Session) -> None:
        project = Project(name="DataGate", slug="dg", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()
        db_session.add(
            DatagateIntegration(
                project_id=project.id,
                base_url="https://api.datagateapp.com",
                client_id="cid",
                client_secret=encrypt_client_secret("sec"),
            )
        )
        local = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Helsinki 1 openvpn tcp",
            slug="helsinki-1",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto tcp\nremote hel.example.com 443\n"},
            timeout_seconds=60,
            connection_mode="persistent",
        )
        db_session.add(local)
        db_session.commit()

        competing = DataGateServer(
            id=1,
            server_type=0,
            server_name="Helsinki 1",
            host="hel.example.com",
            port=443,
            proto="tcp",
            api_url="https://a.example/",
        )
        selected = DataGateServer(
            id=2,
            server_type=0,
            server_name="Helsinki 1",
            host="hel.example.com",
            port=443,
            proto="tcp",
            api_url="https://b.example/",
        )

        mock_client = MagicMock()
        mock_client.list_servers.return_value = [competing, selected]
        mock_client.enrich_server.side_effect = lambda s: s
        mock_client.ensure_config_text.return_value = "client\nproto tcp\n"

        service = DatagateIntegrationService(db_session)
        service._client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

        result = service.import_servers(
            project.id,
            DatagateImportRequest(
                sync_names=True,
                refresh_configs=True,
                import_new=True,
                server_ids=[2],
            ),
        )
        assert result.errors == 0
        assert result.updated == 1
        assert result.created == 0
        db_session.refresh(local)
        assert local.datagate_server_id == 2
        assert local.check_config["config_text"] == "client\nproto tcp\n"

    def test_import_error_does_not_persist_partial_link(self, db_session: Session) -> None:
        project = Project(name="DataGate", slug="dg2", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()
        db_session.add(
            DatagateIntegration(
                project_id=project.id,
                base_url="https://api.datagateapp.com",
                client_id="cid",
                client_secret=encrypt_client_secret("sec"),
            )
        )
        local = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Old Name",
            slug="norway-1",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto tcp\nremote n1.example.com 443\n"},
            timeout_seconds=60,
            connection_mode="ephemeral",
            datagate_server_id=None,
        )
        db_session.add(local)
        db_session.commit()
        original_name = local.name

        server = DataGateServer(
            id=9,
            server_type=0,
            server_name="Norway 1",
            host="n1.example.com",
            port=443,
            proto="tcp",
        )
        mock_client = MagicMock()
        mock_client.list_servers.return_value = [server]
        mock_client.enrich_server.side_effect = lambda s: s
        mock_client.ensure_config_text.side_effect = DataGateApiError("boom", status_code=503)

        service = DatagateIntegrationService(db_session)
        service._client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

        result = service.import_servers(
            project.id,
            DatagateImportRequest(sync_names=True, refresh_configs=True, import_new=False, server_ids=[9]),
        )
        assert result.errors == 1
        assert result.updated == 0
        db_session.refresh(local)
        assert local.name == original_name
        assert local.datagate_server_id is None

    def test_import_creates_new_server(self, db_session: Session) -> None:
        project = Project(name="DataGate", slug="dg3", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()
        db_session.add(
            DatagateIntegration(
                project_id=project.id,
                base_url="https://api.datagateapp.com",
                client_id="cid",
                client_secret=encrypt_client_secret("sec"),
            )
        )
        db_session.commit()

        server = DataGateServer(
            id=42,
            server_type=0,
            server_name="Cyprus",
            host="cy.example.com",
            proto="udp",
            api_url="https://cy.example.com:9443/",
        )
        mock_client = MagicMock()
        mock_client.list_servers.return_value = [server]
        mock_client.enrich_server.side_effect = lambda s: s
        mock_client.ensure_config_text.return_value = "client\nproto udp\n"

        service = DatagateIntegrationService(db_session)
        service._client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

        result = service.import_servers(
            project.id,
            DatagateImportRequest(sync_names=False, refresh_configs=True, import_new=True),
        )
        assert result.created == 1
        assert result.errors == 0
        created = db_session.query(MonitoredComponent).filter_by(datagate_server_id=42).one()
        assert created.name == "Cyprus"
        assert created.check_type == "openvpn"
        assert created.connection_mode == "persistent"
        assert created.datagate_common_name == "statusgate-dg3-42"

    def test_preview_removed_local_excludes_never_linked_and_active_links(self, db_session: Session) -> None:
        project = Project(name="DataGate", slug="dg-preview", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()
        db_session.add(
            DatagateIntegration(
                project_id=project.id,
                base_url="https://api.datagateapp.com",
                client_id="cid",
                client_secret=encrypt_client_secret("sec"),
            )
        )
        kept = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Cyprus",
            slug="cyprus",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto udp\nremote cy.example.com 1194\n"},
            timeout_seconds=60,
            connection_mode="persistent",
            datagate_server_id=1,
        )
        removed = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Helsinki 2",
            slug="helsinki-2",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto udp\nremote old.example.com 1194\n"},
            timeout_seconds=60,
            connection_mode="persistent",
            datagate_server_id=87,
        )
        never_linked = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Manual VPN",
            slug="manual-vpn",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto udp\nremote manual.example.com 1194\n"},
            timeout_seconds=60,
            connection_mode="persistent",
        )
        db_session.add_all([kept, removed, never_linked])
        db_session.commit()

        live = DataGateServer(
            id=1,
            server_type=0,
            server_name="Cyprus",
            host="cy.example.com",
            port=1194,
            proto="udp",
        )
        mock_client = MagicMock()
        mock_client.list_servers.return_value = [live]
        mock_client.enrich_server.side_effect = lambda s: s

        service = DatagateIntegrationService(db_session)
        service._client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

        preview = service.preview(project.id)
        assert [c.slug for c in preview.removed_local] == ["helsinki-2"]
        assert {c.slug for c in preview.unmatched_local} == {"manual-vpn"}
        assert len(preview.matched) == 1

    def test_deactivate_removed_keeps_history(self, db_session: Session) -> None:
        from app.models.check_result import CheckResult

        project = Project(name="DataGate", slug="dg-deact", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()
        db_session.add(
            DatagateIntegration(
                project_id=project.id,
                base_url="https://api.datagateapp.com",
                client_id="cid",
                client_secret=encrypt_client_secret("sec"),
            )
        )
        local = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Helsinki 2",
            slug="helsinki-2",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto udp\nremote old.example.com 1194\n"},
            timeout_seconds=60,
            connection_mode="persistent",
            is_active=True,
            datagate_server_id=87,
            datagate_common_name="statusgate-dg-deact-87",
        )
        db_session.add(local)
        db_session.flush()
        history = CheckResult(
            monitored_component_id=local.id,
            outcome="up",
            latency_ms=12,
        )
        db_session.add(history)
        db_session.commit()
        history_id = history.id

        mock_client = MagicMock()
        mock_client.list_servers.return_value = []
        mock_client.enrich_server.side_effect = lambda s: s

        service = DatagateIntegrationService(db_session)
        service._client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

        result = service.import_servers(
            project.id,
            DatagateImportRequest(
                sync_names=False,
                refresh_configs=False,
                import_new=False,
                deactivate_removed=True,
                server_ids=[],
            ),
        )
        assert result.deactivated == 1
        assert result.deleted == 0
        db_session.refresh(local)
        assert local.is_active is False
        assert local.datagate_server_id == 87
        assert db_session.get(CheckResult, history_id) is not None

    def test_delete_removed_cascades_history(self, db_session: Session) -> None:
        from app.models.check_result import CheckResult

        project = Project(name="DataGate", slug="dg-del", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()
        db_session.add(
            DatagateIntegration(
                project_id=project.id,
                base_url="https://api.datagateapp.com",
                client_id="cid",
                client_secret=encrypt_client_secret("sec"),
            )
        )
        local = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Poland 1",
            slug="poland-1",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto udp\nremote pl.example.com 1194\n"},
            timeout_seconds=60,
            connection_mode="persistent",
            datagate_server_id=90,
        )
        db_session.add(local)
        db_session.flush()
        history = CheckResult(
            monitored_component_id=local.id,
            outcome="up",
            latency_ms=9,
        )
        db_session.add(history)
        db_session.commit()
        component_id = local.id
        history_id = history.id

        mock_client = MagicMock()
        mock_client.list_servers.return_value = []
        mock_client.enrich_server.side_effect = lambda s: s

        service = DatagateIntegrationService(db_session)
        service._client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

        result = service.import_servers(
            project.id,
            DatagateImportRequest(
                sync_names=False,
                refresh_configs=False,
                import_new=False,
                deactivate_removed=True,
                delete_removed=True,
                server_ids=[],
            ),
        )
        assert result.deleted == 1
        assert result.deactivated == 0
        assert db_session.get(MonitoredComponent, component_id) is None
        assert db_session.get(CheckResult, history_id) is None

    def test_partial_import_does_not_treat_unselected_link_as_removed(self, db_session: Session) -> None:
        project = Project(name="DataGate", slug="dg-partial-rm", description=None, is_active=True)
        db_session.add(project)
        db_session.flush()
        db_session.add(
            DatagateIntegration(
                project_id=project.id,
                base_url="https://api.datagateapp.com",
                client_id="cid",
                client_secret=encrypt_client_secret("sec"),
            )
        )
        local = MonitoredComponent(
            project_id=project.id,
            component_kind_id=OPENVPN_COMPONENT_KIND_ID,
            name="Norway 1",
            slug="norway-1",
            check_url="https://probe.example",
            check_type="openvpn",
            check_config={"config_text": "proto udp\nremote n1.example.com 1194\n"},
            timeout_seconds=60,
            connection_mode="persistent",
            is_active=True,
            datagate_server_id=75,
        )
        db_session.add(local)
        db_session.commit()

        linked = DataGateServer(
            id=75,
            server_type=0,
            server_name="Norway 1",
            host="n1.example.com",
            port=1194,
            proto="udp",
        )
        other = DataGateServer(
            id=94,
            server_type=0,
            server_name="Norway 2",
            host="n2.example.com",
            port=1194,
            proto="udp",
        )
        mock_client = MagicMock()
        mock_client.list_servers.return_value = [linked, other]
        mock_client.enrich_server.side_effect = lambda s: s
        mock_client.ensure_config_text.return_value = "client\nproto udp\n"

        service = DatagateIntegrationService(db_session)
        service._client = MagicMock(return_value=mock_client)  # type: ignore[method-assign]

        result = service.import_servers(
            project.id,
            DatagateImportRequest(
                sync_names=False,
                refresh_configs=False,
                import_new=False,
                deactivate_removed=True,
                server_ids=[94],
            ),
        )
        assert result.deactivated == 0
        assert result.deleted == 0
        db_session.refresh(local)
        assert local.is_active is True
        assert local.datagate_server_id == 75


class TestDatagateRemovedAuth:
    def test_operator_cannot_delete_removed(self, client: TestClient, admin_headers: dict) -> None:
        created = _data(
            client.post(
                "/api/auth/register",
                json={"email": "dg-operator@example.com", "password": "password123"},
            )
        )
        patched = client.put(
            f"/api/admin/accounts/{created['id']}/roles",
            json={"access_roles": ["operator"]},
        )
        assert patched.status_code == 200, patched.text

        client.cookies.clear()
        login = client.post(
            "/api/auth/login",
            json={"email": "dg-operator@example.com", "password": "password123"},
        )
        assert login.status_code == 200, login.text

        project = _create_project(client, slug="dg-op-del")
        response = client.post(
            f"/api/admin/projects/{project['id']}/datagate/import",
            json={
                "sync_names": False,
                "refresh_configs": False,
                "import_new": False,
                "delete_removed": True,
                "server_ids": [],
            },
        )
        assert response.status_code == 403
        body = response.json()
        assert body["success"] is False
        assert "admin" in body["message"].lower()

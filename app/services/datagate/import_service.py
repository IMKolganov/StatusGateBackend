"""Preview and apply DataGate → StatusGate imports."""

from __future__ import annotations

import re
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.probe_defaults import default_probe_url
from app.models.component_kind import OPENVPN_COMPONENT_KIND_ID, XRAY_COMPONENT_KIND_ID
from app.models.datagate_integration import DatagateIntegration
from app.models.enums import ConnectionMode
from app.models.monitored_component import MonitoredComponent
from app.models.project import Project
from app.schemas.datagate import (
    DatagateImportItemResult,
    DatagateImportRequest,
    DatagateImportResponse,
    DatagateIntegrationResponse,
    DatagateIntegrationUpsert,
    DatagateLocalComponentSummary,
    DatagateMatchedPair,
    DatagatePreviewResponse,
    DatagateServerSummary,
    DatagateTestResponse,
)
from app.services.datagate.client import DataGateApiError, DataGateClient, DataGateServer
from app.services.datagate.matcher import LocalVpnComponent, match_servers, monitor_common_name


def _slugify(value: str, fallback: str = "service") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:100] or fallback


class DatagateIntegrationService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _get_project(self, project_id: UUID) -> Project:
        project = self._session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        return project

    def get_integration(self, project_id: UUID) -> DatagateIntegration | None:
        self._get_project(project_id)
        return self._session.get(DatagateIntegration, project_id)

    def to_response(self, integration: DatagateIntegration) -> DatagateIntegrationResponse:
        return DatagateIntegrationResponse(
            project_id=integration.project_id,
            base_url=integration.base_url,
            client_id=integration.client_id,
            client_secret_set=bool(integration.client_secret),
            monitor_cn_prefix=integration.monitor_cn_prefix,
            is_enabled=integration.is_enabled,
            created_at=integration.created_at,
            updated_at=integration.updated_at,
        )

    def upsert(self, project_id: UUID, payload: DatagateIntegrationUpsert) -> DatagateIntegration:
        self._get_project(project_id)
        integration = self._session.get(DatagateIntegration, project_id)
        base_url = payload.base_url.rstrip("/")
        if integration is None:
            if not payload.client_secret:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="client_secret is required when creating the integration",
                )
            integration = DatagateIntegration(
                project_id=project_id,
                base_url=base_url,
                client_id=payload.client_id,
                client_secret=payload.client_secret,
                monitor_cn_prefix=payload.monitor_cn_prefix,
                is_enabled=payload.is_enabled,
            )
            self._session.add(integration)
        else:
            integration.base_url = base_url
            integration.client_id = payload.client_id
            integration.monitor_cn_prefix = payload.monitor_cn_prefix
            integration.is_enabled = payload.is_enabled
            if payload.client_secret:
                integration.client_secret = payload.client_secret
        self._session.commit()
        self._session.refresh(integration)
        return integration

    def require_integration(self, project_id: UUID) -> DatagateIntegration:
        integration = self.get_integration(project_id)
        if integration is None or not integration.is_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="DataGate integration is not configured for this project",
            )
        return integration

    def _client(self, integration: DatagateIntegration) -> DataGateClient:
        return DataGateClient(
            base_url=integration.base_url,
            client_id=integration.client_id,
            client_secret=integration.client_secret,
        )

    def test_connection(self, project_id: UUID) -> DatagateTestResponse:
        integration = self.require_integration(project_id)
        client = self._client(integration)
        try:
            client.get_token()
            servers = client.list_servers()
        except DataGateApiError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return DatagateTestResponse(
            ok=True,
            server_count=len(servers),
            message=f"Connected. {len(servers)} server(s) available.",
        )

    def list_servers(self, project_id: UUID) -> list[DatagateServerSummary]:
        integration = self.require_integration(project_id)
        client = self._client(integration)
        try:
            servers = [client.enrich_server(s) for s in client.list_servers()]
        except DataGateApiError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        return [self._server_summary(s) for s in servers]

    def _vpn_components(self, project_id: UUID) -> list[MonitoredComponent]:
        stmt = (
            select(MonitoredComponent)
            .where(
                MonitoredComponent.project_id == project_id,
                MonitoredComponent.check_type.in_(("openvpn", "xray")),
            )
            .order_by(MonitoredComponent.sort_order.asc(), MonitoredComponent.name.asc())
        )
        return list(self._session.scalars(stmt).all())

    def _to_local(self, component: MonitoredComponent) -> LocalVpnComponent:
        config_text = None
        if isinstance(component.check_config, dict):
            config_text = component.check_config.get("config_text")
        return LocalVpnComponent(
            id=component.id,
            name=component.name,
            slug=component.slug,
            check_type=component.check_type,
            config_text=config_text if isinstance(config_text, str) else None,
            datagate_server_id=component.datagate_server_id,
            datagate_common_name=component.datagate_common_name,
        )

    def _server_summary(self, server: DataGateServer) -> DatagateServerSummary:
        return DatagateServerSummary(
            id=server.id,
            server_type=server.server_type,
            check_type=server.check_type,
            server_name=server.server_name,
            api_url=server.api_url,
            is_online=server.is_online,
            is_disabled=server.is_disabled,
            tags=list(server.tags),
            host=server.host,
            port=server.port,
            proto=server.proto,
        )

    def _local_summary(self, component: LocalVpnComponent) -> DatagateLocalComponentSummary:
        return DatagateLocalComponentSummary(
            id=component.id,
            name=component.name,
            slug=component.slug,
            check_type=component.check_type,
            datagate_server_id=component.datagate_server_id,
            datagate_common_name=component.datagate_common_name,
        )

    def preview(self, project_id: UUID) -> DatagatePreviewResponse:
        integration = self.require_integration(project_id)
        client = self._client(integration)
        try:
            servers = [client.enrich_server(s) for s in client.list_servers() if not s.is_disabled]
        except DataGateApiError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        locals_ = [self._to_local(c) for c in self._vpn_components(project_id)]
        buckets = match_servers(servers, locals_)

        name_diffs = [m for m in buckets.matched if m.name_differs]
        sync_question = None
        if buckets.matched:
            if name_diffs:
                samples = ", ".join(
                    f"«{m.component.name}» → «{m.server.server_name}»" for m in name_diffs[:5]
                )
                sync_question = (
                    f"Найдено {len(buckets.matched)} уже подключённых серверов "
                    f"(имена отличаются у {len(name_diffs)}: {samples}"
                    f"{'…' if len(name_diffs) > 5 else ''}). "
                    "Синхронизировать имена сервисов и серверов?"
                )
            else:
                sync_question = (
                    f"Найдено {len(buckets.matched)} уже подключённых серверов. "
                    "Имена совпадают. Обновить конфиги при импорте?"
                )

        return DatagatePreviewResponse(
            matched=[
                DatagateMatchedPair(
                    server=self._server_summary(m.server),
                    component=self._local_summary(m.component),
                    name_differs=m.name_differs,
                    suggested_name=m.server.server_name,
                    endpoint_match=m.endpoint_match,
                    proto=m.server.proto,
                    already_linked=m.already_linked,
                    score=m.score,
                )
                for m in buckets.matched
            ],
            new_servers=[self._server_summary(s) for s in buckets.new_servers],
            unmatched_local=[self._local_summary(c) for c in buckets.unmatched_local],
            sync_names_question=sync_question,
        )

    def _unique_slug(self, project_id: UUID, base: str, *, exclude_id: UUID | None = None) -> str:
        slug = _slugify(base)
        candidate = slug
        n = 2
        while True:
            existing = self._session.scalar(
                select(MonitoredComponent).where(
                    MonitoredComponent.project_id == project_id,
                    MonitoredComponent.slug == candidate,
                )
            )
            if existing is None or (exclude_id is not None and existing.id == exclude_id):
                return candidate
            candidate = f"{slug}-{n}"[:100]
            n += 1

    def import_servers(self, project_id: UUID, payload: DatagateImportRequest) -> DatagateImportResponse:
        project = self._get_project(project_id)
        integration = self.require_integration(project_id)
        client = self._client(integration)

        try:
            all_servers = [client.enrich_server(s) for s in client.list_servers() if not s.is_disabled]
        except DataGateApiError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        selected_ids = set(payload.server_ids) if payload.server_ids is not None else {s.id for s in all_servers}
        servers = [s for s in all_servers if s.id in selected_ids]

        components = self._vpn_components(project_id)
        locals_ = [self._to_local(c) for c in components]
        buckets = match_servers(servers if payload.server_ids is None else all_servers, locals_)
        # Restrict match buckets to selected servers
        matched = [m for m in buckets.matched if m.server.id in selected_ids]
        new_servers = [s for s in buckets.new_servers if s.id in selected_ids]

        by_id = {c.id: c for c in components}
        external_id = f"statusgate:{project_id}"
        items: list[DatagateImportItemResult] = []
        created = updated = skipped = errors = 0

        for match in matched:
            component = by_id.get(match.component.id)
            if component is None:
                continue
            try:
                action_parts: list[str] = []
                if payload.sync_names and match.name_differs:
                    component.name = match.server.server_name
                    action_parts.append("synced_name")
                component.datagate_server_id = match.server.id
                cn = monitor_common_name(integration.monitor_cn_prefix, project.slug, match.server.id)
                if payload.refresh_configs:
                    config_text = client.ensure_config_text(
                        vpn_server_id=match.server.id,
                        common_name=cn,
                        external_id=external_id,
                        xray=match.server.check_type == "xray",
                    )
                    component.check_config = {"config_text": config_text}
                    component.datagate_common_name = cn
                    if match.server.check_type == "openvpn":
                        component.connection_mode = ConnectionMode.PERSISTENT.value
                    if match.server.api_url:
                        component.check_url = match.server.api_url
                    action_parts.append("refreshed_config")
                elif not component.datagate_common_name:
                    component.datagate_common_name = cn
                if not action_parts:
                    action_parts.append("linked")
                self._session.add(component)
                updated += 1
                items.append(
                    DatagateImportItemResult(
                        server_id=match.server.id,
                        server_name=match.server.server_name,
                        action="+".join(action_parts),
                        component_id=component.id,
                    )
                )
            except DataGateApiError as exc:
                errors += 1
                items.append(
                    DatagateImportItemResult(
                        server_id=match.server.id,
                        server_name=match.server.server_name,
                        action="error",
                        component_id=component.id,
                        message=str(exc),
                    )
                )

        if payload.import_new:
            max_sort = max((c.sort_order for c in components), default=0)
            for server in new_servers:
                try:
                    cn = monitor_common_name(integration.monitor_cn_prefix, project.slug, server.id)
                    config_text = client.ensure_config_text(
                        vpn_server_id=server.id,
                        common_name=cn,
                        external_id=external_id,
                        xray=server.check_type == "xray",
                    )
                    max_sort += 10
                    kind_id = XRAY_COMPONENT_KIND_ID if server.check_type == "xray" else OPENVPN_COMPONENT_KIND_ID
                    component = MonitoredComponent(
                        project_id=project_id,
                        component_kind_id=kind_id,
                        name=server.server_name,
                        slug=self._unique_slug(project_id, server.server_name),
                        check_url=(server.api_url or "").strip() or default_probe_url(),
                        check_method="GET",
                        check_type=server.check_type,
                        check_config={"config_text": config_text},
                        timeout_seconds=60,
                        connection_mode=(
                            ConnectionMode.PERSISTENT.value
                            if server.check_type == "openvpn"
                            else ConnectionMode.EPHEMERAL.value
                        ),
                        sort_order=max_sort,
                        is_active=True,
                        datagate_server_id=server.id,
                        datagate_common_name=cn,
                    )
                    self._session.add(component)
                    self._session.flush()
                    created += 1
                    items.append(
                        DatagateImportItemResult(
                            server_id=server.id,
                            server_name=server.server_name,
                            action="created",
                            component_id=component.id,
                        )
                    )
                except DataGateApiError as exc:
                    errors += 1
                    items.append(
                        DatagateImportItemResult(
                            server_id=server.id,
                            server_name=server.server_name,
                            action="error",
                            message=str(exc),
                        )
                    )
        else:
            skipped += len(new_servers)
            for server in new_servers:
                items.append(
                    DatagateImportItemResult(
                        server_id=server.id,
                        server_name=server.server_name,
                        action="skipped_new",
                    )
                )

        self._session.commit()
        return DatagateImportResponse(
            items=items,
            created=created,
            updated=updated,
            skipped=skipped,
            errors=errors,
        )

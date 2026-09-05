"""Preview and apply DataGate → StatusGate imports."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.probe_defaults import default_probe_url
from app.cqrs.commands.datagate import DatagateIntegrationCommandHandler
from app.cqrs.commands.monitored_components import MonitoredComponentCommandHandler
from app.cqrs.queries.datagate import DatagateIntegrationQueryHandler
from app.cqrs.queries.projects import ProjectQueryHandler
from app.models.component_kind import OPENVPN_COMPONENT_KIND_ID, XRAY_COMPONENT_KIND_ID
from app.models.datagate_integration import DatagateIntegration
from app.models.entity_change_log import EntityChangeLog
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
from app.services.audit import audit_scope, clear_pending_for_session, get_audit_context
from app.services.datagate.client import DataGateApiError, DataGateClient, DataGateServer
from app.services.datagate.matcher import LocalVpnComponent, match_servers, monitor_common_name, removed_linked_components
from app.services.datagate.secrets import decrypt_client_secret, encrypt_client_secret
from app.services.datagate.url_validation import validate_datagate_base_url

logger = logging.getLogger(__name__)


def public_error_message(exc: BaseException) -> str:
    """User-facing error text without internal stack / secret leakage."""
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        return exc.phrase or "Request failed"
    if isinstance(exc, DataGateApiError):
        text = str(exc).strip() or "DataGate API request failed"
        return text[:500]
    if isinstance(exc, IntegrityError):
        return "Database conflict while saving DataGate changes"
    if isinstance(exc, SQLAlchemyError):
        return "Database error while applying DataGate changes"
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]


def _slugify(value: str, fallback: str = "service") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:100] or fallback


def _preferred_new_slug(server: DataGateServer) -> str:
    """Build a stable slug that includes proto when present to reduce collisions."""
    base = _slugify(server.server_name)
    proto = (server.proto or "").lower()
    if proto in {"tcp", "udp"} and proto not in base:
        base = _slugify(f"{base}-{proto}")
    # Always suffix with DataGate id so re-imports of distinct servers never collide.
    return _slugify(f"{base}-dg{server.id}")


def _component_snapshot(component: MonitoredComponent) -> dict[str, Any]:
    return {
        "id": str(component.id),
        "name": component.name,
        "slug": component.slug,
        "check_type": component.check_type,
        "is_active": component.is_active,
        "datagate_server_id": component.datagate_server_id,
        "datagate_common_name": component.datagate_common_name,
    }


class DatagateIntegrationService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._projects = ProjectQueryHandler(session)
        self._integration_queries = DatagateIntegrationQueryHandler(session)
        self._integration_commands = DatagateIntegrationCommandHandler(session, auto_commit=False)
        self._component_commands = MonitoredComponentCommandHandler(session, auto_commit=False)

    def _safe_rollback(self) -> None:
        try:
            self._session.rollback()
        finally:
            clear_pending_for_session(self._session)

    def _persist_sync_failure(self, project_id: UUID, batch_id: UUID, message: str) -> None:
        try:
            integration = self._integration_queries.get_by_project_id(project_id)
            if integration is None:
                return
            integration.last_sync_status = "error"
            integration.last_sync_error = message[:2000]
            integration.last_sync_batch_id = batch_id
            self._integration_commands.save(integration, commit=True)
        except Exception:
            logger.exception("Failed to persist DataGate sync failure for project=%s", project_id)
            self._safe_rollback()

    def _get_project(self, project_id: UUID) -> Project:
        project = self._projects.get_by_id(project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        return project

    def get_integration(self, project_id: UUID) -> DatagateIntegration | None:
        self._get_project(project_id)
        return self._integration_queries.get_by_project_id(project_id)

    def to_response(self, integration: DatagateIntegration) -> DatagateIntegrationResponse:
        return DatagateIntegrationResponse(
            project_id=integration.project_id,
            base_url=integration.base_url,
            client_id=integration.client_id,
            client_secret_set=bool(integration.client_secret),
            monitor_cn_prefix=integration.monitor_cn_prefix,
            is_enabled=integration.is_enabled,
            auto_sync_enabled=integration.auto_sync_enabled,
            auto_sync_interval_hours=integration.auto_sync_interval_hours,
            auto_sync_import_new=integration.auto_sync_import_new,
            auto_sync_deactivate_removed=integration.auto_sync_deactivate_removed,
            last_synced_at=integration.last_synced_at,
            last_sync_status=integration.last_sync_status,
            last_sync_error=integration.last_sync_error,
            last_sync_batch_id=integration.last_sync_batch_id,
            created_at=integration.created_at,
            updated_at=integration.updated_at,
        )

    def upsert(self, project_id: UUID, payload: DatagateIntegrationUpsert) -> DatagateIntegration:
        try:
            self._get_project(project_id)
            integration = self._integration_queries.get_by_project_id(project_id)
            try:
                base_url = validate_datagate_base_url(payload.base_url)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
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
                    client_secret=encrypt_client_secret(payload.client_secret),
                    monitor_cn_prefix=payload.monitor_cn_prefix,
                    is_enabled=payload.is_enabled,
                    auto_sync_enabled=payload.auto_sync_enabled,
                    auto_sync_interval_hours=payload.auto_sync_interval_hours,
                    auto_sync_import_new=payload.auto_sync_import_new,
                    auto_sync_deactivate_removed=payload.auto_sync_deactivate_removed,
                )
            else:
                integration.base_url = base_url
                integration.client_id = payload.client_id
                integration.monitor_cn_prefix = payload.monitor_cn_prefix
                integration.is_enabled = payload.is_enabled
                integration.auto_sync_enabled = payload.auto_sync_enabled
                integration.auto_sync_interval_hours = payload.auto_sync_interval_hours
                integration.auto_sync_import_new = payload.auto_sync_import_new
                integration.auto_sync_deactivate_removed = payload.auto_sync_deactivate_removed
                if payload.client_secret:
                    integration.client_secret = encrypt_client_secret(payload.client_secret)
            self._integration_commands.save(integration, commit=True)
            return integration
        except HTTPException:
            self._safe_rollback()
            raise
        except Exception as exc:
            self._safe_rollback()
            logger.exception("DataGate upsert failed for project=%s", project_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to save DataGate integration: {public_error_message(exc)}",
            ) from exc

    def require_integration(self, project_id: UUID) -> DatagateIntegration:
        integration = self.get_integration(project_id)
        if integration is None or not integration.is_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="DataGate integration is not configured for this project",
            )
        return integration

    def _client(self, integration: DatagateIntegration) -> DataGateClient:
        try:
            secret = decrypt_client_secret(integration.client_secret)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc
        return DataGateClient(
            base_url=integration.base_url,
            client_id=integration.client_id,
            client_secret=secret,
        )

    def test_connection(self, project_id: UUID) -> DatagateTestResponse:
        try:
            integration = self.require_integration(project_id)
            client = self._client(integration)
            client.get_token()
            servers = client.list_servers()
        except HTTPException:
            raise
        except DataGateApiError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"DataGate connection failed: {public_error_message(exc)}",
            ) from exc
        except Exception as exc:
            logger.exception("DataGate connection test failed for project=%s", project_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"DataGate connection test failed: {public_error_message(exc)}",
            ) from exc
        return DatagateTestResponse(
            ok=True,
            server_count=len(servers),
            message=f"Connected. {len(servers)} server(s) available.",
        )

    def list_servers(self, project_id: UUID) -> list[DatagateServerSummary]:
        try:
            integration = self.require_integration(project_id)
            client = self._client(integration)
            servers = [client.enrich_server(s) for s in client.list_servers()]
        except HTTPException:
            raise
        except DataGateApiError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to list DataGate servers: {public_error_message(exc)}",
            ) from exc
        except Exception as exc:
            logger.exception("DataGate list_servers failed for project=%s", project_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list DataGate servers: {public_error_message(exc)}",
            ) from exc
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
        try:
            integration = self.require_integration(project_id)
            client = self._client(integration)
            servers = [client.enrich_server(s) for s in client.list_servers() if not s.is_disabled]

            locals_ = [self._to_local(c) for c in self._vpn_components(project_id)]
            buckets = match_servers(servers, locals_)
            enabled_ids = {s.id for s in servers}
            removed = removed_linked_components(locals_, enabled_ids)
            removed_ids = {c.id for c in removed}

            name_diffs = [m for m in buckets.matched if m.name_differs]
            sync_question = None
            if buckets.matched:
                if name_diffs:
                    samples = ", ".join(
                        f"«{m.component.name}» → «{m.server.server_name}»" for m in name_diffs[:5]
                    )
                    sync_question = (
                        f"Found {len(buckets.matched)} already linked servers "
                        f"(names differ for {len(name_diffs)}: {samples}"
                        f"{'…' if len(name_diffs) > 5 else ''}). "
                        "Sync service and server names?"
                    )
                else:
                    sync_question = (
                        f"Found {len(buckets.matched)} already linked servers. "
                        "Names match. Refresh configs on import?"
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
                unmatched_local=[
                    self._local_summary(c) for c in buckets.unmatched_local if c.id not in removed_ids
                ],
                removed_local=[self._local_summary(c) for c in removed],
                sync_names_question=sync_question,
            )
        except HTTPException:
            raise
        except DataGateApiError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"DataGate preview failed: {public_error_message(exc)}",
            ) from exc
        except Exception as exc:
            logger.exception("DataGate preview failed for project=%s", project_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"DataGate preview failed: {public_error_message(exc)}",
            ) from exc

    def _unique_slug(
        self,
        project_id: UUID,
        base: str,
        *,
        reserved: set[str],
        exclude_id: UUID | None = None,
    ) -> str:
        slug = _slugify(base)
        candidate = slug
        n = 2
        while True:
            if candidate not in reserved:
                existing = self._session.scalar(
                    select(MonitoredComponent).where(
                        MonitoredComponent.project_id == project_id,
                        MonitoredComponent.slug == candidate,
                    )
                )
                if existing is None or (exclude_id is not None and existing.id == exclude_id):
                    reserved.add(candidate)
                    return candidate
            candidate = f"{slug}-{n}"[:100]
            n += 1

    def import_servers(
        self,
        project_id: UUID,
        payload: DatagateImportRequest,
        *,
        source: str = "api",
        actor_account_id: UUID | None = None,
        batch_id: UUID | None = None,
        record_sync_status: bool = False,
    ) -> DatagateImportResponse:
        """Shared entrypoint for manual import and worker auto-sync."""
        batch = batch_id or uuid4()
        with audit_scope(source=source, actor_account_id=actor_account_id, batch_id=batch):
            try:
                return self._import_servers_inner(
                    project_id,
                    payload,
                    batch_id=batch,
                    record_sync_status=record_sync_status,
                )
            except HTTPException as exc:
                self._safe_rollback()
                if record_sync_status:
                    self._persist_sync_failure(project_id, batch, public_error_message(exc))
                raise
            except DataGateApiError as exc:
                self._safe_rollback()
                message = f"DataGate import failed: {public_error_message(exc)}"
                if record_sync_status:
                    self._persist_sync_failure(project_id, batch, message)
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=message) from exc
            except Exception as exc:
                self._safe_rollback()
                logger.exception("DataGate import failed for project=%s batch=%s", project_id, batch)
                message = f"DataGate import failed: {public_error_message(exc)}"
                if record_sync_status:
                    self._persist_sync_failure(project_id, batch, message)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=message,
                ) from exc

    def _import_servers_inner(
        self,
        project_id: UUID,
        payload: DatagateImportRequest,
        *,
        batch_id: UUID,
        record_sync_status: bool,
    ) -> DatagateImportResponse:
        project = self._get_project(project_id)
        integration = self.require_integration(project_id)
        client = self._client(integration)

        try:
            all_servers = [client.enrich_server(s) for s in client.list_servers() if not s.is_disabled]
        except DataGateApiError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to load DataGate servers: {public_error_message(exc)}",
            ) from exc

        selected_ids = set(payload.server_ids) if payload.server_ids is not None else {s.id for s in all_servers}
        servers = [s for s in all_servers if s.id in selected_ids]

        components = self._vpn_components(project_id)
        before_snapshot = [_component_snapshot(c) for c in components]
        locals_ = [self._to_local(c) for c in components]
        # Match only selected servers so unselected DG servers cannot steal local components.
        buckets = match_servers(servers, locals_)
        matched = buckets.matched
        new_servers = buckets.new_servers

        by_id = {c.id: c for c in components}
        external_id = f"statusgate:{project_id}"
        items: list[DatagateImportItemResult] = []
        created = updated = skipped = errors = deactivated = deleted = 0
        reserved_slugs = {c.slug for c in components}

        for match in matched:
            component = by_id.get(match.component.id)
            if component is None:
                continue
            try:
                cn = monitor_common_name(integration.monitor_cn_prefix, project.slug, match.server.id)
                config_text: str | None = None
                # Fetch config before mutating ORM state so failures leave the row unchanged.
                if payload.refresh_configs:
                    config_text = client.ensure_config_text(
                        vpn_server_id=match.server.id,
                        common_name=cn,
                        external_id=external_id,
                        xray=match.server.check_type == "xray",
                    )

                action_parts: list[str] = []
                if not component.is_active:
                    component.is_active = True
                    action_parts.append("reactivated")
                if payload.sync_names and match.name_differs:
                    component.name = match.server.server_name
                    action_parts.append("synced_name")
                component.datagate_server_id = match.server.id
                if config_text is not None:
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
                self._component_commands.update(component, commit=False)
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
                try:
                    self._session.refresh(component)
                except Exception:
                    pass
                errors += 1
                items.append(
                    DatagateImportItemResult(
                        server_id=match.server.id,
                        server_name=match.server.server_name,
                        action="error",
                        component_id=component.id,
                        message=public_error_message(exc),
                    )
                )
            except Exception as exc:
                try:
                    self._session.refresh(component)
                except Exception:
                    pass
                logger.exception(
                    "DataGate import item failed server_id=%s component_id=%s",
                    match.server.id,
                    component.id,
                )
                errors += 1
                items.append(
                    DatagateImportItemResult(
                        server_id=match.server.id,
                        server_name=match.server.server_name,
                        action="error",
                        component_id=component.id,
                        message=public_error_message(exc),
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
                    slug = self._unique_slug(project_id, _preferred_new_slug(server), reserved=reserved_slugs)
                    component = MonitoredComponent(
                        project_id=project_id,
                        component_kind_id=kind_id,
                        name=server.server_name,
                        slug=slug,
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
                    try:
                        with self._session.begin_nested():
                            self._component_commands.create(component, commit=False)
                    except IntegrityError as exc:
                        clear_pending_for_session(self._session)
                        errors += 1
                        reserved_slugs.discard(slug)
                        items.append(
                            DatagateImportItemResult(
                                server_id=server.id,
                                server_name=server.server_name,
                                action="error",
                                message=f"Database conflict: {public_error_message(exc)}",
                            )
                        )
                        continue
                    created += 1
                    items.append(
                        DatagateImportItemResult(
                            server_id=server.id,
                            server_name=server.server_name,
                            action="created",
                            component_id=component.id,
                            message=f"slug={slug}",
                        )
                    )
                except DataGateApiError as exc:
                    errors += 1
                    items.append(
                        DatagateImportItemResult(
                            server_id=server.id,
                            server_name=server.server_name,
                            action="error",
                            message=public_error_message(exc),
                        )
                    )
                except Exception as exc:
                    logger.exception("DataGate create failed server_id=%s", server.id)
                    errors += 1
                    items.append(
                        DatagateImportItemResult(
                            server_id=server.id,
                            server_name=server.server_name,
                            action="error",
                            message=public_error_message(exc),
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

        # Cleanup uses the full enabled DG list — never the partial server_ids selection.
        enabled_ids = {s.id for s in all_servers}
        removed_locals = removed_linked_components(locals_, enabled_ids)
        do_delete = payload.delete_removed
        do_deactivate = payload.deactivate_removed

        # Refuse mass wipe when DataGate reports zero enabled servers (outage / empty account).
        if (do_delete or do_deactivate) and not all_servers and removed_locals:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Refusing to deactivate/delete removed services: DataGate returned "
                    "zero enabled servers. Verify DataGate inventory, then retry."
                ),
            )

        if do_delete or do_deactivate:
            for local in removed_locals:
                component = by_id.get(local.id)
                if component is None:
                    continue
                server_id = component.datagate_server_id or 0
                if do_delete:
                    try:
                        with self._session.begin_nested():
                            self._component_commands.delete(component, commit=False)
                    except IntegrityError as exc:
                        clear_pending_for_session(self._session)
                        errors += 1
                        items.append(
                            DatagateImportItemResult(
                                server_id=server_id,
                                server_name=component.name,
                                action="error",
                                component_id=component.id,
                                message=f"Delete failed: {public_error_message(exc)}",
                            )
                        )
                        continue
                    deleted += 1
                    items.append(
                        DatagateImportItemResult(
                            server_id=server_id,
                            server_name=component.name,
                            action="deleted",
                            component_id=component.id,
                        )
                    )
                    continue

                if not component.is_active:
                    skipped += 1
                    items.append(
                        DatagateImportItemResult(
                            server_id=server_id,
                            server_name=component.name,
                            action="skipped_inactive",
                            component_id=component.id,
                        )
                    )
                    continue
                component.is_active = False
                self._component_commands.update(component, commit=False)
                deactivated += 1
                items.append(
                    DatagateImportItemResult(
                        server_id=server_id,
                        server_name=component.name,
                        action="deactivated",
                        component_id=component.id,
                    )
                )

        after_components = self._vpn_components(project_id)
        after_snapshot = [_component_snapshot(c) for c in after_components]
        summary = (
            f"DataGate import: created={created} updated={updated} skipped={skipped} "
            f"deactivated={deactivated} deleted={deleted} errors={errors}"
        )
        ctx = get_audit_context()
        self._session.add(
            EntityChangeLog(
                actor_account_id=ctx.actor_account_id,
                source=ctx.source,
                batch_id=batch_id,
                entity_type="datagate_import_batch",
                entity_id=str(batch_id),
                project_id=project_id,
                action="sync",
                before={"components": before_snapshot},
                after={"components": after_snapshot, "items": [item.model_dump(mode="json") for item in items]},
                diff={
                    "created": created,
                    "updated": updated,
                    "skipped": skipped,
                    "deactivated": deactivated,
                    "deleted": deleted,
                    "errors": errors,
                    "removed_detected": len(removed_locals),
                },
                trace_id=ctx.trace_id,
                request_id=ctx.trace_id,
                summary=summary,
            )
        )

        if record_sync_status:
            integration.last_synced_at = datetime.now(UTC)
            integration.last_sync_status = "ok" if errors == 0 else "partial_error"
            integration.last_sync_error = None if errors == 0 else f"{errors} item error(s)"
            integration.last_sync_batch_id = batch_id
            self._integration_commands.save(integration, commit=False)

        try:
            self._component_commands.commit()
        except Exception as exc:
            self._safe_rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to commit DataGate import: {public_error_message(exc)}",
            ) from exc
        return DatagateImportResponse(
            items=items,
            created=created,
            updated=updated,
            skipped=skipped,
            errors=errors,
            deactivated=deactivated,
            deleted=deleted,
            batch_id=batch_id,
        )

    def run_auto_sync(self, project_id: UUID) -> DatagateImportResponse:
        integration = self.require_integration(project_id)
        payload = DatagateImportRequest(
            sync_names=True,
            refresh_configs=True,
            import_new=integration.auto_sync_import_new,
            deactivate_removed=integration.auto_sync_deactivate_removed,
            delete_removed=False,
            server_ids=None,
        )
        return self.import_servers(
            project_id,
            payload,
            source="worker",
            record_sync_status=True,
        )

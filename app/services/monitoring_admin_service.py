from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.cqrs.common import PaginatedResult, PaginationParams
from app.models.check_result import CheckResult
from app.models.connection_event import ConnectionEvent
from app.models.monitored_component import MonitoredComponent
from app.models.enums import PERSISTENT_VPN_CHECK_TYPES, VPN_CHECK_TYPES, ConnectionMode
from app.schemas.monitoring import MonitoringSettingsResponse, MonitoringSettingsUpdate, PurgeCheckHistoryResponse, SpeedTestAdvisoryResponse
from app.schemas.monitored_component import MonitoredComponentResponse
from app.services.catalog_service import MonitoredComponentService
from app.services.monitoring_service import CheckResultRepository, ConnectionEventRepository, HealthCheckRunner, MonitoringSettingsRepository
from app.services.speed_test_config import (
    CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE,
    estimate_speed_tests_per_minute,
    speed_test_rate_warning,
    uses_default_cloudflare_template,
)
from app.services.vpn_check_service import public_network_summary


class MonitoringAdminService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._component_service = MonitoredComponentService(session)
        self._runner = HealthCheckRunner(session)
        self._settings_repo = MonitoringSettingsRepository(session)
        self._results_repo = CheckResultRepository(session)
        self._events_repo = ConnectionEventRepository(session)

    def get_settings_response(self) -> MonitoringSettingsResponse:
        settings = self._settings_repo.get()
        self._session.commit()
        return MonitoringSettingsResponse(
            default_poll_interval_seconds=settings.default_poll_interval_seconds,
            scheduler_interval_seconds=settings.scheduler_interval_seconds,
            default_speed_test_url_template=settings.default_speed_test_url_template,
            default_speed_test_interval_seconds=settings.default_speed_test_interval_seconds,
            updated_at=settings.updated_at,
        )

    def update_settings(self, payload: MonitoringSettingsUpdate) -> MonitoringSettingsResponse:
        settings = self._settings_repo.get()
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(settings, key, value)
        self._settings_repo.save(settings)
        self._session.commit()
        return self.get_settings_response()

    def get_speed_test_advisory(self, project_id: UUID | None = None) -> SpeedTestAdvisoryResponse:
        settings = self._settings_repo.get()
        if project_id is None:
            components = self._component_service.list(PaginationParams(offset=0, limit=1000)).items
        else:
            components = self._component_service.list_by_project(project_id, PaginationParams(offset=0, limit=1000)).items
        active_vpn = [
            component
            for component in components
            if component.is_active and component.check_type in VPN_CHECK_TYPES and component.speed_test_enabled
        ]
        per_minute = estimate_speed_tests_per_minute(active_vpn, settings)
        uses_cloudflare = any(uses_default_cloudflare_template(component, settings) for component in active_vpn)
        warning = speed_test_rate_warning(active_vpn, settings)
        self._session.commit()
        return SpeedTestAdvisoryResponse(
            active_vpn_service_count=len(active_vpn),
            estimated_speed_tests_per_minute=round(per_minute, 2),
            uses_default_cloudflare_template=uses_cloudflare,
            guidance_requests_per_minute=CLOUDFLARE_SPEED_TEST_GUIDANCE_REQUESTS_PER_MINUTE,
            warning=warning,
        )

    def run_manual_check(self, component_id: UUID) -> CheckResult:
        component = self._component_service.get(component_id)
        if (
            component.check_type in PERSISTENT_VPN_CHECK_TYPES
            and component.connection_mode == ConnectionMode.PERSISTENT.value
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Manual checks are not supported for persistent OpenVPN services; use the connection log instead.",
            )
        result = self._runner.run_check(component)
        self._session.commit()
        return result

    def list_check_results(self, component_id: UUID, params: PaginationParams) -> PaginatedResult[CheckResult]:
        self._component_service.get(component_id)
        items, total = self._results_repo.list_for_component_paginated(
            component_id,
            offset=params.offset,
            limit=params.limit,
        )
        return PaginatedResult(
            items=items,
            total=total,
            offset=params.offset,
            limit=params.limit,
        )

    def list_connection_events(self, component_id: UUID, params: PaginationParams) -> PaginatedResult[ConnectionEvent]:
        self._component_service.get(component_id)
        items, total = self._events_repo.list_for_component_paginated(
            component_id,
            offset=params.offset,
            limit=params.limit,
        )
        return PaginatedResult(
            items=items,
            total=total,
            offset=params.offset,
            limit=params.limit,
        )

    def purge_check_history(self, component_id: UUID, *, keep: int = 0) -> PurgeCheckHistoryResponse:
        self._component_service.get(component_id)
        deleted, remaining = self._results_repo.purge_for_component(component_id, keep=keep)
        if keep == 0:
            self._events_repo.purge_for_component(component_id)
        self._session.commit()
        return PurgeCheckHistoryResponse(deleted_count=deleted, remaining_count=remaining)

    @staticmethod
    def _latest_diagnostics(latest: CheckResult) -> tuple[str | None, str | None]:
        log_tail = None
        if isinstance(latest.details, dict):
            raw = latest.details.get("log_tail")
            if isinstance(raw, str) and raw.strip():
                log_tail = raw
        return latest.error_message, log_tail

    @staticmethod
    def enrich_component(component: MonitoredComponent, latest: CheckResult | None) -> MonitoredComponentResponse:
        response = MonitoredComponentResponse.model_validate(component)
        if latest is not None:
            response.latest_outcome = latest.outcome
            response.latest_latency_ms = latest.latency_ms
            response.latest_checked_at = latest.checked_at
            error_message, log_tail = MonitoringAdminService._latest_diagnostics(latest)
            response.latest_error_message = error_message
            response.latest_log_tail = log_tail
            response.latest_network_summary = public_network_summary(latest.details)
        return response

    def enrich_components(self, components: list[MonitoredComponent]) -> list[MonitoredComponentResponse]:
        latest_map = self._results_repo.latest_by_component_ids([component.id for component in components])
        return [self.enrich_component(component, latest_map.get(component.id)) for component in components]

    def enrich_component_by_id(self, component_id: UUID) -> MonitoredComponentResponse:
        component = self._component_service.get(component_id)
        latest_map = self._results_repo.latest_by_component_ids([component.id])
        return self.enrich_component(component, latest_map.get(component.id))

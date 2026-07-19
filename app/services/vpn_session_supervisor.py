from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.database import SessionLocal
from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome, ConnectionEventType, ConnectionMode, PERSISTENT_VPN_CHECK_TYPES
from app.models.monitored_component import MonitoredComponent
from app.models.project import Project
from app.services.connection_event_service import record_connection_event
from app.services.monitoring_service import CheckResultRepository, HealthCheckRunner, MonitoringSettingsRepository
from app.services.speed_test_config import (
    SpeedTestRunContext,
    effective_speed_test_url_template,
    extract_last_successful_speed_test,
    extract_speed_test_from_details,
    should_run_speed_test,
)
from app.services.vpn_check_service import (
    RECONNECT_DELAY_SECONDS,
    OpenVpnSessionHandle,
    is_openvpn_persistent_session_up,
    run_openvpn_persistent_probe,
    start_openvpn_persistent_session,
    stop_openvpn_persistent_session,
)
from app.services.vpn_netns import NetnsPermissionError

# Capability / host misconfig will not heal by retrying every 5s.
_NETNS_PERMISSION_BACKOFF_SECONDS = 60

logger = logging.getLogger(__name__)


def _connection_event_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    network = details.get("network")
    if not isinstance(network, dict):
        return None
    probe = network.get("probe")
    return {"probe": probe} if isinstance(probe, dict) else None


@dataclass(frozen=True)
class _PersistentComponentSnapshot:
    component_id: UUID
    config_fingerprint: str
    poll_interval_seconds: int
    is_active: bool


class _PersistentOpenVpnWorker(threading.Thread):
    def __init__(self, component_id: UUID, stop_event: threading.Event) -> None:
        super().__init__(name=f"vpn-persist-{component_id}", daemon=True)
        self._component_id = component_id
        self._stop = stop_event
        self._last_probe_ok: bool | None = None

    def run(self) -> None:
        handle: OpenVpnSessionHandle | None = None
        component: MonitoredComponent | None = None
        try:
            while not self._stop.is_set():
                component = self._load_component()
                if component is None or not self._should_run(component):
                    break

                try:
                    if handle is None or not is_openvpn_persistent_session_up(handle):
                        if handle is not None:
                            self._save_session_event(component, handle, ConnectionEventType.TUNNEL_DOWN.value)
                            stop_openvpn_persistent_session(handle)
                            handle = None
                            if self._stop.wait(RECONNECT_DELAY_SECONDS):
                                break
                            self._save_session_event(component, None, ConnectionEventType.RECONNECT.value)

                        handle = start_openvpn_persistent_session(component)
                        if handle is None:
                            self._save_connect_failure(component)
                            if self._stop.wait(RECONNECT_DELAY_SECONDS):
                                break
                            continue

                        self._save_session_event(component, handle, ConnectionEventType.TUNNEL_UP.value)

                    speed_test_context = self._build_speed_test_context(component)
                    result = run_openvpn_persistent_probe(
                        component,
                        handle,
                        speed_test_context=speed_test_context,
                        session_event="probe",
                    )
                    self._persist_result(component, result)
                    self._maybe_record_probe_transition(component, result)

                    interval = self._probe_interval_seconds(component)
                    if self._stop.wait(interval):
                        break
                except NetnsPermissionError:
                    logger.error(
                        "Persistent OpenVPN worker for component %s cannot create netns; "
                        "worker needs SYS_ADMIN and security_opt apparmor:unconfined, then recreate. "
                        "Retrying in %ss.",
                        self._component_id,
                        _NETNS_PERMISSION_BACKOFF_SECONDS,
                        exc_info=True,
                    )
                    if handle is not None:
                        stop_openvpn_persistent_session(handle)
                        handle = None
                    if self._stop.wait(_NETNS_PERMISSION_BACKOFF_SECONDS):
                        break
                except Exception:
                    logger.exception("Persistent OpenVPN worker failed for component %s", self._component_id)
                    if handle is not None:
                        stop_openvpn_persistent_session(handle)
                        handle = None
                        self._safe_record_connection_event(
                            component,
                            ConnectionEventType.TUNNEL_DOWN.value,
                            outcome=CheckOutcome.DOWN.value,
                            message="VPN session stopped after worker error",
                        )
                    if self._stop.wait(RECONNECT_DELAY_SECONDS):
                        break
        finally:
            if handle is not None:
                stop_openvpn_persistent_session(handle)
                if component is None:
                    component = self._load_component()
                if component is not None:
                    self._safe_record_connection_event(
                        component,
                        ConnectionEventType.TUNNEL_DOWN.value,
                        outcome=CheckOutcome.DOWN.value,
                        message="VPN session stopped",
                    )

    def _load_component(self) -> MonitoredComponent | None:
        with SessionLocal() as session:
            return session.get(MonitoredComponent, self._component_id)

    @staticmethod
    def _should_run(component: MonitoredComponent) -> bool:
        return (
            component.is_active
            and component.check_type in PERSISTENT_VPN_CHECK_TYPES
            and component.connection_mode == ConnectionMode.PERSISTENT.value
        )

    def _probe_interval_seconds(self, component: MonitoredComponent) -> int:
        with SessionLocal() as session:
            settings = MonitoringSettingsRepository(session).get()
            runner = HealthCheckRunner(session)
            return runner.effective_poll_interval(component, settings)

    def _build_speed_test_context(self, component: MonitoredComponent) -> SpeedTestRunContext:
        with SessionLocal() as session:
            settings = MonitoringSettingsRepository(session).get()
            latest_map = CheckResultRepository(session).latest_by_component_ids([component.id])
            latest = latest_map.get(component.id)
            latest_details = latest.details if latest and isinstance(latest.details, dict) else None
            return SpeedTestRunContext(
                url_template=effective_speed_test_url_template(component, settings),
                run_speed_test=should_run_speed_test(component, settings, latest),
                previous_speed_test=extract_speed_test_from_details(latest_details),
                last_successful_speed_test=extract_last_successful_speed_test(latest_details),
            )

    def _persist_result(self, component: MonitoredComponent, result: CheckResult) -> None:
        with SessionLocal() as session:
            component_row = session.get(MonitoredComponent, component.id)
            if component_row is None:
                return
            component_row.last_checked_at = result.checked_at
            session.add(component_row)
            CheckResultRepository(session).add(result)
            session.commit()

    def _record_connection_event(
        self,
        component: MonitoredComponent,
        event_type: str,
        *,
        occurred_at: datetime | None = None,
        outcome: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with SessionLocal() as session:
            record_connection_event(
                session,
                component_id=component.id,
                event_type=event_type,
                occurred_at=occurred_at,
                outcome=outcome,
                message=message,
                details=details,
            )
            session.commit()

    def _safe_record_connection_event(
        self,
        component: MonitoredComponent,
        event_type: str,
        *,
        occurred_at: datetime | None = None,
        outcome: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._record_connection_event(
                component,
                event_type,
                occurred_at=occurred_at,
                outcome=outcome,
                message=message,
                details=details,
            )
        except Exception:
            logger.exception(
                "Failed to record connection event %s for component %s",
                event_type,
                component.id,
            )

    def _maybe_record_probe_transition(self, component: MonitoredComponent, result: CheckResult) -> None:
        if result.outcome not in {CheckOutcome.UP.value, CheckOutcome.DEGRADED.value}:
            return

        probe_ok = result.outcome == CheckOutcome.UP.value
        details = result.details if isinstance(result.details, dict) else None
        network = details.get("network") if isinstance(details, dict) else None
        probe = network.get("probe") if isinstance(network, dict) else None
        event_details = {"probe": probe} if isinstance(probe, dict) else None

        if self._last_probe_ok is False and probe_ok:
            self._record_connection_event(
                component,
                ConnectionEventType.AVAILABLE.value,
                occurred_at=result.checked_at,
                outcome=result.outcome,
                message="Probe succeeded through VPN tunnel",
                details=event_details,
            )
        elif self._last_probe_ok is not False and not probe_ok:
            self._record_connection_event(
                component,
                ConnectionEventType.UNAVAILABLE.value,
                occurred_at=result.checked_at,
                outcome=result.outcome,
                message=result.error_message or "Probe failed through VPN tunnel",
                details=event_details,
            )

        self._last_probe_ok = probe_ok

    def _save_connect_failure(self, component: MonitoredComponent) -> None:
        checked_at = datetime.now(UTC)
        message = "OpenVPN tunnel did not come up in time"
        result = CheckResult(
            monitored_component_id=component.id,
            checked_at=checked_at,
            outcome=CheckOutcome.TIMEOUT.value,
            latency_ms=None,
            http_status_code=None,
            error_message=message,
            details={
                "check_type": component.check_type,
                "connection_mode": ConnectionMode.PERSISTENT.value,
                "session_event": ConnectionEventType.CONNECT_FAILED.value,
            },
        )
        self._persist_result(component, result)
        self._record_connection_event(
            component,
            ConnectionEventType.CONNECT_FAILED.value,
            occurred_at=checked_at,
            outcome=CheckOutcome.TIMEOUT.value,
            message=message,
        )

    def _save_session_event(
        self,
        component: MonitoredComponent,
        handle: OpenVpnSessionHandle | None,
        event: str,
    ) -> None:
        if handle is None:
            checked_at = datetime.now(UTC)
            message = f"VPN session event: {event}"
            result = CheckResult(
                monitored_component_id=component.id,
                checked_at=checked_at,
                outcome=CheckOutcome.DOWN.value,
                latency_ms=None,
                http_status_code=None,
                error_message=message,
                details={
                    "check_type": component.check_type,
                    "connection_mode": ConnectionMode.PERSISTENT.value,
                    "session_event": event,
                },
            )
            self._persist_result(component, result)
            self._record_connection_event(
                component,
                event,
                occurred_at=checked_at,
                outcome=CheckOutcome.DOWN.value,
                message=message,
            )
            return

        speed_test_context = SpeedTestRunContext(
            url_template=SpeedTestRunContext.default().url_template,
            run_speed_test=False,
        )
        result = run_openvpn_persistent_probe(
            component,
            handle,
            speed_test_context=speed_test_context,
            session_event=event,
        )
        self._persist_result(component, result)
        self._record_connection_event(
            component,
            event,
            occurred_at=result.checked_at,
            outcome=result.outcome,
            message=result.error_message,
            details=_connection_event_details(result.details if isinstance(result.details, dict) else None),
        )


class VpnSessionSupervisor:
    _instance: VpnSessionSupervisor | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._workers: dict[UUID, _PersistentOpenVpnWorker] = {}
        self._stop_events: dict[UUID, threading.Event] = {}
        self._snapshots: dict[UUID, _PersistentComponentSnapshot] = {}
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> VpnSessionSupervisor:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def sync(self) -> None:
        desired = self._load_desired_components()
        desired_ids = set(desired)

        with self._lock:
            running_ids = set(self._workers)

            for component_id in running_ids - desired_ids:
                self._stop_worker(component_id)

            for component_id in desired_ids:
                snapshot = self._snapshots.get(component_id)
                desired_snapshot = self._snapshot_for(component_id)
                if desired_snapshot is None:
                    continue
                if component_id not in running_ids or snapshot != desired_snapshot:
                    if component_id in running_ids:
                        self._stop_worker(component_id)
                    self._start_worker(component_id, desired_snapshot)

    def stop_all(self) -> None:
        with self._lock:
            for component_id in list(self._workers):
                self._stop_worker(component_id)

    def _start_worker(self, component_id: UUID, snapshot: _PersistentComponentSnapshot) -> None:
        stop_event = threading.Event()
        worker = _PersistentOpenVpnWorker(component_id, stop_event)
        self._stop_events[component_id] = stop_event
        self._workers[component_id] = worker
        self._snapshots[component_id] = snapshot
        worker.start()
        logger.info("Started persistent OpenVPN worker for component %s", component_id)

    def _stop_worker(self, component_id: UUID) -> None:
        stop_event = self._stop_events.pop(component_id, None)
        worker = self._workers.pop(component_id, None)
        self._snapshots.pop(component_id, None)
        if stop_event is not None:
            stop_event.set()
        if worker is not None:
            worker.join(timeout=30)
        logger.info("Stopped persistent OpenVPN worker for component %s", component_id)

    def _load_desired_components(self) -> list[UUID]:
        with SessionLocal() as session:
            stmt = (
                select(MonitoredComponent.id)
                .join(Project, MonitoredComponent.project_id == Project.id)
                .where(
                    MonitoredComponent.is_active.is_(True),
                    Project.is_active.is_(True),
                    MonitoredComponent.check_type.in_(PERSISTENT_VPN_CHECK_TYPES),
                    MonitoredComponent.connection_mode == ConnectionMode.PERSISTENT.value,
                )
            )
            return list(session.scalars(stmt).all())

    def _snapshot_for(self, component_id: UUID) -> _PersistentComponentSnapshot | None:
        with SessionLocal() as session:
            component = session.get(MonitoredComponent, component_id)
            if component is None:
                return None
            settings = MonitoringSettingsRepository(session).get()
            runner = HealthCheckRunner(session)
            return _PersistentComponentSnapshot(
                component_id=component.id,
                config_fingerprint=_component_fingerprint(component),
                poll_interval_seconds=runner.effective_poll_interval(component, settings),
                is_active=component.is_active,
            )


def _component_fingerprint(component: MonitoredComponent) -> str:
    payload = {
        "check_config": component.check_config,
        "check_url": component.check_url,
        "timeout_seconds": component.timeout_seconds,
        "speed_test_bytes": component.speed_test_bytes,
        "speed_test_url_template": component.speed_test_url_template,
        "speed_test_interval_seconds": component.speed_test_interval_seconds,
        "speed_test_enabled": component.speed_test_enabled,
        "connection_mode": component.connection_mode,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

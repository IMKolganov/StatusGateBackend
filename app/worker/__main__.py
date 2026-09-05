from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from app.database import SessionLocal
from app.services.audit import audit_scope, clear_pending_for_session
from app.services.datagate.import_service import DatagateIntegrationService, public_error_message
from app.services.monitoring_service import HealthCheckRunner, MonitoringSettingsRepository
from app.services.vpn_session_supervisor import VpnSessionSupervisor

logger = logging.getLogger(__name__)

_last_autosync_check_at: datetime | None = None
_AUTOSYNC_CHECK_EVERY = timedelta(minutes=5)


def _list_due_project_ids(session) -> list:
    service = DatagateIntegrationService(session)
    now = datetime.now(UTC)
    due = []
    for integration in service._integration_queries.list_auto_sync_enabled():
        interval = max(1, int(integration.auto_sync_interval_hours or 24))
        if integration.last_synced_at is None:
            due.append(integration.project_id)
            continue
        last = integration.last_synced_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now - last >= timedelta(hours=interval):
            due.append(integration.project_id)
    return due


def _persist_autosync_failure(project_id, exc: BaseException) -> None:
    """Record failure in a fresh session so a broken import transaction cannot leak."""
    message = public_error_message(exc)
    with SessionLocal() as err_session:
        service = DatagateIntegrationService(err_session)
        integration = service.get_integration(project_id)
        if integration is None:
            return
        integration.last_sync_status = "error"
        integration.last_sync_error = message[:2000]
        service._integration_commands.save(integration, commit=True)


def _run_one_autosync(project_id) -> None:
    with SessionLocal() as session:
        service = DatagateIntegrationService(session)
        try:
            with audit_scope(source="worker"):
                result = service.run_auto_sync(project_id)
                logger.info(
                    "DataGate auto-sync project=%s batch=%s created=%s updated=%s errors=%s",
                    project_id,
                    result.batch_id,
                    result.created,
                    result.updated,
                    result.errors,
                )
        except Exception as exc:
            logger.exception("DataGate auto-sync failed for project=%s", project_id)
            try:
                session.rollback()
            finally:
                clear_pending_for_session(session)
            try:
                _persist_autosync_failure(project_id, exc)
            except Exception:
                logger.exception("Failed to persist auto-sync error for project=%s", project_id)


def _maybe_run_datagate_autosync() -> None:
    global _last_autosync_check_at
    now = datetime.now(UTC)
    if _last_autosync_check_at is not None and now - _last_autosync_check_at < _AUTOSYNC_CHECK_EVERY:
        return
    _last_autosync_check_at = now

    with SessionLocal() as session:
        due_ids = _list_due_project_ids(session)

    for project_id in due_ids:
        _run_one_autosync(project_id)


def run_scheduler_cycle() -> int:
    VpnSessionSupervisor.instance().sync()
    with SessionLocal() as session:
        runner = HealthCheckRunner(session)
        settings = MonitoringSettingsRepository(session).get()
        session.commit()
        results = runner.run_due_checks()
        if results:
            logger.info("Completed %s health check(s)", len(results))
        interval = settings.scheduler_interval_seconds
    try:
        _maybe_run_datagate_autosync()
    except Exception:
        logger.exception("DataGate auto-sync check failed")
    return interval


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("StatusGate monitoring worker started")
    try:
        while True:
            try:
                sleep_seconds = run_scheduler_cycle()
            except Exception:
                logger.exception("Scheduler cycle failed")
                sleep_seconds = 30
            time.sleep(sleep_seconds)
    finally:
        VpnSessionSupervisor.instance().stop_all()


if __name__ == "__main__":
    main()

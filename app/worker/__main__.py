import logging
import time
from datetime import UTC, datetime, timedelta

from app.database import SessionLocal
from app.services.audit import audit_scope
from app.services.datagate.import_service import DatagateIntegrationService
from app.services.monitoring_service import HealthCheckRunner, MonitoringSettingsRepository
from app.services.vpn_session_supervisor import VpnSessionSupervisor

logger = logging.getLogger(__name__)

_last_autosync_check_at: datetime | None = None
_AUTOSYNC_CHECK_EVERY = timedelta(minutes=5)


def _maybe_run_datagate_autosync() -> None:
    global _last_autosync_check_at
    now = datetime.now(UTC)
    if _last_autosync_check_at is not None and now - _last_autosync_check_at < _AUTOSYNC_CHECK_EVERY:
        return
    _last_autosync_check_at = now

    with SessionLocal() as session:
        service = DatagateIntegrationService(session)
        due = []
        for integration in service._integration_queries.list_auto_sync_enabled():
            interval = max(1, int(integration.auto_sync_interval_hours or 24))
            if integration.last_synced_at is None:
                due.append(integration)
                continue
            last = integration.last_synced_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if now - last >= timedelta(hours=interval):
                due.append(integration)

        for integration in due:
            project_id = integration.project_id
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
            except Exception:
                logger.exception("DataGate auto-sync failed for project=%s", project_id)
                try:
                    integration = service.get_integration(project_id)
                    if integration is not None:
                        integration.last_sync_status = "error"
                        integration.last_sync_error = "auto-sync failed; see worker logs"
                        service._integration_commands.save(integration, commit=True)
                except Exception:
                    logger.exception("Failed to persist auto-sync error for project=%s", project_id)


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

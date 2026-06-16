import logging
import time

from app.database import SessionLocal
from app.services.monitoring_service import HealthCheckRunner, MonitoringSettingsRepository

logger = logging.getLogger(__name__)


def run_scheduler_cycle() -> int:
    with SessionLocal() as session:
        runner = HealthCheckRunner(session)
        settings = MonitoringSettingsRepository(session).get()
        session.commit()
        results = runner.run_due_checks()
        if results:
            logger.info("Completed %s health check(s)", len(results))
        return settings.scheduler_interval_seconds


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("StatusGate monitoring worker started")
    while True:
        try:
            sleep_seconds = run_scheduler_cycle()
        except Exception:
            logger.exception("Scheduler cycle failed")
            sleep_seconds = 30
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()

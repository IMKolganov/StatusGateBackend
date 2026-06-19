#!/usr/bin/env python3
"""Seed a demo project with 90 days of mixed check outcomes for timeline UI testing.

Run from repo root:

  docker run --rm --network statusgate_default \\
    -v "$(pwd)/backend:/app" -w /app -e PYTHONPATH=/app \\
    -e JWT_SECRET=test-secret-with-at-least-32-chars-long \\
    -e DATABASE_URL=postgresql+psycopg://statusgate:statusgate@db:5432/statusgate \\
    statusgate-backend python scripts/seed_demo_timeline.py
"""

from __future__ import annotations

import os
import random
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.check_result import CheckResult
from app.models.enums import CheckOutcome
from app.models.monitored_component import MonitoredComponent
from app.models.project import Project

WEB_KIND_ID = UUID("00000000-0000-4000-8000-000000000020")
PROJECT_SLUG = "timeline-demo"
DAYS = 90
CHECKS_PER_DAY = 24

SERVICES = (
    ("api-gateway", "API Gateway"),
    ("auth-service", "Auth Service"),
    ("worker-queue", "Worker Queue"),
)


def _outcome_for_day(day_offset: int) -> str:
    if day_offset < 45:
        return CheckOutcome.UP.value
    if day_offset == 52:
        return CheckOutcome.TIMEOUT.value
    if day_offset == 53:
        return CheckOutcome.DOWN.value
    if day_offset in {60, 61}:
        return CheckOutcome.DEGRADED.value
    if day_offset == 70:
        return CheckOutcome.ERROR.value
    if day_offset >= 80 and random.random() < 0.08:
        return CheckOutcome.TIMEOUT.value
    return CheckOutcome.UP.value


def seed(session: Session) -> None:
    project = session.scalar(select(Project).where(Project.slug == PROJECT_SLUG))
    if project is None:
        project = Project(
            name="Timeline Demo",
            slug=PROJECT_SLUG,
            description="Demo project for status timeline UI",
            is_active=True,
        )
        session.add(project)
        session.flush()
        print(f"Created project {PROJECT_SLUG}")
    else:
        print(f"Using existing project {PROJECT_SLUG}")

    components: list[MonitoredComponent] = []
    for slug, name in SERVICES:
        component = session.scalar(
            select(MonitoredComponent).where(
                MonitoredComponent.project_id == project.id,
                MonitoredComponent.slug == slug,
            )
        )
        if component is None:
            component = MonitoredComponent(
                project_id=project.id,
                component_kind_id=WEB_KIND_ID,
                name=name,
                slug=slug,
                environment="demo",
                check_url="https://example.com/health",
                is_active=True,
            )
            session.add(component)
            session.flush()
            print(f"Created component {slug}")
        else:
            session.execute(delete(CheckResult).where(CheckResult.monitored_component_id == component.id))
            print(f"Reset history for component {slug}")
        components.append(component)

    range_end = datetime.now(UTC).date()
    range_start = range_end - timedelta(days=DAYS - 1)

    for component in components:
        for day_index in range(DAYS):
            day = range_start + timedelta(days=day_index)
            outcome = _outcome_for_day(day_index)
            if day_index < 20 and component.slug == "worker-queue":
                continue
            for hour in range(CHECKS_PER_DAY):
                checked_at = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=hour)
                session.add(
                    CheckResult(
                        monitored_component_id=component.id,
                        checked_at=checked_at,
                        outcome=outcome,
                        latency_ms=random.randint(25, 180),
                    )
                )

    session.commit()
    print(f"Seeded {len(components)} services with up to {DAYS * CHECKS_PER_DAY} checks each.")
    print(f"Open http://localhost:5173/projects/{PROJECT_SLUG} after starting the frontend dev server.")


def main() -> int:
    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is required", file=sys.stderr)
        return 1
    with SessionLocal() as session:
        seed(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

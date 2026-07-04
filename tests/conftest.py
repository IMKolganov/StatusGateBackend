import os
from collections.abc import Generator
from urllib.parse import urlparse
from uuid import UUID

import psycopg
from psycopg import sql
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-with-at-least-32-characters")
os.environ.setdefault(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://statusgate:statusgate@localhost:5432/statusgate_test",
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ.setdefault("ALLOW_REGISTRATION", "true")
os.environ.setdefault("REQUIRE_EMAIL_VERIFICATION", "false")

TEST_ADMIN_EMAIL = "admin@example.com"
TEST_PASSWORD = "password123"

from app.api.deps import get_db
from app.api.routes.auth import limiter
from app.main import app
from app.models import Base
from app.models.access_role import AccessRole
from app.models.component_kind import DEFAULT_COMPONENT_KINDS, ComponentKind
from app.models.monitoring_settings import MONITORING_SETTINGS_ID, MonitoringSettings

limiter.enabled = False

ADMIN_ROLE_ID = UUID("00000000-0000-4000-8000-000000000001")
OPERATOR_ROLE_ID = UUID("00000000-0000-4000-8000-000000000002")
VIEWER_ROLE_ID = UUID("00000000-0000-4000-8000-000000000003")
USER_ROLE_ID = UUID("00000000-0000-4000-8000-000000000004")

ACCESS_ROLES = [
    (ADMIN_ROLE_ID, "Administrator", "admin", "Full access"),
    (OPERATOR_ROLE_ID, "Operator", "operator", "Manage catalog"),
    (VIEWER_ROLE_ID, "Viewer", "viewer", "Read-only"),
    (USER_ROLE_ID, "User", "user", "Public account"),
]


def _postgres_admin_dsn(database_url: str) -> str:
    normalized = database_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlparse(normalized)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    user = parsed.username or "statusgate"
    password = parsed.password or "statusgate"
    return f"postgresql://{user}:{password}@{host}:{port}/postgres"


def _test_database_name(database_url: str) -> str:
    normalized = database_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlparse(normalized)
    name = (parsed.path or "/statusgate_test").lstrip("/")
    return name or "statusgate_test"


def _ensure_test_database() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    admin_dsn = _postgres_admin_dsn(database_url)
    test_db_name = _test_database_name(database_url)
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_db_name,))
                if cur.fetchone() is None:
                    cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_db_name)))
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL is not available for integration tests: {exc}")


def _run_migrations(database_url: str) -> None:
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_cfg, "head")


def _seed_access_roles(session: Session) -> None:
    for role_id, name, slug, description in ACCESS_ROLES:
        exists = session.get(AccessRole, role_id)
        if exists is None:
            session.add(AccessRole(id=role_id, name=name, slug=slug, description=description))
    session.commit()


def _seed_monitoring_settings(session: Session) -> None:
    exists = session.get(MonitoringSettings, MONITORING_SETTINGS_ID)
    if exists is None:
        session.add(MonitoringSettings(id=MONITORING_SETTINGS_ID))
    session.commit()


def _seed_component_kinds(session: Session) -> None:
    for kind_id, name, slug, description in DEFAULT_COMPONENT_KINDS:
        exists = session.get(ComponentKind, kind_id)
        if exists is None:
            session.add(ComponentKind(id=kind_id, name=name, slug=slug, description=description))
    session.commit()


def _truncate_all(session: Session) -> None:
    table_names = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    session.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    session.commit()


@pytest.fixture(scope="session")
def engine() -> Generator[Engine, None, None]:
    _ensure_test_database()
    database_url = os.environ["DATABASE_URL"]
    _run_migrations(database_url)
    eng = create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Generator[Session, None, None]:
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = session_factory()
    _truncate_all(session)
    _seed_access_roles(session)
    _seed_monitoring_settings(session)
    _seed_component_kinds(session)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    client.post(
        "/api/auth/register",
        json={"email": TEST_ADMIN_EMAIL, "password": TEST_PASSWORD},
    )
    login = client.post(
        "/api/auth/login",
        json={"email": TEST_ADMIN_EMAIL, "password": TEST_PASSWORD},
    )
    assert login.status_code == 200, login.text
    return {}

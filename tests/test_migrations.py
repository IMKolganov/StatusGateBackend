import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _alembic_config(database_url: str | None = None) -> Config:
    cfg = Config("alembic.ini")
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(_alembic_config())


def test_alembic_has_single_head() -> None:
    heads = _script_directory().get_heads()
    assert heads == ["012"], f"expected single head 012, got {heads}"


def test_alembic_revision_ids_are_unique() -> None:
    revisions = [rev.revision for rev in _script_directory().walk_revisions()]
    duplicates = sorted({rev for rev in revisions if revisions.count(rev) > 1})
    assert duplicates == [], f"duplicate revision ids: {duplicates}"


def test_alembic_revision_filenames_match_revision_ids() -> None:
    migration_files = sorted(
        path for path in VERSIONS_DIR.glob("*.py") if path.name != "__init__.py"
    )
    script = _script_directory()
    revisions_by_file = {
        path.name: next(
            rev.revision
            for rev in script.walk_revisions()
            if rev.path and Path(rev.path).name == path.name
        )
        for path in migration_files
    }

    mismatches = []
    for filename, revision_id in revisions_by_file.items():
        prefix = filename.split("_", 1)[0]
        if prefix != revision_id:
            mismatches.append(f"{filename} uses revision {revision_id}")

    assert mismatches == [], "revision id / filename prefix mismatches:\n" + "\n".join(mismatches)


def _postgres_admin_dsn(database_url: str) -> str:
    normalized = database_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlparse(normalized)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    user = parsed.username or "statusgate"
    password = parsed.password or "statusgate"
    return f"postgresql://{user}:{password}@{host}:{port}/postgres"


def _with_database_name(database_url: str, database_name: str) -> str:
    normalized = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlparse(normalized)
    return (
        f"postgresql+psycopg://{parsed.username}:{parsed.password}"
        f"@{parsed.hostname}:{parsed.port or 5432}/{database_name}"
    )


def _database_name(database_url: str) -> str:
    normalized = database_url.replace("postgresql+psycopg://", "postgresql://")
    parsed = urlparse(normalized)
    return (parsed.path or "/statusgate_migration_test").lstrip("/") or "statusgate_migration_test"


def _ensure_database(database_url: str) -> None:
    admin_dsn = _postgres_admin_dsn(database_url)
    db_name = _database_name(database_url)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{db_name}"')


def _drop_database(database_url: str) -> None:
    admin_dsn = _postgres_admin_dsn(database_url)
    db_name = _database_name(database_url)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


@pytest.mark.integration
def test_alembic_upgrade_head_on_empty_database() -> None:
    base_database_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://statusgate:statusgate@localhost:5432/statusgate_test",
    )
    migration_db_url = _with_database_name(base_database_url, "statusgate_migration_test")

    try:
        _drop_database(migration_db_url)
        _ensure_database(migration_db_url)
        command.upgrade(_alembic_config(migration_db_url), "head")

        engine = create_engine(migration_db_url, pool_pre_ping=True)
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("monitored_components")}
        assert "speed_test_bytes" in columns

        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "012"

        columns = {column["name"] for column in inspector.get_columns("monitored_components")}
        assert "speed_test_url_template" in columns
        assert "speed_test_interval_seconds" in columns
        assert "speed_test_enabled" in columns
        settings_columns = {column["name"] for column in inspector.get_columns("monitoring_settings")}
        assert "default_speed_test_url_template" in settings_columns
        assert "default_speed_test_interval_seconds" in settings_columns
    except (psycopg.Error, OperationalError) as exc:
        pytest.skip(f"PostgreSQL is not available for migration integration test: {exc}")
    finally:
        try:
            _drop_database(migration_db_url)
        except psycopg.Error:
            pass


@pytest.mark.integration
def test_compose_database_is_at_head_revision() -> None:
    database_url = os.environ.get(
        "COMPOSE_DATABASE_URL",
        "postgresql+psycopg://statusgate:statusgate@localhost:5432/statusgate",
    )
    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "012"

        columns = {column["name"] for column in inspect(engine).get_columns("monitored_components")}
        assert "speed_test_bytes" in columns
        assert "speed_test_url_template" in columns
    except (psycopg.Error, OperationalError) as exc:
        pytest.skip(f"Compose PostgreSQL is not available: {exc}")

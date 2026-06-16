# StatusGate Backend

FastAPI backend for [StatusGate](https://github.com/IMKolganov/StatusGateBackend): public status pages, health monitoring, incident history, and admin APIs.

## Stack

- Python 3.14, FastAPI, SQLAlchemy, Alembic
- PostgreSQL
- Background monitoring worker

## Quick start (Docker)

```bash
cp .env.example .env
# Edit JWT_SECRET in .env

docker compose up -d --build
```

API: http://localhost:8000  
OpenAPI: http://localhost:8000/docs

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

export DATABASE_URL=postgresql+psycopg://statusgate:statusgate@localhost:5432/statusgate
export JWT_SECRET=replace-with-at-least-32-characters-long-random-secret

alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Worker (separate terminal):

```bash
python -m app.worker
```

## Tests

```bash
pytest -v
```

Requires PostgreSQL with database `statusgate_test` (see `tests/conftest.py`).

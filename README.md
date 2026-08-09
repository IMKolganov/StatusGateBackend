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
# Optional: DEFAULT_SPEED_TEST_URL_TEMPLATE, DEFAULT_PROBE_URL, INTERNET_PING_HOST, VPN_NETNS_DNS_SERVERS, …

docker compose up -d --build
```

API: http://localhost:8000  
OpenAPI: http://localhost:8000/docs

## Configuration

See `.env.example`. Notable optional overrides (hardcoded fallbacks when unset):

| Variable | Default purpose |
|----------|-----------------|
| `DEFAULT_SPEED_TEST_URL_TEMPLATE` | VPN/WAN download URL template (`{bytes}` placeholder) |
| `CLOUDFLARE_SPEED_TEST_ORIGIN` | Origin used to detect Cloudflare speed-test URLs |
| `DEFAULT_PROBE_URL` | Exit-IP probe when a VPN service has no `check_url` |
| `GOOGLE_PROBE_URL` | Reachability probe through the tunnel |
| `INTERNET_PING_HOST` | Continuous internet-path ping target |
| `VPN_NETNS_DNS_SERVERS` | Comma-separated nameservers written into VPN netns `resolv.conf` |
| `HOST_WAN_BASELINE_PATH` | JSON file shared by API + worker for the latest host WAN snapshot |

VPN checks measure **download + upload** through the tunnel and stamp a **host WAN** baseline onto results when the worker can run it safely (skipped while ephemeral OpenVPN may own host routes).

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
# Needs Postgres (e.g. docker compose up -d db from the monorepo root).
pytest -v
pytest -q --cov=app --cov-report=term-missing
```

Requires PostgreSQL with database `statusgate_test` (see `tests/conftest.py`).

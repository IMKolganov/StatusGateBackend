FROM python:3.14-slim AS base

WORKDIR /app

ARG XRAY_VERSION=25.6.8

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        openvpn \
        iproute2 \
        iputils-ping \
        curl \
        unzip \
        ca-certificates \
    && curl -fsSL "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-64.zip" -o /tmp/xray.zip \
    && unzip -q /tmp/xray.zip xray -d /usr/local/bin \
    && chmod +x /usr/local/bin/xray \
    && rm /tmp/xray.zip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

# Test image: needs Postgres (compose service `backend-test` + `db`).
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY pytest.ini .
COPY tests ./tests
ENV JWT_SECRET=compose-test-jwt-secret-at-least-32-characters
CMD ["pytest", "-q", "--tb=line"]

FROM base AS runtime
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

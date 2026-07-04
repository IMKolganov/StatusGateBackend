#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
basedpyright app tests alembic

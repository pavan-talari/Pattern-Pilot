#!/bin/bash
set -e

echo "Running Alembic migrations..."
alembic upgrade head

echo "Starting Pattern Pilot API..."
exec uvicorn pattern_pilot.api.server:create_app --factory --host 0.0.0.0 --port 8100 --reload

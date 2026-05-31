#!/usr/bin/env bash
set -euo pipefail
cd /home/devcode/hermes-ops-dashboard
export HERMES_DASHBOARD_PROFILE=tecnico
export HERMES_DASHBOARD_PORT=8770
export HERMES_DASHBOARD_BASIC_USER=admin
export HERMES_DASHBOARD_BASIC_PASSWORD='HermesOps-2026-05-31!'
exec python3 app.py

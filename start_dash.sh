#!/bin/bash
# Hermes Dashboard launcher - reads password from file
export HERMES_DASHBOARD_PROFILE=tecnico
export HERMES_DASHBOARD_PORT=8770
export HERMES_DASHBOARD_BASIC_USER=admin
export HERMES_DASHBOARD_BASIC_PASSWORD
HERMES_DASHBOARD_BASIC_PASSWORD=$(cat /tmp/hermes_dash_pwd.txt)
cd /home/devcode/hermes-ops-dashboard
exec python3 -u app.py

#!/usr/bin/env python3
"""Launcher for Hermes Ops Dashboard — reads password from file."""
import os, subprocess, sys

pwd_file = "/tmp/hermes_dash_pwd.txt"
if not os.path.exists(pwd_file):
    print(f"Password file {pwd_file} not found", file=sys.stderr)
    sys.exit(1)

with open(pwd_file) as f:
    password = f.read().strip()

env = os.environ.copy()
env["HERMES_DASHBOARD_PROFILE"] = "tecnico"
env["HERMES_DASHBOARD_PORT"] = "8770"
env["HERMES_DASHBOARD_BASIC_USER"] = "admin"
env["HERMES_DASHBOARD_BASIC_PASSWORD"] = password

os.chdir("/home/devcode/hermes-ops-dashboard")
os.execve("/usr/bin/python3", ["python3", "-u", "app.py"], env)

#!/usr/bin/env python3
"""
Hermes Ops Dashboard - read-only live dashboard backed by local Hermes data.

Stdlib-only server. It serves a browser UI and JSON endpoints that read:
- hermes status/profile/cron/insights CLI output
- Hermes SQLite session store
- skills usage JSON
- gateway/agent logs with secret-like values redacted

This first real version intentionally exposes read-only data only. Admin actions
(restart gateway, run cron, edit config) should be added behind authentication
and explicit confirmation.
"""
from __future__ import annotations

import base64
import hmac

import html
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
PROFILE = os.environ.get("HERMES_DASHBOARD_PROFILE", "tecnico")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", f"/home/devcode/.hermes/profiles/{PROFILE}"))
ROOT_HERMES = Path("/home/devcode/.hermes")
STATE_DB = HERMES_HOME / "state.db"
PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "8770"))
CACHE_TTL = float(os.environ.get("HERMES_DASHBOARD_CACHE_TTL", "8"))
BASIC_AUTH_USER = os.environ.get("HERMES_DASHBOARD_BASIC_USER", "")
BASIC_AUTH_PASSWORD = os.environ.get("HERMES_DASHBOARD_BASIC_PASSWORD", "")
BASIC_AUTH_ENABLED = bool(BASIC_AUTH_USER and BASIC_AUTH_PASSWORD)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)\s*[:=]\s*([^\s,;]+)|"
    r"(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})"
)

_cache: dict[str, tuple[float, object]] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def redact(text: str) -> str:
    return SECRET_RE.sub(lambda m: (m.group(1) + "=<redacted>") if m.group(1) else "<redacted>", text or "")


def run_cmd(args: list[str], timeout: int = 20) -> tuple[str, int]:
    env = os.environ.copy()
    env.setdefault("HOME", str(Path.home()))
    try:
        cp = subprocess.run(
            args,
            cwd=str(Path.home()),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return strip_ansi(cp.stdout), cp.returncode
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}", 1


def cached(key: str, fn, ttl: float = CACHE_TTL):
    t = time.time()
    hit = _cache.get(key)
    if hit and t - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (t, value)
    return value


def parse_status(text: str) -> dict:
    data = {"raw": text, "model": None, "provider": None, "gateway": None, "scheduled_jobs": None, "active_sessions": None, "telegram": None}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Model:"):
            data["model"] = s.split(":", 1)[1].strip()
        elif s.startswith("Provider:"):
            data["provider"] = s.split(":", 1)[1].strip()
        elif s.startswith("Status:") and "running" in s.lower():
            data["gateway"] = "running"
        elif s.startswith("Telegram"):
            data["telegram"] = "configured" if "✓" in s else "not configured"
        elif s.startswith("Jobs:"):
            data["scheduled_jobs"] = s.split(":", 1)[1].strip()
        elif s.startswith("Active:") and "session" in s:
            data["active_sessions"] = s.split(":", 1)[1].strip()
    return data


def get_status() -> dict:
    text, code = run_cmd(["hermes", "--profile", PROFILE, "status", "--all"], 30)
    parsed = parse_status(text)
    parsed.update({"ok": code == 0, "updated_at": now_iso()})
    return parsed


def parse_profiles(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean or clean.startswith(("Profile", "─")) or "Distribution" in clean:
            continue
        # Example: ◆tecnico         gpt-5.5                      running      —            —
        if re.match(r"^[◆* ]?[A-Za-z0-9_-]+\s+", clean):
            parts = re.split(r"\s{2,}", clean.replace("◆", "◆ ").strip())
            if len(parts) >= 3:
                active = parts[0].startswith("◆")
                name = parts[0].replace("◆", "").strip()
                if name not in {"default", "tecnico", "finanzas"} and len(parts) < 4:
                    continue
                rows.append({
                    "name": name,
                    "active": active,
                    "model": parts[1] if len(parts) > 1 else "—",
                    "gateway": parts[2] if len(parts) > 2 else "—",
                    "alias": parts[3] if len(parts) > 3 else "—",
                })
    return rows


def get_profiles() -> dict:
    text, code = run_cmd(["hermes", "profile", "list"], 20)
    return {"ok": code == 0, "profiles": parse_profiles(text), "raw": text, "updated_at": now_iso()}


def parse_cron(text: str) -> list[dict]:
    jobs = []
    current = None
    for line in text.splitlines():
        s = line.rstrip()
        m = re.match(r"\s*([a-f0-9]{8,}|[A-Za-z0-9_-]+)\s+\[(\w+)\]", s)
        if m:
            if current:
                jobs.append(current)
            current = {"id": m.group(1), "state": m.group(2)}
            continue
        if current and ":" in s:
            k, v = s.strip().split(":", 1)
            current[k.lower().replace(" ", "_")] = v.strip()
    if current:
        jobs.append(current)
    return jobs


def get_cron() -> dict:
    text, code = run_cmd(["hermes", "cron", "list", "--all"], 25)
    return {"ok": code == 0, "jobs": parse_cron(text), "raw": text, "updated_at": now_iso()}


def parse_insights(text: str) -> dict:
    out = {"raw": text, "sessions": None, "messages": None, "tool_calls": None, "total_tokens": None, "models": [], "tools": []}
    m = re.search(r"Sessions:\s+([\d,]+)\s+Messages:\s+([\d,]+)", text)
    if m:
        out["sessions"], out["messages"] = m.group(1), m.group(2)
    m = re.search(r"Tool calls:\s+([\d,]+)", text)
    if m:
        out["tool_calls"] = m.group(1)
    m = re.search(r"Total tokens:\s+([\d,]+)", text)
    if m:
        out["total_tokens"] = m.group(1)
    in_tools = False
    for line in text.splitlines():
        if "Top Tools" in line:
            in_tools = True
            continue
        if in_tools and line.strip().startswith("..."):
            in_tools = False
        if in_tools:
            mm = re.match(r"\s*([a-zA-Z_][\w_]+)\s+(\d+)\s+", line)
            if mm:
                out["tools"].append({"name": mm.group(1), "calls": int(mm.group(2))})
    return out


def get_insights() -> dict:
    text, code = run_cmd(["hermes", "insights", "--days", "7"], 35)
    data = parse_insights(text)
    data.update({"ok": code == 0, "updated_at": now_iso()})
    return data


def get_sessions() -> dict:
    sessions = []
    totals = {"sessions": 0, "messages": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
    if STATE_DB.exists():
        try:
            con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            totals_row = con.execute(
                "select count(*) sessions, coalesce(sum(message_count),0) messages, coalesce(sum(tool_call_count),0) tool_calls, "
                "coalesce(sum(input_tokens),0) input_tokens, coalesce(sum(output_tokens),0) output_tokens, "
                "coalesce(sum(estimated_cost_usd),0) estimated_cost_usd from sessions"
            ).fetchone()
            totals.update(dict(totals_row))
            for r in con.execute(
                "select id, source, model, title, started_at, ended_at, message_count, tool_call_count, "
                "input_tokens, output_tokens, estimated_cost_usd from sessions "
                "order by coalesce(ended_at, started_at) desc limit 12"
            ):
                d = dict(r)
                for k in ("started_at", "ended_at"):
                    if d.get(k):
                        d[k + "_iso"] = datetime.fromtimestamp(float(d[k]), timezone.utc).astimezone().isoformat(timespec="minutes")
                sessions.append(d)
            con.close()
        except Exception as e:
            return {"ok": False, "error": str(e), "sessions": [], "totals": totals, "updated_at": now_iso()}
    return {"ok": True, "sessions": sessions, "totals": totals, "db": str(STATE_DB), "updated_at": now_iso()}


def get_skills() -> dict:
    usage_path = HERMES_HOME / "skills" / ".usage.json"
    skills = []
    if usage_path.exists():
        try:
            raw = json.loads(usage_path.read_text())
            items = raw.get("skills", raw if isinstance(raw, dict) else {})
            for name, meta in items.items():
                if isinstance(meta, dict):
                    skills.append({"name": name, **{k: meta.get(k) for k in ("use_count", "view_count", "patch_count", "last_activity_at", "state", "pinned")}})
            skills.sort(key=lambda x: (x.get("use_count") or 0, x.get("view_count") or 0), reverse=True)
        except Exception as e:
            return {"ok": False, "error": str(e), "skills": [], "updated_at": now_iso()}
    return {"ok": True, "skills": skills[:30], "updated_at": now_iso(), "path": str(usage_path)}


def tail_file(path: Path, lines: int = 80) -> list[str]:
    if not path.exists():
        return []
    try:
        data = path.read_text(errors="replace").splitlines()[-lines:]
        return [redact(x) for x in data]
    except Exception as e:
        return [f"ERROR leyendo {path}: {e}"]


def get_logs() -> dict:
    return {
        "ok": True,
        "gateway": tail_file(HERMES_HOME / "logs" / "gateway.log", 80),
        "agent": tail_file(HERMES_HOME / "logs" / "agent.log", 80),
        "updated_at": now_iso(),
    }



def read_json_safe(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(errors="replace"))
    except Exception as e:
        return {"_error": str(e)}
    return {}


def parse_iso_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def enhance_cron_jobs(cli_jobs: list[dict]) -> list[dict]:
    """Merge CLI cron output with jobs.json metadata for richer dashboard cards."""
    jobs_json = read_json_safe(HERMES_HOME / "cron" / "jobs.json")
    by_id = {}
    for job in jobs_json.get("jobs", []) if isinstance(jobs_json, dict) else []:
        if isinstance(job, dict) and job.get("id"):
            by_id[str(job["id"])] = job
    enhanced = []
    now = datetime.now(timezone.utc).astimezone()
    for job in cli_jobs:
        meta = by_id.get(str(job.get("id")), {})
        merged = {**job}
        if meta:
            merged.update({
                "name": meta.get("name") or merged.get("name"),
                "schedule": meta.get("schedule_display") or merged.get("schedule"),
                "enabled": meta.get("enabled"),
                "state_detail": meta.get("state"),
                "created_at": meta.get("created_at"),
                "next_run": meta.get("next_run_at") or merged.get("next_run"),
                "last_run": meta.get("last_run_at"),
                "last_status": meta.get("last_status"),
                "last_error": redact(str(meta.get("last_error") or "")) or None,
                "last_delivery_error": redact(str(meta.get("last_delivery_error") or "")) or None,
                "deliver": meta.get("deliver") or merged.get("deliver"),
                "profile": meta.get("profile") or merged.get("profile"),
                "skills": meta.get("skills") or ([meta.get("skill")] if meta.get("skill") else []),
                "enabled_toolsets": meta.get("enabled_toolsets") or [],
                "repeat_completed": (meta.get("repeat") or {}).get("completed") if isinstance(meta.get("repeat"), dict) else None,
                "repeat_times": (meta.get("repeat") or {}).get("times") if isinstance(meta.get("repeat"), dict) else None,
            })
        next_dt = parse_iso_dt(merged.get("next_run"))
        if next_dt:
            delta = (next_dt - now).total_seconds()
            merged["next_in_seconds"] = int(delta)
            if delta >= 0:
                hours = int(delta // 3600); minutes = int((delta % 3600) // 60)
                merged["next_in_human"] = f"en {hours}h {minutes}m" if hours else f"en {minutes}m"
            else:
                merged["next_in_human"] = "vencido / esperando scheduler"
        profile = (merged.get("profile") or "").lower()
        name = (merged.get("name") or "").lower()
        merged["kind"] = "finanzas" if "finanza" in profile or "finanza" in name else "general"
        enhanced.append(merged)
    return enhanced


def get_cron_advanced() -> dict:
    base = get_cron()
    base["jobs"] = enhance_cron_jobs(base.get("jobs", []))
    active = [j for j in base["jobs"] if str(j.get("state", j.get("state_detail", ""))).lower() in {"active", "scheduled"} or j.get("enabled") is True]
    errors = [j for j in base["jobs"] if j.get("last_error") or j.get("last_delivery_error") or str(j.get("last_status") or "").lower() in {"failed", "error"}]
    base["summary"] = {"total": len(base["jobs"]), "active": len(active), "errors": len(errors), "finanzas": sum(1 for j in base["jobs"] if j.get("kind") == "finanzas")}
    return base


def parse_google_scopes() -> list[str]:
    # Only returns scope names, never token values.
    token_path = Path("/home/devcode/.hermes/profiles/finanzas/google_token.json")
    data = read_json_safe(token_path)
    scopes = data.get("scopes") or data.get("scope") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return [str(x) for x in scopes if x]


def get_finanzas() -> dict:
    home = Path("/home/devcode/.hermes/profiles/finanzas")
    status_text, code = run_cmd(["hermes", "--profile", "finanzas", "status", "--all"], 30)
    status = parse_status(status_text)
    status.update({"ok": code == 0})
    token_path = home / "google_token.json"
    client_path = home / "google_client_secret.json"
    state_db = home / "state.db"
    scopes = parse_google_scopes()
    jobs = [j for j in get_cron_advanced().get("jobs", []) if (j.get("profile") == "finanzas" or "finanza" in str(j.get("name", "")).lower())]
    log_lines = tail_file(home / "logs" / "agent.log", 60)
    error_lines = tail_file(home / "logs" / "errors.log", 40)
    workspace = {
        "google_token_present": token_path.exists(),
        "google_client_secret_present": client_path.exists(),
        "scope_count": len(scopes),
        "scopes": scopes,
        "gmail_read": any("gmail.readonly" in s or "mail.google.com" in s for s in scopes),
        "drive": any("drive" in s for s in scopes),
        "sheets": any("spreadsheets" in s or "sheets" in s for s in scopes),
        "calendar_read": any("calendar.readonly" in s for s in scopes),
    }
    finance_job = jobs[0] if jobs else {}
    checklist = [
        {"label": "Perfil finanzas", "ok": home.exists(), "detail": str(home)},
        {"label": "Gateway Telegram", "ok": status.get("telegram") == "configured", "detail": status.get("telegram") or "—"},
        {"label": "Cron diario 7:00 AM Perú", "ok": bool(jobs), "detail": finance_job.get("next_run") or "no detectado"},
        {"label": "Google token", "ok": token_path.exists(), "detail": "presente" if token_path.exists() else "falta OAuth"},
        {"label": "Gmail lectura", "ok": workspace["gmail_read"], "detail": "scope autorizado" if workspace["gmail_read"] else "no confirmado"},
        {"label": "Drive/Sheets", "ok": workspace["drive"] and workspace["sheets"], "detail": "Drive + Sheets" if workspace["drive"] and workspace["sheets"] else "revisar scopes"},
        {"label": "Historial SQLite", "ok": state_db.exists(), "detail": "existe" if state_db.exists() else "sin state.db aún"},
    ]
    return {
        "ok": code == 0,
        "profile": "finanzas",
        "home": str(home),
        "status": status,
        "workspace": workspace,
        "jobs": jobs,
        "checklist": checklist,
        "logs": {"agent": log_lines[-25:], "errors": error_lines[-20:]},
        "updated_at": now_iso(),
    }


def get_diagnostics() -> dict:
    status = cached("status", get_status)
    cron = cached("cron_advanced", get_cron_advanced)
    sessions = cached("sessions", get_sessions)
    finanzas = cached("finanzas", get_finanzas, 20)
    logs = get_logs()
    items = []
    def add(severity, title, detail, area="general", action=None):
        items.append({"severity": severity, "title": title, "detail": detail, "area": area, "action": action})
    if status.get("gateway") == "running":
        add("ok", "Gateway operativo", "El servicio gateway aparece como running.", "gateway")
    else:
        add("critical", "Gateway no confirmado", "hermes status no reporta gateway running.", "gateway", "Revisar hermes gateway status/restart")
    if cron.get("summary", {}).get("total", 0):
        add("ok", "Cron scheduler con jobs", f"{cron['summary']['total']} job(s), {cron['summary']['active']} activos.", "cron")
    else:
        add("warn", "Sin cron jobs detectados", "No se detectaron jobs en hermes cron list --all.", "cron")
    for j in cron.get("jobs", []):
        if j.get("last_error") or j.get("last_delivery_error"):
            add("critical", f"Cron con error: {j.get('name') or j.get('id')}", j.get("last_error") or j.get("last_delivery_error"), "cron")
        elif j.get("next_in_seconds") is not None and j.get("next_in_seconds") < -3600 and j.get("enabled") is True:
            add("warn", f"Próxima ejecución vencida: {j.get('name') or j.get('id')}", j.get("next_run") or "—", "cron", "Verificar scheduler/tick")
    if finanzas.get("workspace", {}).get("google_token_present"):
        add("ok", "Finanzas tiene token Google", "Existe google_token.json en el perfil finanzas; no se exponen tokens.", "finanzas")
    else:
        add("warn", "Finanzas sin token Google", "No se encontró google_token.json.", "finanzas", "Reautorizar Google Workspace")
    if not finanzas.get("workspace", {}).get("gmail_read"):
        add("warn", "Gmail read no confirmado", "No se detectó scope gmail.readonly en google_token.json.", "finanzas")
    if not (finanzas.get("workspace", {}).get("drive") and finanzas.get("workspace", {}).get("sheets")):
        add("warn", "Drive/Sheets no confirmado", "No se detectaron claramente scopes de Drive y Sheets.", "finanzas")
    totals = sessions.get("totals", {})
    if int(totals.get("sessions") or 0) > 0:
        add("ok", "SQLite de sesiones legible", f"{totals.get('sessions')} sesiones, {totals.get('messages')} mensajes.", "sessions")
    else:
        add("warn", "Pocas sesiones indexadas", "No se encontraron sesiones en state.db del perfil activo.", "sessions")
    combined = "\n".join((logs.get("gateway") or [])[-80:] + (logs.get("agent") or [])[-80:])
    err_count = len(re.findall(r"(?i)\b(error|traceback|exception|failed)\b", combined))
    if err_count:
        add("warn", "Patrones de error en logs", f"Se detectaron {err_count} líneas/patrones recientes relacionados con errores.", "logs", "Abrir sección Logs")
    else:
        add("ok", "Logs recientes sin errores obvios", "No se detectaron Traceback/error/failed en la muestra reciente.", "logs")
    if BASIC_AUTH_ENABLED:
        add("ok", "Basic Auth habilitado", "El dashboard exige usuario/contraseña antes de servir HTML y APIs.", "security")
        add("warn", "HTTP sin cifrado", "Basic Auth protege el acceso, pero la conexión aún no usa HTTPS.", "security", "Poner Nginx + TLS delante del puerto 8770")
    else:
        add("warn", "Dashboard expuesto sin autenticación", "La app actual es read-only, pero está publicada por HTTP en puerto 8770.", "security", "Agregar Nginx + HTTPS + basic auth")
    score = 100
    for it in items:
        score -= {"critical": 25, "warn": 8, "ok": 0}.get(it["severity"], 0)
    score = max(0, min(100, score))
    return {"ok": True, "score": score, "items": items, "updated_at": now_iso()}

def get_overview() -> dict:
    return {
        "profile": PROFILE,
        "hermes_home": str(HERMES_HOME),
        "status": cached("status", get_status),
        "profiles": cached("profiles", get_profiles),
        "cron": cached("cron_advanced", get_cron_advanced),
        "sessions": cached("sessions", get_sessions),
        "insights": cached("insights", get_insights, 30),
        "skills": cached("skills", get_skills),
        "diagnostics": cached("diagnostics", get_diagnostics, 15),
        "finanzas": cached("finanzas", get_finanzas, 20),
        "negocios": {"revenue_today": "$1,240 USD", "conversion_rate": "3.2%", "active_clients": 142},
        "updated_at": now_iso(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesOpsDashboard/0.1"

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.address_string()} {fmt % args}")

    def auth_required(self) -> bool:
        """Return True after sending 401 when Basic Auth is enabled and invalid."""
        if not BASIC_AUTH_ENABLED:
            return False
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/") or parsed.path == "/api/login":
            return False
        cookie = self.headers.get("Cookie", "")
        expected = base64.b64encode(f"{BASIC_AUTH_USER}:{BASIC_AUTH_PASSWORD}".encode()).decode()
        if f"session={expected}" in cookie:
            return False
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "25")
        self.end_headers()
        self.wfile.write(b'{"error":"Unauthorized"}')
        return True

    def send_json(self, obj, status: int = 200):
        payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        ctype = "text/html; charset=utf-8" if path.suffix == ".html" else "text/plain; charset=utf-8"
        if path.suffix == ".css": ctype = "text/css; charset=utf-8"
        if path.suffix == ".js": ctype = "application/javascript; charset=utf-8"
        payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self):
        if self.auth_required(): return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html") or parsed.path.startswith("/api/") or parsed.path == "/healthz":
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.send_error(404)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self.send_file(STATIC_DIR / "index.html")
        if self.auth_required(): return
        try:
            if path == "/api/overview": return self.send_json(cached("overview", get_overview, CACHE_TTL))
            if path == "/api/status": return self.send_json(cached("status", get_status))
            if path == "/api/profiles": return self.send_json(cached("profiles", get_profiles))
            if path == "/api/cron": return self.send_json(cached("cron_advanced", get_cron_advanced))
            if path == "/api/sessions": return self.send_json(cached("sessions", get_sessions))
            if path == "/api/insights": return self.send_json(cached("insights", get_insights, 30))
            if path == "/api/skills": return self.send_json(cached("skills", get_skills))
            if path == "/api/logs": return self.send_json(get_logs())
            if path == "/api/diagnostics": return self.send_json(cached("diagnostics", get_diagnostics, 15))
            if path == "/api/finanzas": return self.send_json(cached("finanzas", get_finanzas, 20))
            if path == "/api/negocios": return self.send_json({"revenue_today": "$1,240 USD", "conversion_rate": "3.2%", "active_clients": 142})
            if path == "/healthz": return self.send_json({"ok": True, "profile": PROFILE, "time": now_iso()})
            if path in ("/", "/index.html"):
                return self.send_file(STATIC_DIR / "index.html")
            safe = Path(path.lstrip("/"))
            if ".." in safe.parts:
                self.send_error(400)
                return
            if not self.headers.get("Cookie", ""):
                if path.startswith("/api/"):
                    self.send_json({"ok": False, "error": "Unauthorized"}, 401)
                    return
            return self.send_file(STATIC_DIR / safe)
        except Exception as e:
            return self.send_json({"ok": False, "error": html.escape(str(e))}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
                user = data.get("username", "")
                password = data.get("password", "")
                if hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(password, BASIC_AUTH_PASSWORD):
                    token = base64.b64encode(f"{BASIC_AUTH_USER}:{BASIC_AUTH_PASSWORD}".encode()).decode()
                    payload = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Strict")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            except Exception:
                pass
            self.send_json({"ok": False, "error": "Credenciales incorrectas"}, 401)
            return
        self.send_error(404)


def main():
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Hermes Ops Dashboard on http://0.0.0.0:{PORT}")
    print(f"Profile={PROFILE} HERMES_HOME={HERMES_HOME}")
    print(f"Basic auth={'enabled' if BASIC_AUTH_ENABLED else 'disabled'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

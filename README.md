# Hermes Ops Dashboard

App real de solo lectura para monitorear Hermes usando datos locales reales.

## Ejecutar

```bash
cd /home/devcode/hermes-ops-dashboard
HERMES_DASHBOARD_PROFILE=tecnico HERMES_DASHBOARD_PORT=8770 python3 app.py
```

Con Basic Auth opcional:

```bash
cd /home/devcode/hermes-ops-dashboard
HERMES_DASHBOARD_PROFILE=tecnico \
HERMES_DASHBOARD_PORT=8770 \
HERMES_DASHBOARD_BASIC_USER=admin \
HERMES_DASHBOARD_BASIC_PASSWORD='cambia-esta-clave' \
python3 app.py
```

Abrir:

- http://37.60.248.106:8770/
- JSON principal: http://37.60.248.106:8770/api/overview
- Cron avanzado: http://37.60.248.106:8770/api/cron
- Diagnóstico: http://37.60.248.106:8770/api/diagnostics
- Finanzas: http://37.60.248.106:8770/api/finanzas

## Datos conectados

- `hermes --profile tecnico status --all`
- `hermes --profile finanzas status --all`
- `hermes profile list`
- `hermes cron list --all`
- `/home/devcode/.hermes/profiles/tecnico/cron/jobs.json`
- `hermes insights --days 7`
- `/home/devcode/.hermes/profiles/tecnico/state.db`
- `/home/devcode/.hermes/profiles/tecnico/skills/.usage.json`
- presencia/scopes no sensibles de Google OAuth en perfil `finanzas`
- logs de gateway/agent con redacción básica de secretos

## Secciones actuales

- Overview operativo.
- Agent Mesh de perfiles.
- Cron jobs avanzado con próxima ejecución, entrega, skills, perfil y errores.
- Diagnóstico automático con score y hallazgos por área.
- Panel Finanzas para cron diario, Google OAuth, Gmail/Drive/Sheets y logs del perfil.
- Sesiones recientes, skills más usadas y logs.

## Seguridad

Versión actual: solo lectura, con Basic Auth opcional cuando se inician las variables `HERMES_DASHBOARD_BASIC_USER` y `HERMES_DASHBOARD_BASIC_PASSWORD`. Antes de exponer acciones como reiniciar gateway, ejecutar cron o editar config, mantener autenticación y agregar confirmaciones. Para producción también conviene poner Nginx + HTTPS delante del puerto 8770.

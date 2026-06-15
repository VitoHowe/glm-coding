# Frontend Migration Rollout

## Current Rollout State

The Vue SPA is implemented in parallel with the legacy Jinja page.

- `/` serves `web/dist/index.html` when the frontend has been built.
- `/legacy` always serves the previous Jinja template.
- If `web/dist/index.html` is missing, `/` falls back to the legacy template automatically.
- `/api/*` routes remain unchanged.

## Local Development

1. Start FastAPI as usual on `127.0.0.1:8787`.
2. In a second terminal, run `cd web && npm run dev`.
3. Open `http://127.0.0.1:5173` for the Vue SPA.
4. Use `http://127.0.0.1:8787/legacy` to compare the old page.

## Production-like Local Startup

`start.bat` now installs frontend dependencies if needed, builds `web/dist`, and starts FastAPI. This keeps the normal one-click Windows startup path while allowing automatic legacy fallback if npm is unavailable.

## Cutover Strategy

1. Keep the legacy template for at least one validation cycle.
2. Compare key flows in `/` and `/legacy`: import, refresh, product select, schedule update, sync, run, pause, delete, context modal, QR display.
3. If the SPA fails, remove `web/dist` or open `/legacy` to continue using the old page.
4. After validation, remove the legacy Jinja template and route in a separate cleanup change.

## Deferred Backend Optimization

The SPA intentionally preserves the old refresh behavior: `GET /api/accounts` followed by concurrent `GET /api/accounts/{id}` calls. Once the UI is stable, add a backend aggregate endpoint such as `GET /api/dashboard` to reduce N+1 polling pressure.
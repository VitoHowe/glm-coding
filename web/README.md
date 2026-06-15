# GLM Desk Web

Vue 3 + Vite + TypeScript frontend for the FastAPI GLM Desk service.

## Development

```powershell
cd web
npm install
npm run dev
```

Vite proxies `/api` and `/healthz` to `http://127.0.0.1:8787`.

## Production Build

```powershell
cd web
npm run build
```

The FastAPI app serves `web/dist/index.html` at `/` when the build exists. The legacy Jinja page remains available at `/legacy`.

## Design Notes

- UI pattern: data-dense operations dashboard.
- Component library: Naive UI, registered on demand to keep the bundle smaller.
- Accessibility: visible focus states, semantic buttons, loading states, and a mobile-safe horizontal table region.
- Fallback: if `web/dist/index.html` does not exist, FastAPI automatically renders the legacy Jinja template.
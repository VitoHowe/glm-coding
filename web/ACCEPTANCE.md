# Frontend Migration Acceptance Checklist

## Functional Regression

- [ ] Dashboard loads `/healthz`, `/api/accounts`, and per-account details.
- [ ] Import account submits label, token, and invitation code.
- [ ] Refresh button reloads dashboard state.
- [ ] Account name opens context modal with account, session, and latest task JSON.
- [ ] Product selector persists `selected_product_id` through `PATCH /api/accounts/{id}`.
- [ ] Schedule switch and time input persist `schedule_enabled` and `scheduled_start_time`.
- [ ] Sync fingerprint calls `POST /api/accounts/{id}/bootstrap?refresh_fingerprint=true`.
- [ ] Run and pause call their existing flow-control endpoints.
- [ ] Delete requires confirmation and calls the existing delete endpoint.
- [ ] Latest QR image displays when `tasks[0].qr_base64` exists.
- [ ] Empty state appears when there are no accounts.

## Build And Runtime

- [x] `npm install` succeeds in `web/`.
- [x] `npm run typecheck` succeeds.
- [x] `npm run build` succeeds.
- [x] `python -m py_compile app/web/routes.py app/main.py` succeeds.
- [x] FastAPI TestClient returns 200 for `/` and `/legacy`.
- [x] FastAPI mounts `/assets` when `web/dist/assets` exists.

## UI/UX

- [x] Uses a data-dense dashboard layout rather than copied legacy CSS.
- [x] Uses Naive UI components for cards, buttons, forms, modal, table shell, switch, select, tags, alerts, spinner, and popconfirm.
- [x] Has visible focus states for keyboard users.
- [x] Uses loading state for table and per-action buttons.
- [x] Uses horizontal overflow containment for narrow tables.
- [x] Respects `prefers-reduced-motion`.

## Risks

- Current refresh keeps the legacy N+1 request pattern. Add `GET /api/dashboard` if account counts grow.
- `start.bat` now runs npm install/build when `web/package.json` exists; first startup will be slower.
- UI 文案已集中到 `web/src/locales/zhCN.ts`，Naive UI 通过 `zhCN/dateZhCN` 完成本地化配置。后续新增页面应优先复用该文案模块，避免继续散落硬编码字符串。
- `web/dist` is ignored because it is generated. Build before production-like serving or let `start.bat` build it.
- Legacy page should remain until real account flows are manually compared.
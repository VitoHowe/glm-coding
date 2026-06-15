# Frontend API Contract

The Vue frontend keeps the existing FastAPI contract unchanged. All endpoints return the backend wrapper shape:

```ts
interface ApiResponse<T> {
  ok: boolean;
  data?: T;
  error?: { message?: string; details?: unknown };
}
```

## Dashboard bootstrap

- `GET /healthz` -> `HealthPayload`, used for transport/status metadata.
- `GET /api/accounts` -> `PublicAccountRecord[]`, used as the dashboard index.
- `GET /api/accounts/{account_id}` -> `AccountDetailResponse`, used for row details, products, latest task, QR, and context modal.

Current dashboard refresh intentionally preserves the legacy N+1 pattern: list accounts first, then fetch details concurrently. A future `GET /api/dashboard` aggregate endpoint should replace this once the SPA is stable.

## Account management

- `POST /api/accounts/import` with `AccountImportPayload` -> imports and syncs an account.
- `PATCH /api/accounts/{account_id}` with `AccountPreferencesPayload` -> updates selected product and schedule preferences.
- `DELETE /api/accounts/{account_id}` -> deletes account and local cache.
- `POST /api/accounts/{account_id}/bootstrap?refresh_fingerprint=true` -> syncs account context and rotates fingerprint.

## Flow controls

- `POST /api/accounts/{account_id}/run` -> starts the payment flow.
- `POST /api/accounts/{account_id}/pause` -> requests pause for the active flow.

## Error handling

`apiClient` unwraps `data` on success and throws `ApiClientError` on failed HTTP status or `{ ok: false }`. The dashboard composable converts thrown errors into a visible status banner and clears per-action loading state in `finally`.
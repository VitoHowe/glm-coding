import type {
  AccountDetailResponse,
  AccountImportPayload,
  AccountPreferencesPayload,
  ApiResponse,
  HealthPayload,
  NetworkEgressMode,
  NetworkModePayload,
  ProxyPoolSourcesPayload,
  RuntimeLogsPayload,
  PublicAccountRecord,
  TicketPoolEntry,
} from "../types/api";

export class ApiClientError extends Error {
  details?: unknown;

  constructor(message: string, details?: unknown) {
    super(message);
    this.name = "ApiClientError";
    this.details = details;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = (await response
    .json()
    .catch(() => null)) as ApiResponse<T> | null;
  if (!response.ok || !payload?.ok) {
    throw new ApiClientError(
      payload?.error?.message || `HTTP ${response.status}`,
      payload?.error?.details,
    );
  }
  return payload.data as T;
}

export const api = {
  health: () => request<HealthPayload>("/healthz"),
  todayLogs: () => request<RuntimeLogsPayload>("/api/logs/today"),
  getNetworkMode: () => request<NetworkModePayload>("/api/network-mode"),
  updateNetworkMode: (mode: NetworkEgressMode) =>
    request<NetworkModePayload>("/api/network-mode", {
      method: "PATCH",
      body: JSON.stringify({ mode }),
    }),
  getProxyPoolSources: () =>
    request<ProxyPoolSourcesPayload>("/api/proxy-pool/sources"),
  saveProxyPoolSources: (content: string) =>
    request<ProxyPoolSourcesPayload>("/api/proxy-pool/sources", {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),
  listAccounts: () => request<PublicAccountRecord[]>("/api/accounts"),
  getAccount: (accountId: string) =>
    request<AccountDetailResponse>(
      `/api/accounts/${encodeURIComponent(accountId)}`,
    ),
  importAccount: (payload: AccountImportPayload) =>
    request<AccountDetailResponse>("/api/accounts/import", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteAccount: (accountId: string) =>
    request<unknown>(`/api/accounts/${encodeURIComponent(accountId)}`, {
      method: "DELETE",
    }),
  updateAccount: (accountId: string, payload: AccountPreferencesPayload) =>
    request<AccountDetailResponse>(
      `/api/accounts/${encodeURIComponent(accountId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    ),
  bootstrapAccount: (accountId: string, refreshFingerprint = true) =>
    request<AccountDetailResponse>(
      `/api/accounts/${encodeURIComponent(accountId)}/bootstrap?refresh_fingerprint=${String(refreshFingerprint)}`,
      { method: "POST" },
    ),
  runAccount: (accountId: string) =>
    request<unknown>(`/api/accounts/${encodeURIComponent(accountId)}/run`, {
      method: "POST",
    }),
  startStockMonitor: (accountId: string) =>
    request<unknown>(
      `/api/accounts/${encodeURIComponent(accountId)}/stock-monitor/start`,
      {
        method: "POST",
      },
    ),
  stopStockMonitor: (accountId: string) =>
    request<unknown>(
      `/api/accounts/${encodeURIComponent(accountId)}/stock-monitor/stop`,
      {
        method: "POST",
      },
    ),
  pauseAccount: (accountId: string) =>
    request<unknown>(`/api/accounts/${encodeURIComponent(accountId)}/pause`, {
      method: "POST",
    }),
  getTicketPool: (accountId: string) =>
    request<{ pool: TicketPoolEntry[]; collected: number; target: number }>(
      `/api/accounts/${encodeURIComponent(accountId)}/tickets`,
    ),
  clearTicketPool: (accountId: string) =>
    request<unknown>(`/api/accounts/${encodeURIComponent(accountId)}/tickets`, {
      method: "DELETE",
    }),
};

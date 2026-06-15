import { computed, onBeforeUnmount, ref } from "vue";
import { zhCN as copy } from "../locales/zhCN";
import { api } from "../services/api";
import type {
  AccountDetailResponse,
  AccountImportPayload,
  AccountPreferencesPayload,
  HealthPayload
} from "../types/api";

export type BannerTone = "success" | "warning" | "error" | "info";

export interface StatusBanner {
  tone: BannerTone;
  text: string;
  linkText?: string;
  linkHref?: string;
}

const POLL_INTERVAL_MS = 5000;

export function useDashboard() {
  const details = ref<AccountDetailResponse[]>([]);
  const health = ref<HealthPayload | null>(null);
  const loading = ref(false);
  const actionKey = ref("");
  const banner = ref<StatusBanner | null>(null);
  let pollTimer: number | undefined;
  let qrReminderReady = false;
  let knownQrTaskKeys = new Set<string>();

  const accountsTotal = computed(() => details.value.length);
  const runningTotal = computed(
    () =>
      details.value.filter(({ account }) =>
        ["running", "pause_requested"].includes(String(account.last_schedule_status || "").toLowerCase())
      ).length
  );
  const qrTotal = computed(() => details.value.filter(({ tasks }) => Boolean(tasks?.[0]?.qr_base64)).length);

  function setBanner(text: string, tone: BannerTone = "success", link?: { text: string; href: string }) {
    banner.value = { text, tone, linkText: link?.text, linkHref: link?.href };
  }

  function clearBanner() {
    banner.value = null;
  }

  function qrTaskKey(detail: AccountDetailResponse) {
    const task = detail.tasks?.[0];
    if (!task?.qr_base64) {
      return "";
    }
    return `${detail.account.id}:${task.id || task.biz_id || task.updated_at || ""}`;
  }

  function qrImageUrl(detail: AccountDetailResponse) {
    const task = detail.tasks?.[0];
    if (!task?.id || !task.qr_base64) {
      return "";
    }
    return `/api/accounts/${encodeURIComponent(detail.account.id)}/tasks/${encodeURIComponent(task.id)}/qr.png`;
  }

  function applyQrGeneratedReminder(nextDetails: AccountDetailResponse[]) {
    const currentKeys = new Set<string>();
    const newQrDetails: AccountDetailResponse[] = [];
    const shouldNotify = qrReminderReady && nextDetails.length > 1;

    for (const detail of nextDetails) {
      const key = qrTaskKey(detail);
      if (!key) {
        continue;
      }
      currentKeys.add(key);
      if (shouldNotify && !knownQrTaskKeys.has(key)) {
        newQrDetails.push(detail);
      }
    }

    knownQrTaskKeys = currentKeys;
    if (!qrReminderReady) {
      qrReminderReady = true;
      return false;
    }
    if (newQrDetails.length === 0) {
      return false;
    }

    const labels = newQrDetails.map((detail) => detail.account.label || detail.account.id);
    const firstTask = newQrDetails[0]?.tasks?.[0];
    const text =
      newQrDetails.length === 1
        ? copy.feedback.qrGenerated(labels[0], firstTask?.biz_id || "")
        : copy.feedback.qrGeneratedBatch(newQrDetails.length, labels.slice(0, 3).join("、"));
    const qrUrl = newQrDetails.length === 1 ? qrImageUrl(newQrDetails[0]) : "";
    setBanner(text, "warning", qrUrl ? { text: copy.feedback.openQr, href: qrUrl } : undefined);
    return true;
  }

  async function refreshDashboard(silent = false) {
    if (!silent) {
      loading.value = true;
    }
    try {
      const [healthPayload, accounts] = await Promise.all([api.health(), api.listAccounts()]);
      const detailPayloads = await Promise.all(accounts.map((account) => api.getAccount(account.id)));
      health.value = healthPayload;
      details.value = detailPayloads;
      const qrReminderShown = applyQrGeneratedReminder(detailPayloads);
      if (!silent && !qrReminderShown) {
        setBanner(copy.feedback.dashboardRefreshed, "success");
      }
    } catch (error) {
      setBanner(error instanceof Error ? error.message : copy.feedback.dashboardRefreshFailed, "error");
    } finally {
      loading.value = false;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = window.setInterval(() => {
      void refreshDashboard(true);
    }, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = undefined;
    }
  }

  async function runAction(key: string, successText: string, action: () => Promise<unknown>) {
    actionKey.value = key;
    try {
      await action();
      setBanner(successText, "success");
      await refreshDashboard(true);
    } catch (error) {
      setBanner(error instanceof Error ? error.message : copy.feedback.operationFailed, "error");
    } finally {
      actionKey.value = "";
    }
  }

  async function importAccount(payload: AccountImportPayload) {
    await runAction("import", copy.feedback.accountImported, () => api.importAccount(payload));
  }

  async function updatePreferences(accountId: string, payload: AccountPreferencesPayload) {
    await runAction(`prefs:${accountId}`, copy.feedback.preferencesSaved, () => api.updateAccount(accountId, payload));
  }

  async function syncAccount(accountId: string) {
    await runAction(`sync:${accountId}`, copy.feedback.accountSynced, () => api.bootstrapAccount(accountId, true));
  }

  async function deleteAccount(accountId: string) {
    await runAction(`delete:${accountId}`, copy.feedback.accountDeleted, () => api.deleteAccount(accountId));
  }

  async function runAccount(accountId: string) {
    await runAction(`run:${accountId}`, copy.feedback.paymentStarted, () => api.runAccount(accountId));
  }

  async function probeAccount(accountId: string) {
    await runAction(`probe:${accountId}`, copy.feedback.probeStarted, () => api.probeAccount(accountId));
  }

  async function pauseAccount(accountId: string) {
    await runAction(`pause:${accountId}`, copy.feedback.pauseRequested, () => api.pauseAccount(accountId));
  }

  onBeforeUnmount(stopPolling);

  return {
    actionKey,
    accountsTotal,
    banner,
    clearBanner,
    deleteAccount,
    details,
    health,
    importAccount,
    loading,
    pauseAccount,
    probeAccount,
    qrTotal,
    refreshDashboard,
    runningTotal,
    runAccount,
    startPolling,
    syncAccount,
    updatePreferences
  };
}

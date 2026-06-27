import { computed, onBeforeUnmount, ref } from "vue";
import { zhCN as copy } from "../locales/zhCN";
import { api } from "../services/api";
import type {
  AccountDetailResponse,
  AccountImportPayload,
  AccountPreferencesPayload,
  HealthPayload,
  NetworkEgressMode,
} from "../types/api";

export type BannerTone = "success" | "warning" | "error" | "info";

export interface StatusBannerLink {
  text: string;
  href: string;
}

export interface StatusBanner {
  tone: BannerTone;
  text: string;
  linkText?: string;
  linkHref?: string;
  links?: StatusBannerLink[];
}

const POLL_INTERVAL_MS = 5000;

export function useDashboard() {
  const details = ref<AccountDetailResponse[]>([]);
  const health = ref<HealthPayload | null>(null);
  const loading = ref(false);
  const actionKey = ref("");
  const banner = ref<StatusBanner | null>(null);
  let pollTimer: number | undefined;
  let titleTimer: number | undefined;
  let qrBeepTimer: number | undefined;
  let qrBeepStopTimer: number | undefined;
  let qrAudio: AudioContext | undefined;
  let qrReminderReady = false;
  let knownQrTaskKeys = new Set<string>();
  const pendingQrReminders = new Map<string, { label: string; bizId: string; href: string }>();
  const baseTitle = typeof document === "undefined" ? copy.app.title : document.title || copy.app.title;

  const accountsTotal = computed(() => details.value.length);
  const runningTotal = computed(
    () =>
      details.value.filter(({ account }) =>
        ["running", "pause_requested", "stock_monitoring"].includes(
          String(account.last_schedule_status || "").toLowerCase(),
        ),
      ).length,
  );
  const qrTotal = computed(
    () =>
      details.value.filter(({ tasks }) => Boolean(tasks?.[0]?.qr_base64))
        .length,
  );

  function setBanner(
    text: string,
    tone: BannerTone = "success",
    link?: { text: string; href: string },
    links?: StatusBannerLink[],
  ) {
    banner.value = { text, tone, linkText: link?.text, linkHref: link?.href, links };
  }

  function clearBanner() {
    banner.value = null;
    pendingQrReminders.clear();
    stopTitleReminder();
  }

  function stopTitleReminder() {
    if (titleTimer) {
      window.clearInterval(titleTimer);
      titleTimer = undefined;
    }
    if (typeof document !== "undefined") {
      document.title = baseTitle;
    }
  }

  function startTitleReminder(count: number) {
    if (typeof document === "undefined") {
      return;
    }
    stopTitleReminder();
    let active = true;
    const alertTitle = count > 1 ? `(${count}) 支付二维码已生成` : "支付二维码已生成";
    document.title = alertTitle;
    titleTimer = window.setInterval(() => {
      document.title = active ? alertTitle : baseTitle;
      active = !active;
    }, 900);
  }

  function getQrAudioContext() {
    if (typeof window === "undefined") {
      return undefined;
    }
    if (qrAudio) {
      return qrAudio;
    }
    const AudioContextCtor =
      window.AudioContext ||
      (window as typeof window & { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!AudioContextCtor) {
      return undefined;
    }
    try {
      qrAudio = new AudioContextCtor();
      return qrAudio;
    } catch {
      return undefined;
    }
  }

  function unlockQrAudio() {
    const audio = getQrAudioContext();
    if (!audio) {
      return;
    }
    try {
      if (audio.state === "suspended") {
        void audio.resume();
      }
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      const startAt = audio.currentTime;
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(880, startAt);
      gain.gain.setValueAtTime(0.0001, startAt);
      gain.gain.exponentialRampToValueAtTime(0.0001, startAt + 0.03);
      oscillator.connect(gain);
      gain.connect(audio.destination);
      oscillator.start(startAt);
      oscillator.stop(startAt + 0.035);
    } catch {
      // Browser autoplay policies can still reject unlock outside user gestures.
    }
  }

  async function showQrNotification(text: string, links: StatusBannerLink[]) {
    if (typeof window === "undefined" || !("Notification" in window)) {
      return;
    }
    try {
      let permission = Notification.permission;
      if (permission === "default") {
        permission = await Notification.requestPermission();
      }
      if (permission !== "granted") {
        return;
      }
      const notification = new Notification(copy.app.title, {
        body: text,
        requireInteraction: true,
        tag: "aegisflow-qr-generated",
      });
      const firstLink = links[0]?.href;
      if (firstLink) {
        notification.onclick = () => {
          window.focus();
          window.open(firstLink, "_blank", "noopener,noreferrer");
          notification.close();
        };
      }
    } catch {
      // Notification permission can be blocked by browser policy.
    }
  }

  function playQrBeepBurst(count: number) {
    const audio = getQrAudioContext();
    if (!audio) {
      return;
    }
    try {
      if (audio.state === "suspended") {
        void audio.resume();
      }
      const beepCount = Math.min(Math.max(count, 1), 3);
      for (let i = 0; i < beepCount; i += 1) {
        const oscillator = audio.createOscillator();
        const gain = audio.createGain();
        const startAt = audio.currentTime + 0.02 + i * 0.22;
        oscillator.type = "sine";
        oscillator.frequency.setValueAtTime(880, startAt);
        gain.gain.setValueAtTime(0.0001, startAt);
        gain.gain.exponentialRampToValueAtTime(0.28, startAt + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, startAt + 0.16);
        oscillator.connect(gain);
        gain.connect(audio.destination);
        oscillator.start(startAt);
        oscillator.stop(startAt + 0.18);
      }
    } catch {
      // Browser autoplay policies can block audio before user interaction.
    }
  }

  function stopQrBeep() {
    if (qrBeepTimer) {
      window.clearInterval(qrBeepTimer);
      qrBeepTimer = undefined;
    }
    if (qrBeepStopTimer) {
      window.clearTimeout(qrBeepStopTimer);
      qrBeepStopTimer = undefined;
    }
  }

  function playQrBeep(count: number) {
    const durationMs = 5000;
    const intervalMs = 550;
    stopQrBeep();
    playQrBeepBurst(count);
    qrBeepTimer = window.setInterval(() => {
      playQrBeepBurst(count);
    }, intervalMs);
    qrBeepStopTimer = window.setTimeout(stopQrBeep, durationMs);
  }

  function triggerQrAttention(count: number, text: string, links: StatusBannerLink[]) {
    startTitleReminder(count);
    void playQrBeep(count);
    void showQrNotification(text, links);
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
    const shouldNotify = qrReminderReady;

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

    for (const detail of newQrDetails) {
      const key = qrTaskKey(detail);
      const href = qrImageUrl(detail);
      if (!key || !href) {
        continue;
      }
      pendingQrReminders.set(key, {
        label: detail.account.label || detail.account.id,
        bizId: detail.tasks?.[0]?.biz_id || "",
        href,
      });
    }

    const reminders = Array.from(pendingQrReminders.values());
    const labels = reminders.map((item) => item.label);
    const text =
      reminders.length === 1
        ? copy.feedback.qrGenerated(labels[0], reminders[0]?.bizId || "")
        : copy.feedback.qrGeneratedBatch(
            reminders.length,
            labels.slice(0, 4).join("、"),
          );
    const links = reminders.map((item) => ({
      text: copy.feedback.openQrFor(item.label),
      href: item.href,
    }));
    setBanner(
      text,
      "warning",
      links.length === 1 ? { text: copy.feedback.openQr, href: links[0].href } : undefined,
      links.length > 1 ? links : undefined,
    );
    triggerQrAttention(newQrDetails.length, text, links);
    return true;
  }

  async function refreshDashboard(silent = false) {
    if (!silent) {
      loading.value = true;
    }
    try {
      const [healthPayload, accounts] = await Promise.all([
        api.health(),
        api.listAccounts(),
      ]);
      const detailPayloads = await Promise.all(
        accounts.map((account) => api.getAccount(account.id)),
      );
      health.value = healthPayload;
      details.value = detailPayloads;
      const qrReminderShown = applyQrGeneratedReminder(detailPayloads);
      if (!silent && !qrReminderShown) {
        setBanner(copy.feedback.dashboardRefreshed, "success");
      }
    } catch (error) {
      setBanner(
        error instanceof Error
          ? error.message
          : copy.feedback.dashboardRefreshFailed,
        "error",
      );
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

  function handleWindowFocus() {
    stopTitleReminder();
  }

  if (typeof window !== "undefined") {
    window.addEventListener("focus", handleWindowFocus);
    window.addEventListener("pointerdown", unlockQrAudio, { passive: true });
    window.addEventListener("keydown", unlockQrAudio);
  }

  async function runAction(
    key: string,
    successText: string,
    action: () => Promise<unknown>,
  ) {
    if (actionKey.value) {
      return; // 已有操作进行中，忽略重复触发（防抖）
    }
    actionKey.value = key;
    unlockQrAudio();
    try {
      await action();
      setBanner(successText, "success");
      await refreshDashboard(true);
    } catch (error) {
      setBanner(
        error instanceof Error ? error.message : copy.feedback.operationFailed,
        "error",
      );
    } finally {
      actionKey.value = "";
    }
  }

  async function importAccount(payload: AccountImportPayload) {
    await runAction("import", copy.feedback.accountImported, () =>
      api.importAccount(payload),
    );
  }

  async function updatePreferences(
    accountId: string,
    payload: AccountPreferencesPayload,
  ) {
    await runAction(`prefs:${accountId}`, copy.feedback.preferencesSaved, () =>
      api.updateAccount(accountId, payload),
    );
  }

  async function syncAccount(accountId: string) {
    await runAction(`sync:${accountId}`, copy.feedback.accountSynced, () =>
      api.bootstrapAccount(accountId, true),
    );
  }

  async function deleteAccount(accountId: string) {
    await runAction(`delete:${accountId}`, copy.feedback.accountDeleted, () =>
      api.deleteAccount(accountId),
    );
  }

  async function runAccount(accountId: string) {
    await runAction(`run:${accountId}`, copy.feedback.paymentStarted, () =>
      api.runAccount(accountId),
    );
  }

  async function startStockMonitor(accountId: string) {
    await runAction(`stock:${accountId}`, copy.feedback.stockMonitorStarted, () =>
      api.startStockMonitor(accountId),
    );
  }

  async function stopStockMonitor(accountId: string) {
    await runAction(`stock:${accountId}`, copy.feedback.stockMonitorStopped, () =>
      api.stopStockMonitor(accountId),
    );
  }

  async function pauseAccount(accountId: string) {
    await runAction(`pause:${accountId}`, copy.feedback.pauseRequested, () =>
      api.pauseAccount(accountId),
    );
  }

  async function clearTicketPool(accountId: string) {
    await runAction(
      `clearpool:${accountId}`,
      copy.feedback.ticketPoolCleared,
      () => api.clearTicketPool(accountId),
    );
  }

  async function updateNetworkMode(mode: NetworkEgressMode) {
    await runAction("network-mode", copy.feedback.networkModeSaved, () =>
      api.updateNetworkMode(mode),
    );
  }

  const networkModeBusy = computed(() => actionKey.value === "network-mode");

  async function saveProxyPoolSources(content: string) {
    await runAction("proxy-pool-sources", copy.feedback.proxyPoolSourcesSaved, () =>
      api.saveProxyPoolSources(content),
    );
    // 代理池延迟检测是异步的，稍后再刷新一次，让 web 状态反映检测结果
    window.setTimeout(() => {
      void refreshDashboard(true).catch(() => undefined);
    }, 3000);
  }

  onBeforeUnmount(() => {
    stopPolling();
    stopTitleReminder();
    stopQrBeep();
    window.removeEventListener("focus", handleWindowFocus);
    window.removeEventListener("pointerdown", unlockQrAudio);
    window.removeEventListener("keydown", unlockQrAudio);
    void qrAudio?.close().catch(() => undefined);
  });

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
    clearTicketPool,
    pauseAccount,
    qrTotal,
    refreshDashboard,
    runningTotal,
    runAccount,
    startPolling,
    startStockMonitor,
    stopStockMonitor,
    syncAccount,
    updateNetworkMode,
    updatePreferences,
    networkModeBusy,
    saveProxyPoolSources,
  };
}

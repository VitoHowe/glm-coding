<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { dateZhCN, zhCN as naiveZhCN } from "naive-ui";
import AccountContextModal from "./components/AccountContextModal.vue";
import AccountTable from "./components/AccountTable.vue";
import AppShell from "./components/AppShell.vue";
import DashboardStats from "./components/DashboardStats.vue";
import ImportAccountModal from "./components/ImportAccountModal.vue";
import StatusBanner from "./components/StatusBanner.vue";
import { useDashboard } from "./composables/useDashboard";
import { zhCN as copy } from "./locales/zhCN";
import { api } from "./services/api";
import type { AccountDetailResponse, AccountImportPayload } from "./types/api";

const dashboard = useDashboard();
const showImport = ref(false);
const showContext = ref(false);
const showLogs = ref(false);
const logLoading = ref(false);
const logText = ref("");
const logMeta = ref("");
const selectedDetail = ref<AccountDetailResponse | null>(null);

const importing = computed(() => dashboard.actionKey.value === "import");

const themeOverrides = {
  common: {
    primaryColor: "#c99724",
    primaryColorHover: "#b8891e",
    primaryColorPressed: "#8f6a16",
    borderRadius: "14px",
    fontFamily: "Fira Sans, Segoe UI, sans-serif",
    fontFamilyMono: "Fira Code, ui-monospace, SFMono-Regular, Menlo, monospace"
  },
  Button: {
    heightLarge: "46px",
    borderRadiusMedium: "12px"
  },
  Card: {
    borderRadius: "20px"
  }
};

onMounted(async () => {
  await dashboard.refreshDashboard();
  dashboard.startPolling();
});

function openContext(detail: AccountDetailResponse) {
  selectedDetail.value = detail;
  showContext.value = true;
}

async function submitImport(payload: AccountImportPayload) {
  await dashboard.importAccount(payload);
  showImport.value = false;
}

async function updateProduct(accountId: string, productId: string) {
  if (!productId) {
    return;
  }
  await dashboard.updatePreferences(accountId, { selected_product_id: productId });
}

async function updateSchedule(accountId: string, enabled: boolean, time: string) {
  await dashboard.updatePreferences(accountId, {
    schedule_enabled: enabled,
    scheduled_start_time: time
  });
}

async function updatePreviewConcurrency(accountId: string, value: number) {
  await dashboard.updatePreferences(accountId, {
    preview_concurrency: value
  });
}

async function openLogs() {
  showLogs.value = true;
  logLoading.value = true;
  try {
    const payload = await api.todayLogs();
    logText.value = payload.text || "";
    logMeta.value = payload.truncated
      ? `${payload.date} / 最近 ${payload.lines.length} 行，共 ${payload.total || payload.lines.length} 行`
      : `${payload.date} / 共 ${payload.lines.length} 行`;
  } catch (error) {
    logText.value = error instanceof Error ? error.message : "日志加载失败";
    logMeta.value = "日志加载失败";
  } finally {
    logLoading.value = false;
  }
}
</script>

<template>
  <n-config-provider :locale="naiveZhCN" :date-locale="dateZhCN" :theme-overrides="themeOverrides">
    <AppShell :health="dashboard.health.value" @logs="openLogs" @refresh="dashboard.refreshDashboard()" @import="showImport = true">
      <StatusBanner :banner="dashboard.banner.value" @close="dashboard.clearBanner" />
      <DashboardStats
        :accounts-total="dashboard.accountsTotal.value"
        :running-total="dashboard.runningTotal.value"
        :qr-total="dashboard.qrTotal.value"
      />
      <AccountTable
        :details="dashboard.details.value"
        :loading="dashboard.loading.value"
        :action-key="dashboard.actionKey.value"
        @open-context="openContext"
        @select-product="updateProduct"
        @update-schedule="updateSchedule"
        @update-preview-concurrency="updatePreviewConcurrency"
        @sync="dashboard.syncAccount"
        @delete="dashboard.deleteAccount"
        @run="dashboard.runAccount"
        @probe="dashboard.probeAccount"
        @pause="dashboard.pauseAccount"
      />
    </AppShell>

    <ImportAccountModal v-model:show="showImport" :loading="importing" @submit="submitImport" />
    <AccountContextModal v-model:show="showContext" :detail="selectedDetail" />
    <n-drawer v-model:show="showLogs" display-directive="show" placement="right" width="min(960px, 92vw)">
      <n-drawer-content :title="copy.app.logsTitle" closable>
        <div class="logs-toolbar">
          <span>{{ logMeta || copy.app.logsToday }}</span>
          <n-button size="small" secondary :loading="logLoading" @click="openLogs">{{ copy.app.refreshLogs }}</n-button>
        </div>
        <n-input
          class="runtime-log-viewer"
          type="textarea"
          readonly
          :autosize="false"
          :value="logText || copy.app.noLogs"
        />
      </n-drawer-content>
    </n-drawer>
  </n-config-provider>
</template>

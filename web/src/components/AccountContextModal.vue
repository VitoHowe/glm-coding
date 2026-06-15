<script setup lang="ts">
import { computed } from "vue";
import { zhCN as copy } from "../locales/zhCN";
import type { AccountDetailResponse } from "../types/api";

const props = defineProps<{
  show: boolean;
  detail: AccountDetailResponse | null;
}>();

const emit = defineEmits<{
  "update:show": [value: boolean];
}>();

const contextJson = computed(() => {
  if (!props.detail) {
    return "{}";
  }
  return JSON.stringify(
    {
      account: props.detail.account,
      session: props.detail.session,
      latestTask: props.detail.tasks?.[0] || null
    },
    null,
    2
  );
});
</script>

<template>
  <n-modal :show="show" preset="card" class="desk-modal wide" :title="copy.contextModal.title" @update:show="emit('update:show', $event)">
    <template v-if="detail">
      <div class="context-grid">
        <n-card :bordered="false"><span>{{ copy.contextModal.label }}</span><strong>{{ detail.account.label }}</strong></n-card>
        <n-card :bordered="false"><span>{{ copy.contextModal.customer }}</span><strong>{{ detail.session.customer_number || "-" }}</strong></n-card>
        <n-card :bordered="false"><span>{{ copy.contextModal.name }}</span><strong>{{ detail.session.customer_name || "-" }}</strong></n-card>
        <n-card :bordered="false"><span>{{ copy.contextModal.status }}</span><strong>{{ detail.account.account_status || copy.contextModal.unchecked }}</strong></n-card>
        <n-card :bordered="false"><span>{{ copy.contextModal.schedule }}</span><strong>{{ detail.account.schedule_enabled ? detail.account.scheduled_start_time : copy.contextModal.disabled }}</strong></n-card>
        <n-card :bordered="false"><span>{{ copy.contextModal.browser }}</span><strong>{{ detail.account.browser_impersonate || "-" }}</strong></n-card>
      </div>
      <n-input class="json-view" :value="contextJson" type="textarea" readonly :autosize="{ minRows: 16, maxRows: 22 }" />
    </template>
  </n-modal>
</template>
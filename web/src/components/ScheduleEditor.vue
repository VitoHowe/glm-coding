<script setup lang="ts">
import { computed } from "vue";
import { zhCN as copy } from "../locales/zhCN";

const props = defineProps<{
  accountId: string;
  enabled: boolean;
  time: string;
}>();

const emit = defineEmits<{
  update: [accountId: string, enabled: boolean, time: string];
}>();

const normalizedTime = computed(() => props.time || "00:00:00");

function updateEnabled(enabled: boolean) {
  emit("update", props.accountId, enabled, normalizedTime.value);
}

function updateTime(event: Event) {
  const target = event.target as HTMLInputElement;
  emit("update", props.accountId, props.enabled, target.value);
}
</script>

<template>
  <div class="schedule-cell">
    <n-switch :value="enabled" :aria-label="copy.schedule.enableLabel" @update:value="updateEnabled" />
    <input
      v-if="!enabled"
      class="time-input"
      type="time"
      step="1"
      :value="normalizedTime"
      :aria-label="copy.schedule.timeLabel"
      @change="updateTime"
    />
    <span v-else class="schedule-time-readonly">{{ normalizedTime }}</span>
  </div>
</template>

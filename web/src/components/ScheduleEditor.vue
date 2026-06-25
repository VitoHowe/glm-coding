<script setup lang="ts">
import { computed } from "vue";
import { zhCN as copy } from "../locales/zhCN";

const props = defineProps<{
  accountId: string;
  enabled: boolean;
  time: string;
  warmupLeadSeconds: number;
}>();

const emit = defineEmits<{
  update: [accountId: string, enabled: boolean, time: string, warmupLeadSeconds: number];
}>();

const normalizedTime = computed(() => props.time || "00:00:00");
const normalizedWarmupLeadSeconds = computed(() =>
  Math.max(0, Math.min(120, Number(props.warmupLeadSeconds || 0))),
);

function updateEnabled(enabled: boolean) {
  emit("update", props.accountId, enabled, normalizedTime.value, normalizedWarmupLeadSeconds.value);
}

function updateTime(event: Event) {
  const target = event.target as HTMLInputElement;
  emit("update", props.accountId, props.enabled, target.value, normalizedWarmupLeadSeconds.value);
}

function updateWarmupLead(event: Event) {
  const raw = parseInt((event.target as HTMLInputElement).value, 10);
  const seconds = Math.max(0, Math.min(120, isNaN(raw) ? 0 : raw));
  emit("update", props.accountId, props.enabled, normalizedTime.value, seconds);
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
    <label class="schedule-lead-field">
      <span>{{ copy.schedule.warmupLeadShort }}</span>
      <input
        class="schedule-lead-input"
        type="number"
        min="0"
        max="120"
        step="1"
        :value="normalizedWarmupLeadSeconds"
        :title="copy.schedule.warmupLeadHint"
        :aria-label="copy.schedule.warmupLeadLabel"
        @change="updateWarmupLead"
      />
    </label>
  </div>
</template>

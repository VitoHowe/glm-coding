<script setup lang="ts">
import { zhCN as copy } from "../locales/zhCN";
import type { HealthPayload } from "../types/api";

defineProps<{
  health: HealthPayload | null;
}>();

const emit = defineEmits<{
  logs: [];
  refresh: [];
  import: [];
}>();
</script>

<template>
  <main class="desk-shell">
    <header class="command-bar" :aria-label="copy.app.commandCenterLabel">
      <div class="command-title">
        <strong>{{ copy.app.title }}</strong>
        <span>{{ copy.app.eyebrow }}</span>
      </div>
      <div class="command-actions" :aria-label="copy.app.primaryActionsLabel">
        <n-tag round type="info">{{ health?.transport || copy.app.transportPending }}</n-tag>
        <n-button secondary @click="emit('logs')">{{ copy.app.viewLogs }}</n-button>
        <n-button secondary @click="emit('refresh')">{{ copy.app.refresh }}</n-button>
        <n-button type="primary" @click="emit('import')">{{ copy.app.importAccount }}</n-button>
      </div>
    </header>

    <slot />
  </main>
</template>

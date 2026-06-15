<script setup lang="ts">
import { zhCN as copy } from "../locales/zhCN";

defineProps<{
  accountsTotal: number;
  runningTotal: number;
  qrTotal: number;
}>();
</script>

<template>
  <section class="stats-grid" :aria-label="copy.stats.ariaLabel">
    <n-card class="stat-card" :bordered="false">
      <span class="stat-label">{{ copy.stats.accounts }}</span>
      <strong>{{ accountsTotal }}</strong>
      <small>{{ copy.stats.accountsHint }}</small>
    </n-card>
    <n-card class="stat-card accent" :bordered="false">
      <span class="stat-label">{{ copy.stats.running }}</span>
      <strong>{{ runningTotal }}</strong>
      <small>{{ copy.stats.runningHint }}</small>
    </n-card>
    <n-card class="stat-card warm" :bordered="false">
      <span class="stat-label">{{ copy.stats.qrReady }}</span>
      <strong>{{ qrTotal }}</strong>
      <small>{{ copy.stats.qrReadyHint }}</small>
    </n-card>
  </section>
</template>
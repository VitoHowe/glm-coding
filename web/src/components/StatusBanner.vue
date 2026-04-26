<script setup lang="ts">
import type { StatusBanner } from "../composables/useDashboard";

defineProps<{
  banner: StatusBanner | null;
}>();

const emit = defineEmits<{
  close: [];
}>();
</script>

<template>
  <n-alert
    v-if="banner"
    class="status-banner"
    :type="banner.tone"
    closable
    role="status"
    @close="emit('close')"
  >
    <span>{{ banner.text }}</span>
    <a
      v-if="banner.linkHref"
      class="status-banner-link"
      :href="banner.linkHref"
      target="_blank"
      rel="noopener noreferrer"
    >
      {{ banner.linkText || "打开" }}
    </a>
  </n-alert>
</template>

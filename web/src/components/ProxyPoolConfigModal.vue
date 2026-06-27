<script setup lang="ts">
import { ref, watch } from "vue";
import { zhCN as copy } from "../locales/zhCN";
import { api } from "../services/api";

const props = defineProps<{
  show: boolean;
  loading: boolean;
}>();

const emit = defineEmits<{
  "update:show": [value: boolean];
  submit: [content: string];
}>();

const content = ref("");
const sourcePath = ref("");
const loadingContent = ref(false);
const loadError = ref("");

// 打开时拉取当前代理源文件内容
watch(
  () => props.show,
  async (show) => {
    if (!show) {
      return;
    }
    content.value = "";
    sourcePath.value = "";
    loadError.value = "";
    loadingContent.value = true;
    try {
      const data = await api.getProxyPoolSources();
      content.value = data.content || "";
      sourcePath.value = data.path || "";
    } catch (error) {
      loadError.value = error instanceof Error ? error.message : copy.feedback.operationFailed;
    } finally {
      loadingContent.value = false;
    }
  },
);

function submit() {
  emit("submit", content.value);
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    class="desk-modal"
    :title="copy.proxyPoolConfig.title"
    @update:show="emit('update:show', $event)"
  >
    <p class="modal-copy muted">
      {{ copy.proxyPoolConfig.hint }}<code>{{ sourcePath || "good_proxies.txt" }}</code>
    </p>
    <n-input
      v-model:value="content"
      type="textarea"
      :autosize="{ minRows: 12, maxRows: 24 }"
      :disabled="loadingContent"
      :placeholder="copy.proxyPoolConfig.placeholder"
    />
    <p v-if="loadError" class="modal-copy muted">{{ loadError }}</p>
    <div class="modal-actions">
      <n-button secondary @click="emit('update:show', false)">
        {{ copy.proxyPoolConfig.cancel }}
      </n-button>
      <n-button
        type="primary"
        :loading="loading"
        :disabled="loadingContent"
        @click="submit"
      >
        {{ copy.proxyPoolConfig.submit }}
      </n-button>
    </div>
  </n-modal>
</template>

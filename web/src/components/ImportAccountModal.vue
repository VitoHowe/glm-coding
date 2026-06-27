<script setup lang="ts">
import { reactive, watch } from "vue";
import { zhCN as copy } from "../locales/zhCN";
import type { AccountImportPayload } from "../types/api";

const props = defineProps<{
  show: boolean;
  loading: boolean;
}>();

const emit = defineEmits<{
  "update:show": [value: boolean];
  submit: [payload: AccountImportPayload];
}>();

const form = reactive({
  label: "",
  token: ""
});

watch(
  () => props.show,
  (show) => {
    if (!show) {
      form.label = "";
      form.token = "";
    }
  }
);

function submit() {
  emit("submit", {
    label: form.label.trim(),
    token: form.token.trim()
  });
}
</script>

<template>
  <n-modal :show="show" preset="card" class="desk-modal" :title="copy.importModal.title" @update:show="emit('update:show', $event)">
    <n-form label-placement="top" @submit.prevent="submit">
      <n-form-item :label="copy.importModal.label" required>
        <n-input v-model:value="form.label" autocomplete="off" :placeholder="copy.importModal.labelPlaceholder" />
      </n-form-item>
      <n-form-item :label="copy.importModal.token" required>
        <n-input
          v-model:value="form.token"
          type="textarea"
          :placeholder="copy.importModal.tokenPlaceholder"
          :autosize="{ minRows: 8, maxRows: 14 }"
        />
      </n-form-item>
      <div class="modal-actions">
        <n-button secondary @click="emit('update:show', false)">{{ copy.importModal.cancel }}</n-button>
        <n-button type="primary" :loading="loading" :disabled="!form.label.trim() || !form.token.trim()" @click="submit">
          {{ copy.importModal.submit }}
        </n-button>
      </div>
    </n-form>
  </n-modal>
</template>

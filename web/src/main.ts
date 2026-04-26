import { createApp } from "vue";
import {
  create,
  NAlert,
  NButton,
  NCard,
  NConfigProvider,
  NDrawer,
  NDrawerContent,
  NEmpty,
  NForm,
  NFormItem,
  NImage,
  NInput,
  NModal,
  NPopconfirm,
  NSelect,
  NSpin,
  NSwitch,
  NTable,
  NTag
} from "naive-ui";
import App from "./App.vue";
import "./styles.css";

const naive = create({
  components: [
    NAlert,
    NButton,
    NCard,
    NConfigProvider,
    NDrawer,
    NDrawerContent,
    NEmpty,
    NForm,
    NFormItem,
    NImage,
    NInput,
    NModal,
    NPopconfirm,
    NSelect,
    NSpin,
    NSwitch,
    NTable,
    NTag
  ]
});

createApp(App).use(naive).mount("#app");

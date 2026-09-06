import { createApp } from "vue";
import {
  createRouter,
  createWebHistory,
} from "vue-router";
import App from "./App.vue";
import routes from "./routes";
import "./style.css";
import "bootstrap/dist/css/bootstrap.min.css";
// Vuetify
import "vuetify/styles";
import { createVuetify } from "vuetify";
import { aliases, mdi } from "vuetify/iconsets/mdi";
import "@mdi/font/css/materialdesignicons.css";

const vuetify = createVuetify({
  icons: {
    defaultSet: "mdi",
    aliases,
    sets: { mdi },
  },
});

const getRouterBase = () => {
  const baseEl = document.querySelector("base");
  const href = baseEl ? baseEl.getAttribute("href") : "";
  if (href && href.trim() !== "") {
    return href;
  }
  const path = window?.location?.pathname || "";
  if (path.startsWith("/apps/slider")) {
    return "/apps/slider/";
  }
  return "/";
};

const router = createRouter({
  // 統一使用 history 模式，自動依據外層 Reverse Proxy (<base>) 調整基礎路徑。
  history: createWebHistory(getRouterBase()),
  routes,
});

createApp(App).use(router).use(vuetify).mount("#app");

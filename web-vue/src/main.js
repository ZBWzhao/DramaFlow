import { createApp } from "vue";
import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import * as ElIcons from "@element-plus/icons-vue";

import "element-plus/dist/index.css";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/controls/dist/style.css";
import "@vue-flow/minimap/dist/style.css";

import "./styles/theme.css";
// 画布视图专用暗色主题（作用域 .canvas-app，不影响首页/制片台）
import "./styles/canvas.css";
import "./styles/canvas2.css";
import App from "./App.vue";

const app = createApp(App);
app.use(ElementPlus, { locale: zhCn });
for (const [name, comp] of Object.entries(ElIcons)) app.component(name, comp);
app.mount("#app");

import { createApp } from "vue";
import "../styles/tokens.css";
import "./skeleton.css";
import SkeletonApp from "./SkeletonApp.vue";
import { router } from "./router";

createApp(SkeletonApp).use(router).mount("#sk-root");

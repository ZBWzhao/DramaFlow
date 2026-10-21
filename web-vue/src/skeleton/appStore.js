import { computed, reactive } from "vue";

/**
 * 骨架 Demo 的“全局状态”演示：模块级单例，挂在任何路由之外。
 * 真实接入时应换成 store.js 的 state；这里只是为了证明
 * 「路由切换 → 组件卸载重建 → 任务坞状态不丢」。
 */
const startedAt = Date.now();

export const dock = reactive({
  collapsed: true,
  /** 本 Demo 的显性指标：跨页面切换次数 */
  switched: 0,
  tasks: [
    {
      id: "t-agent",
      kind: "agent",
      title: "Agent 正在拆解《第 1 集 · 退婚》",
      step: "建生成槽位 · 连参考图",
      progress: 12,
      status: "running",
    },
    {
      id: "t-batch",
      kind: "batch",
      title: "批量生成 3 张分镜",
      step: "排队中 · 预计 40s",
      progress: 0,
      status: "queued",
    },
  ],
});

/** 已用时长（秒），由唯一一个模块级计时器驱动，路由切换不会重置 */
export const elapsed = reactive({ s: 0 });

setInterval(() => {
  elapsed.s = Math.floor((Date.now() - startedAt) / 1000);
  const agent = dock.tasks[0];
  if (agent.progress < 88) {
    agent.progress = Math.min(88, agent.progress + Math.random() * 1.6 + 0.4);
  }
}, 1000);

export const activeCount = computed(
  () => dock.tasks.filter((t) => t.status === "running" || t.status === "queued").length,
);

export function markSwitch() {
  dock.switched += 1;
}

export function toggleDock(value) {
  dock.collapsed = typeof value === "boolean" ? value : !dock.collapsed;
}

export function fmtElapsed(s) {
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m ? `${m}m${String(r).padStart(2, "0")}s` : `${s}s`;
}

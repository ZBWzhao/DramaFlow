<script setup>
/* 系统外壳：顶栏（品牌 / 主导航 / 工具区）+ 路由出口。
 *
 * 主导航按产品流程划分：剧本 → 制作 → 管线产物 → 画布 → 预览；
 * 工具区放跨流程的入口：项目资产 / 数据看板 / 项目信息 / 任务中心。
 * AI 助手是全局件：剧本页与画布页停靠（页面自挂 AiPanel mode="dock"），
 * 其余页面用右下浮球唤起全局抽屉（AiPanel mode="drawer"）。
 */
import { computed, nextTick, provide, ref } from "vue";
import { useRoute } from "vue-router";
import TaskCenter from "./TaskCenter.vue";
import NewProjectDialog from "./NewProjectDialog.vue";
import AiPanel from "../ai/AiPanel.vue";
import { applyField } from "../ai/diff.js";
import { getJSON } from "../api.js";
import { savePrompt, studio } from "../studio/studioStore.js";
import { shell } from "./shellStore.js";

const route = useRoute();

/* 项目态：/project/:name/* 下才显示项目导航与工具区 */
const inproj = computed(() => route.path.startsWith("/project/"));
/* 画布页是整屏工作台，自带完整顶栏（返回/项目/视图切换/模型/导出），
 * 外壳顶栏再叠一层只会双份导航——画布态直接隐藏。 */
const isCanvas = computed(() => route.path.endsWith("/canvas"));
const pname = computed(() => String(route.params.name || ""));
/* 顶栏展示项目标题；画布 key 只用于 URL 与 API */
const ptitle = computed(() => studio.project?.title || pname.value);
/* 画幅/画质来自项目 meta（新建时选的值），没加载到就不显示，不编默认值 */
const specText = computed(() => {
  const meta = studio.project?.project || {};
  if (!meta.aspect_ratio) return "";
  return `${meta.aspect_ratio} · ${String(meta.quality || "").toUpperCase()}`;
});

/* 项目信息弹窗：项目身份与规格一览 */
const infoOpen = ref(false);
const infoRows = computed(() => {
  const p = studio.project;
  const meta = p?.project || {};
  if (!p) return [];
  return [
    ["项目", ptitle.value],
    ["画布", pname.value],
    ["当前集", p.episode ? `第 ${p.episode} 集` : "—"],
    ["视觉风格", meta.style_preset || "—"],
    ["画幅 / 画质", specText.value || "—"],
    ["片段", `${(studio.segments || []).length} 段`],
  ];
});

/* 剧本/画布页自带停靠 AI，浮球只在这些页面之外出现 */
const fabVisible = computed(
  () => inproj.value && !route.path.endsWith("/script") && !route.path.endsWith("/canvas"),
);
const drawerOpen = ref(false);
const drawerPanel = ref(null);

/* 页面组件 inject("ai-open") 可唤起全局抽屉并直接发一句话（「让 AI 改」入口） */
provide("ai-open", (text) => {
  drawerOpen.value = true;
  if (text) nextTick(() => drawerPanel.value?.send(text));
});

/* 快捷指令按所在页注入 */
const chips = computed(() => {
  const p = route.path;
  if (p.endsWith("/production") || p.endsWith("/wall"))
    return ["这段提示词换个机位", "1-7 为什么过期", "第二集拆分镜"];
  if (p.endsWith("/assets")) return ["检查全片形象一致性", "给苏晚换个更冷的音色"];
  if (p.endsWith("/dashboard") || p.endsWith("/preview")) return ["总结本集进度", "下一段最该做什么"];
  return [];
});

/* 全局抽屉的 diff 应用：拉详情 → 字段写回 → 人改保存（转 Task 9 同一动作） */
async function applyDiff(d) {
  if (!d.segmentId) return false;
  studio.canvas = pname.value;
  const detail = await getJSON(
    `/api/studio/segments/${encodeURIComponent(d.segmentId)}?canvas=${encodeURIComponent(pname.value)}`,
  );
  await savePrompt(d.segmentId, applyField(detail.prompt || "", d.field, d.new));
  return true;
}

/* 主导航：制作 tab 覆盖 production 与 wall（首帧墙是制作的子页） */
const tabs = [
  { label: "剧本", to: (n) => `/project/${n}/script`, match: (p) => p.endsWith("/script") },
  { label: "制作", to: (n) => `/project/${n}/production`, match: (p) => p.endsWith("/production") || p.endsWith("/wall") },
  { label: "管线产物", to: (n) => `/project/${n}/pipeline`, match: (p) => p.endsWith("/pipeline") },
  { label: "画布", to: (n) => `/project/${n}/canvas`, match: (p) => p.endsWith("/canvas") },
  { label: "预览", to: (n) => `/project/${n}/preview`, match: (p) => p.endsWith("/preview") },
];
</script>

<template>
  <div class="df-shell">
    <header v-if="!isCanvas" class="topbar">
      <div class="brand">
        <span class="wd">DRAMA<i>FLOW</i></span>
        <router-link v-if="inproj" class="home-link" to="/">‹ 项目</router-link>
      </div>
      <span v-if="inproj" class="pname">{{ ptitle }}</span>
      <nav v-if="inproj" class="top-tabs">
        <router-link v-for="t in tabs" :key="t.label" :to="t.to(pname)" class="tab" :class="{ cur: t.match(route.path) }">{{ t.label }}</router-link>
      </nav>
      <div v-if="inproj" class="top-utils">
        <router-link class="util" :to="`/project/${pname}/assets`" :class="{ cur: route.path.endsWith('/assets') }">项目资产</router-link>
        <router-link class="util" :to="`/project/${pname}/dashboard`" :class="{ cur: route.path.endsWith('/dashboard') }">数据看板</router-link>
        <button class="util" type="button" @click="infoOpen = true">项目信息</button>
        <span class="sep"></span>
        <span v-if="specText" class="spec num">{{ specText }}</span>
        <TaskCenter />
      </div>
      <!-- NEW-PROJECT：首页态右上按钮 + 全局弹窗 -->
      <div v-if="!inproj" class="home-right">
        <button class="btn primary" type="button" @click="shell.newOpen = true">新建项目</button>
      </div>
    </header>

    <main class="df-main">
      <router-view />
    </main>

    <!-- 项目信息 -->
    <div v-if="infoOpen" class="mask" @click.self="infoOpen = false">
      <div class="box">
        <h3>项目信息</h3>
        <div v-for="([k, v]) in infoRows" :key="k" class="irow">
          <span class="k">{{ k }}</span>
          <span class="v" :class="{ num: k !== '项目' }">{{ v }}</span>
        </div>
        <div class="box-foot">
          <span class="tip">数据看板 / 资产库顶栏同源</span>
          <button class="btn sm" type="button" @click="infoOpen = false">关闭</button>
        </div>
      </div>
    </div>

    <!-- AI 全局抽屉 + 浮球（剧本/画布页停靠面板之外的全局入口） -->
    <NewProjectDialog v-if="shell.newOpen" @close="shell.newOpen = false" />
    <button v-if="fabVisible" class="ai-fab" type="button" title="AI 助手" @click="drawerOpen = true">AI</button>
    <Transition name="drawer">
      <AiPanel
        v-if="drawerOpen"
        ref="drawerPanel"
        class="ai-drawer"
        mode="drawer"
        :canvas="pname"
        :chips="chips"
        :apply-handler="applyDiff"
        @canvas-changed="() => {}"
      >
        <template #head-extra>
          <button class="cls" type="button" @click="drawerOpen = false">收起 ›</button>
        </template>
      </AiPanel>
    </Transition>
  </div>
</template>

<style scoped>
/* 作用域重置：新壳内的元素回到 demo 的零间距基线（旧视图不受影响） */
.df-shell,
.df-shell *,
.df-shell *::before,
.df-shell *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

.df-shell { display: flex; flex-direction: column; height: 100vh; }
.df-main { flex: 1; overflow: hidden; position: relative; display: flex; flex-direction: column; }
.df-main > :deep(*) { flex: 1; min-height: 0; }

/* ---------- 顶栏（demo v4 · .topbar） ---------- */
.topbar {
  display: flex; align-items: stretch; height: var(--h-topbar);
  padding: 0 var(--sp-5); background: var(--bg-2);
  border-bottom: 1px solid var(--line); flex: none; position: relative; z-index: 20;
}
.brand { display: flex; align-items: center; gap: 10px; padding-right: var(--sp-5); border-right: 1px solid var(--line); }
.brand .wd { font-weight: 600; font-size: var(--fs-13); letter-spacing: .18em; }
.brand .wd i { font-style: normal; color: var(--accent); }
.home-link { color: var(--text-3); font-size: var(--fs-12); text-decoration: none; }
.home-link:hover { color: var(--text); }
.pname {
  display: flex; align-items: center; padding: 0 var(--sp-4);
  color: var(--text-2); max-width: 300px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.top-tabs { display: flex; align-items: stretch; margin: 0 auto; }
.tab {
  position: relative; display: flex; align-items: center; padding: 0 26px;
  color: var(--text-3); font-size: var(--fs-14);
  font-family: var(--serif); letter-spacing: .06em; text-decoration: none;
}
.tab:hover { color: var(--text-2); }
.tab.cur { color: var(--text); }
.tab.cur::after { content: ""; position: absolute; left: 26px; right: 26px; bottom: 0; height: 2px; background: var(--accent); }
.top-utils { display: flex; align-items: center; gap: var(--sp-4); }
.util { color: var(--text-3); font-size: var(--fs-13); padding: 5px 3px; text-decoration: none; background: none; border: none; font-family: inherit; cursor: pointer; }
.util:hover { color: var(--text); }
.util.cur { color: var(--text); }
.sep { width: 1px; height: 16px; background: var(--line-2); }
.spec { font-family: var(--mono); font-size: var(--fs-11); color: var(--text-3); }
.home-right { margin-left: auto; display: flex; align-items: center; }

/* ---------- 项目信息弹窗 ---------- */
.mask { position: fixed; inset: 0; background: rgba(5, 4, 3, .7); z-index: 80; display: grid; place-items: center; }
.box { background: var(--panel); border: 1px solid var(--line-2); border-radius: var(--r-3); width: 420px; max-width: 92vw; padding: var(--sp-5); }
.box h3 { font-family: var(--serif); font-size: var(--fs-16); margin-bottom: var(--sp-3); }
.irow { display: flex; justify-content: space-between; gap: var(--sp-4); padding: 9px 0; border-bottom: 1px solid var(--line); font-size: var(--fs-13); }
.irow .k { color: var(--text-3); flex: none; }
.irow .v { color: var(--text-2); text-align: right; word-break: break-all; }
.box-foot { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-4); }
.box-foot .tip { margin-right: auto; color: var(--text-3); font-size: var(--fs-11); }

/* ---------- 全局 AI 抽屉（demo v4 · .ai-fab/.ai-drawer） ---------- */
.ai-fab {
  position: fixed; right: 22px; bottom: 74px; z-index: 70;
  width: 44px; height: 44px; border-radius: 50%;
  background: var(--panel); border: 1px solid var(--line-2);
  color: var(--gold); font-family: var(--serif); font-size: var(--fs-14);
  box-shadow: 0 8px 30px rgba(0, 0, 0, .5);
}
.ai-fab:hover { border-color: rgba(194, 163, 107, .5); }
.ai-drawer {
  position: fixed; top: 0; right: 0; bottom: 0; width: 380px; z-index: 65;
  border-left: 1px solid var(--line-2); box-shadow: -16px 0 48px rgba(0, 0, 0, .45);
}
.drawer-enter-active, .drawer-leave-active { transition: transform 240ms var(--ease); }
.drawer-enter-from, .drawer-leave-to { transform: translateX(100%); }
.cls { color: var(--text-3); font-size: var(--fs-12); }
.cls:hover { color: var(--text); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); cursor: pointer; background: none; border: none; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
</style>

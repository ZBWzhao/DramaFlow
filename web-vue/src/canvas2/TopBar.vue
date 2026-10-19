<script setup>
/* 画布顶栏：项目上下文 + 保存状态 + 视图切换 + 全局模型 + 主操作。
 *
 * 纯展示组件：状态全部由 props 传入，动作只 emit，不直接读写 store。
 * 顶栏要在一行里回答四件事：我在哪、改的存了没、用哪个模型、下一步做什么。
 */
import { computed, ref, onBeforeUnmount, onMounted } from "vue";

const props = defineProps({
  title: { type: String, default: "" },
  saveState: { type: String, default: "saved" }, // saved | syncing | conflict | error
  savedAt: { type: Number, default: 0 },
  view: { type: String, default: "canvas" }, // canvas | studio | assets
  selectedCount: { type: Number, default: 0 },
  providers: { type: Array, default: () => [] },
  provider: { type: String, default: "" },
  model: { type: String, default: "" },
  problems: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
});

const emit = defineEmits([
  "back",
  "open-project",
  "switch-view",
  "change-model",
  "generate-selected",
  "export",
  "open-info",
]);

const VIEWS = [
  { id: "canvas", label: "画布" },
  { id: "studio", label: "制片台" },
  { id: "assets", label: "素材库" },
];

const root = ref(null);
const modelOpen = ref(false);
const problemsOpen = ref(false);

const activeProvider = computed(
  () => props.providers.find((item) => item.id === props.provider) || null,
);
const models = computed(() => activeProvider.value?.models || []);
const modelLabel = computed(() => {
  const found = models.value.find((item) => item.id === props.model);
  const name = found?.label || props.model || "默认模型";
  return name.length > 22 ? `${name.slice(0, 21)}…` : name;
});
const providerLabel = computed(() => activeProvider.value?.label || props.provider || "—");

const saveText = computed(() => {
  if (props.saveState === "syncing") return "同步中";
  if (props.saveState === "conflict") return "画布已更新";
  if (props.saveState === "error") return "保存失败";
  if (!props.savedAt) return "已保存";
  const date = new Date(props.savedAt);
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `已保存 ${hh}:${mm}`;
});

function pickProvider(item) {
  if (!item.configured) return;
  const next = item.models?.find((model) => model.kind === "video") || item.models?.[0];
  emit("change-model", { provider: item.id, model: next?.id || "" });
}
function pickModel(item) {
  emit("change-model", { provider: props.provider, model: item.id });
  modelOpen.value = false;
}
function toggleModel() {
  modelOpen.value = !modelOpen.value;
  problemsOpen.value = false;
}
function toggleProblems() {
  problemsOpen.value = !problemsOpen.value;
  modelOpen.value = false;
}

function onDocPointer(event) {
  if (!root.value?.contains(event.target)) {
    modelOpen.value = false;
    problemsOpen.value = false;
  }
}
function onDocKey(event) {
  if (event.key === "Escape") {
    modelOpen.value = false;
    problemsOpen.value = false;
  }
}
onMounted(() => {
  document.addEventListener("pointerdown", onDocPointer);
  document.addEventListener("keydown", onDocKey);
});
onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", onDocPointer);
  document.removeEventListener("keydown", onDocKey);
});
</script>

<template>
  <header ref="root" class="cv2-topbar">
    <div class="tb-left">
      <button class="tb-icon" type="button" title="返回项目列表" aria-label="返回项目列表" @click="emit('back')">←</button>
      <button class="tb-project" type="button" title="打开项目管理" @click="emit('open-project')">
        <span class="tb-project-name">{{ title || "未命名项目" }}</span>
        <span class="tb-caret" aria-hidden="true">▾</span>
      </button>
      <span class="tb-save" :class="saveState" :title="saveText">
        <i class="tb-save-dot" aria-hidden="true"></i>{{ saveText }}
      </span>
    </div>

    <nav class="tb-views" role="tablist" aria-label="视图切换">
      <button
        v-for="item in VIEWS"
        :key="item.id"
        type="button"
        role="tab"
        :aria-selected="view === item.id"
        :class="{ active: view === item.id }"
        @click="emit('switch-view', item.id)"
      >
        {{ item.label }}
      </button>
    </nav>

    <div class="tb-right">
      <div class="tb-pop">
        <button class="tb-model" type="button" :aria-expanded="modelOpen" @click="toggleModel">
          <span class="tb-model-provider">{{ providerLabel }}</span>
          <span class="tb-model-name tabular">{{ modelLabel }}</span>
          <span class="tb-caret" aria-hidden="true">▾</span>
        </button>
        <div v-if="modelOpen" class="tb-menu model-menu" role="menu">
          <p class="tb-menu-title">Provider</p>
          <button
            v-for="item in providers"
            :key="item.id"
            type="button"
            role="menuitem"
            class="tb-menu-row"
            :class="{ current: item.id === provider, off: !item.configured }"
            :disabled="!item.configured"
            @click="pickProvider(item)"
          >
            <span>{{ item.label || item.id }}</span>
            <span class="tb-menu-hint">{{ item.configured ? (item.mock ? "占位" : "可用") : "未配置" }}</span>
          </button>
          <template v-if="models.length">
            <p class="tb-menu-title">Model</p>
            <button
              v-for="item in models"
              :key="item.id"
              type="button"
              role="menuitem"
              class="tb-menu-row"
              :class="{ current: item.id === model }"
              @click="pickModel(item)"
            >
              <span>{{ item.label || item.id }}</span>
              <span class="tb-menu-hint">{{ item.kind || "" }}</span>
            </button>
          </template>
        </div>
      </div>

      <div class="tb-pop">
        <button
          v-if="problems.length"
          class="tb-problems"
          type="button"
          :aria-expanded="problemsOpen"
          title="打开问题清单"
          @click="toggleProblems"
        >
          {{ problems.length }} 待处理
        </button>
        <div v-if="problemsOpen" class="tb-menu problems-menu" role="dialog" aria-label="问题清单">
          <p class="tb-menu-title">画布待处理</p>
          <p v-for="(item, index) in problems" :key="index" class="tb-problem-row">{{ item }}</p>
        </div>
      </div>

      <button class="tb-btn" type="button" title="导出全片提示词" @click="emit('export')">导出</button>
      <button
        class="tb-btn primary"
        type="button"
        :disabled="!selectedCount || busy"
        title="生成选中的节点"
        @click="emit('generate-selected')"
      >
        生成选中<span v-if="selectedCount" class="tabular">&nbsp;{{ selectedCount }}</span>
      </button>
      <button class="tb-icon" type="button" title="会话信息 / 技能 / 工具" aria-label="会话信息" @click="emit('open-info')">i</button>
    </div>
  </header>
</template>

<style scoped>
.cv2-topbar {
  grid-area: top;
  position: relative;
  z-index: var(--z-chrome);
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  height: var(--h-topbar);
  padding: 0 var(--sp-4);
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--panel) 92%, transparent);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  box-shadow: var(--hairline-top);
  color: var(--text-2);
  font-size: var(--fs-12);
}

.tb-left {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}

.tb-icon {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border: 1px solid transparent;
  border-radius: var(--r-btn);
  background: transparent;
  color: var(--text-2);
  font-size: var(--fs-13);
  line-height: 1;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.tb-icon:hover {
  background: var(--card-2);
  color: var(--text);
}

.tb-project {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  max-width: 280px;
  padding: 4px var(--sp-2);
  border: 1px solid transparent;
  border-radius: var(--r-btn);
  background: transparent;
  color: var(--text);
  font-size: var(--fs-13);
  font-weight: 500;
}
.tb-project:hover {
  background: var(--card-2);
  border-color: var(--line);
}
.tb-project-name {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.tb-caret {
  color: var(--text-3);
  font-size: 9px;
}

.tb-save {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text-3-strong);
  font-size: var(--fs-11);
  white-space: nowrap;
}
.tb-save-dot {
  width: 5px;
  height: 5px;
  border-radius: var(--r-pill);
  background: currentColor;
}
.tb-save.syncing {
  color: var(--accent);
}
.tb-save.conflict,
.tb-save.error {
  color: var(--warn);
}

.tb-views {
  display: flex;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--line);
  border-radius: var(--r-btn);
  background: var(--bg-2);
}
.tb-views button {
  padding: 3px 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-3-strong);
  font-size: var(--fs-11);
  line-height: 18px;
}
.tb-views button:hover {
  color: var(--text);
}
.tb-views button.active {
  background: var(--card-2);
  color: var(--text);
  box-shadow: 0 0 0 1px var(--line-2) inset;
}

.tb-right {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-left: auto;
}

.tb-pop {
  position: relative;
}

.tb-model,
.tb-problems {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  height: 30px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: var(--r-btn);
  background: var(--card);
  color: var(--text-2);
  font-size: var(--fs-11);
  white-space: nowrap;
}
.tb-model:hover,
.tb-problems:hover {
  border-color: var(--line-2);
  color: var(--text);
}
.tb-model-provider {
  color: var(--text-3);
}
.tb-model-name {
  color: var(--text);
}
.tb-problems {
  border-color: color-mix(in srgb, var(--warn) 34%, var(--line));
  color: var(--warn);
}

.tb-menu {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: var(--z-popover);
  min-width: 220px;
  max-width: 340px;
  padding: var(--sp-2);
  border: 1px solid var(--line-2);
  border-radius: var(--r-card);
  background: var(--card);
  box-shadow: var(--elev-3);
}
.tb-menu-title {
  margin: var(--sp-1) var(--sp-2) 2px;
  color: var(--text-3);
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.tb-menu-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  width: 100%;
  padding: 5px var(--sp-2);
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-2);
  font-size: var(--fs-12);
  text-align: left;
}
.tb-menu-row:hover {
  background: var(--card-2);
  color: var(--text);
}
.tb-menu-row.current {
  background: var(--accent-soft);
  color: var(--text);
}
.tb-menu-row.off {
  color: var(--text-3);
  cursor: not-allowed;
}
.tb-menu-hint {
  color: var(--text-3);
  font-size: 10px;
}
.problems-menu {
  min-width: 320px;
  max-width: 420px;
}
.tb-problem-row {
  margin: 0;
  padding: 5px var(--sp-2);
  border-top: 1px solid var(--line);
  color: var(--text-2);
  font-size: var(--fs-11);
  line-height: 1.45;
}

.tb-btn {
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--line-2);
  border-radius: var(--r-btn);
  background: var(--card);
  color: var(--text);
  font-size: var(--fs-12);
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.tb-btn:hover {
  border-color: var(--line-3);
  background: var(--card-2);
}
.tb-btn.primary {
  border-color: var(--accent-line);
  background: var(--accent);
  color: var(--text-on-accent);
  font-weight: 500;
}
.tb-btn.primary:hover {
  background: color-mix(in srgb, var(--accent) 88%, #ffffff);
}
.tb-btn:disabled {
  border-color: var(--line);
  background: var(--card);
  color: var(--text-3);
  cursor: not-allowed;
}
</style>

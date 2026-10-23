<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useRouter } from "vue-router";
import { actions, state } from "../store.js";

const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(["close"]);

const sortMode = ref(localStorage.getItem("toonflow-project-sort") || "recent");
const order = ref(readOrder());
const recent = ref(readRecent());

function readOrder() {
  try { return JSON.parse(localStorage.getItem("toonflow-project-order") || "[]"); } catch { return []; }
}
function readRecent() {
  try { return JSON.parse(localStorage.getItem("toonflow-project-recent") || "{}"); } catch { return {}; }
}
function saveOrder() { localStorage.setItem("toonflow-project-order", JSON.stringify(order.value)); }
function saveRecent() { localStorage.setItem("toonflow-project-recent", JSON.stringify(recent.value)); }
function titleOf(name) { return state.canvasMeta[name]?.story_title || name; }
function summaryOf(name) { return state.canvasMeta[name]?.story_summary || "暂无简介"; }

function normalizedOrder() {
  const available = new Set(state.canvases);
  const next = order.value.filter((name) => available.has(name));
  for (const name of state.canvases) if (!next.includes(name)) next.push(name);
  if (next.join("\0") !== order.value.join("\0")) {
    order.value = next;
    saveOrder();
  }
  return next;
}

const projects = computed(() => {
  const names = normalizedOrder().map((name) => ({ name, title: titleOf(name), summary: summaryOf(name) }));
  if (sortMode.value === "name") return [...names].sort((a, b) => a.title.localeCompare(b.title, "zh-CN"));
  if (sortMode.value === "recent") {
    return [...names].sort((a, b) => (recent.value[b.name] || 0) - (recent.value[a.name] || 0));
  }
  return names;
});

watch(sortMode, (value) => localStorage.setItem("toonflow-project-sort", value));
watch(() => state.canvases.join("\0"), normalizedOrder);

function markRecent(name) {
  recent.value = { ...recent.value, [name]: Date.now() };
  saveRecent();
}

const router = useRouter();

async function openProject(name) {
  if (name === state.canvasName) { emit("close"); return; }
  try {
    await actions.switchCanvas(name);
    // URL 同步（replace：切项目不算一次新导航，返回键不该落在这里）
    router.replace(`/project/${name}/canvas`);
    markRecent(name);
    emit("close");
  } catch (err) {
    ElMessage.error(`切换项目失败：${err.message || err}`);
  }
}

function moveProject(name, delta) {
  sortMode.value = "manual";
  const next = normalizedOrder();
  const index = next.indexOf(name);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= next.length) return;
  [next[index], next[target]] = [next[target], next[index]];
  order.value = next;
  saveOrder();
}

async function renameProject(project) {
  try {
    const { value } = await ElMessageBox.prompt("输入项目展示名称", "重命名项目", {
      inputValue: project.title,
      inputPattern: /\S+/,
      inputErrorMessage: "名称不能为空",
      confirmButtonText: "保存",
      cancelButtonText: "取消",
    });
    await actions.renameCanvas(project.name, value);
    ElMessage.success("项目已重命名");
  } catch (err) {
    if (err !== "cancel" && err !== "close") ElMessage.error(`重命名失败：${err.message || err}`);
  }
}

async function deleteProject(project) {
  if (project.name === "main") {
    ElMessage.warning("默认项目不能删除");
    return;
  }
  try {
    await ElMessageBox.confirm(
      `将删除项目“${project.title}”的画布节点和连线。关联会话会保留在历史存储中，但不再出现在项目列表。确定继续吗？`,
      "删除项目",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消", confirmButtonClass: "el-button--danger" },
    );
    await actions.deleteCanvas(project.name);
    ElMessage.success("项目已删除");
  } catch (err) {
    if (err !== "cancel" && err !== "close") ElMessage.error(`删除失败：${err.message || err}`);
  }
}
</script>

<template>
  <div v-if="props.open" class="project-panel-layer" @click.self="emit('close')">
    <aside class="project-panel" aria-label="项目管理">
      <header class="project-panel-head">
        <div>
          <h2>项目</h2>
          <p>每个项目对应一张独立的短剧生产画布</p>
        </div>
        <el-button circle text title="关闭项目面板" aria-label="关闭项目面板" @click="emit('close')">
          <el-icon><Close /></el-icon>
        </el-button>
      </header>

      <div class="project-panel-toolbar">
        <span class="dim">{{ projects.length }} 个项目</span>
        <el-select v-model="sortMode" size="small" aria-label="项目排序">
          <el-option label="最近使用" value="recent" />
          <el-option label="名称排序" value="name" />
          <el-option label="手动排序" value="manual" />
        </el-select>
      </div>

      <div class="project-list">
        <div
          v-for="(project, index) in projects"
          :key="project.name"
          class="project-item"
          :class="{ active: project.name === state.canvasName }"
          @click="openProject(project.name)"
        >
          <div class="project-item-main">
            <span class="project-dot" />
            <div class="project-item-copy">
              <strong>{{ project.title }}</strong>
              <span>{{ project.name }} · {{ project.summary }}</span>
            </div>
          </div>
          <div class="project-item-actions" @click.stop>
            <el-button text circle size="small" title="上移" :disabled="sortMode !== 'manual' || index === 0" @click="moveProject(project.name, -1)">
              <el-icon><ArrowUp /></el-icon>
            </el-button>
            <el-button text circle size="small" title="下移" :disabled="sortMode !== 'manual' || index === projects.length - 1" @click="moveProject(project.name, 1)">
              <el-icon><ArrowDown /></el-icon>
            </el-button>
            <el-button text circle size="small" title="重命名项目" @click="renameProject(project)">
              <el-icon><Edit /></el-icon>
            </el-button>
            <el-button text circle size="small" type="danger" title="删除项目" @click="deleteProject(project)">
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
      </div>

      <footer class="project-panel-foot">项目名称修改只影响展示名，内部画布 ID 保持不变。</footer>
    </aside>
  </div>
</template>

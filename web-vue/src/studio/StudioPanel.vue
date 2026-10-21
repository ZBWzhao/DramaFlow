<template>
  <div class="studio">
    <!-- ───────────────────────────── 顶栏 -->
    <div class="studio-top">
      <button class="studio-back" @click="$emit('close')">← 项目列表</button>
      <div class="studio-title">
        <strong>{{ studio.project?.title || studio.canvas || "制片台" }}</strong>
        <span>
          {{ studio.project?.project?.region || "—" }} · {{ studio.project?.project?.genre || "—" }}
          · {{ studio.project?.project?.aspect_ratio || "9:16" }}
        </span>
      </div>

      <div class="studio-tabs">
        <button v-for="t in TABS" :key="t.key" :class="{ on: studio.tab === t.key }" @click="switchTab(t.key)">
          {{ t.label }}
        </button>
      </div>

      <div class="studio-top-right">
        <span class="studio-chip">
          片段 <b>{{ studio.project?.counts?.segments?.done || 0 }}/{{ studio.project?.counts?.segments?.total || 0 }}</b>
        </span>
        <span class="studio-chip">
          资产 <b>{{ assetReady }}/{{ assetTotal }}</b>
        </span>
        <span class="studio-chip mock" :class="{ ok: !isMock }" style="cursor: pointer" title="点一下切换 Mock / 真模型" @click="openProviders">
          {{ isMock ? "Mock 出片" : activeProviderLabel }}
        </span>
        <button class="btn ghost sm" title="查看 Agent 编排的结构视图" @click="$emit('canvas')">结构视图</button>
        <button class="btn ghost sm" :disabled="studio.loading" @click="reloadAll">刷新</button>
      </div>
    </div>

    <!-- 媒体 Provider 选择 -->
    <div v-if="providerOpen" class="provider-pop" @click.self="providerOpen = false">
      <div class="provider-box">
        <div class="studio-section-title"><span>媒体 Provider</span><span class="sub">Mock 不花钱；真模型耗余额</span></div>
        <div v-for="p in providers" :key="p.id" class="provider-row" :class="{ on: p.active }">
          <div style="flex: 1; min-width: 0">
            <div style="display: flex; gap: 6px; align-items: center">
              <b>{{ p.label }}</b>
              <span v-if="p.mock" class="tag info" style="font-size: 9px">不产生费用</span>
              <span v-else-if="!p.configured" class="tag warn" style="font-size: 9px">未配置 Key</span>
              <span v-if="p.active" class="tag ok" style="font-size: 9px">当前</span>
            </div>
            <div style="font-size: 11px; color: var(--text-3); margin-top: 2px">
              {{ p.configured ? (p.models || []).length + " 个模型" : "设置 TF_ROUTER_API_KEY 后用 --env-file 启动即可解锁" }}
            </div>
          </div>
          <button class="btn sm" :disabled="!p.configured || p.active" @click="pickProvider(p)">切换</button>
        </div>
        <div v-if="activeModels.length" style="margin-top: 10px">
          <div class="studio-section-title" style="margin-bottom: 6px"><span>出片模型</span></div>
          <div style="display: flex; flex-wrap: wrap; gap: 6px">
            <button
              v-for="m in activeModels"
              :key="m.id"
              class="btn sm"
              :class="{ primary: m.id === currentModel }"
              @click="pickModel(m.id)"
            >
              {{ m.label }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ───────────────────────────── 主体 -->
    <div class="studio-body">
      <!-- 左栏 -->
      <div class="studio-side">
        <div>
          <div class="studio-section-title"><span>制作流程</span></div>
          <div class="pipe">
            <div
              v-for="step in studio.project?.pipeline || []"
              :key="step.key"
              class="pipe-step"
              :class="step.state"
            >
              <span class="dot">{{ step.state === "done" ? "✓" : "" }}</span>
              <span>{{ step.label }}</span>
            </div>
          </div>
        </div>

        <div>
          <div class="studio-section-title">
            <span>分集</span>
            <span style="text-transform: none; letter-spacing: 0">{{ episodes.length }}</span>
          </div>
          <div class="ep-list">
            <button
              v-for="ep in episodes"
              :key="ep.no"
              class="ep-card"
              :class="{ on: ep.no === studio.project?.episode }"
              @click="pickEpisode(ep.no)"
            >
              <span class="ep-no">EP {{ String(ep.no).padStart(2, "0") }}</span>
              <span class="ep-name">{{ ep.title || "未命名" }}</span>
              <span class="ep-meta">
                <span>{{ ep.segment_done }}/{{ ep.segment_total }} 段</span>
                <span>{{ ep.status === "done" ? "已完成" : ep.segment_done ? "进行中" : "待开始" }}</span>
              </span>
            </button>
            <div v-if="!episodes.length" class="empty" style="padding: 16px">还没有分集</div>
          </div>
        </div>

        <div style="margin-top: auto; font-size: 11px; line-height: 1.7; color: var(--text-3)">
          制片台管的是「这一集能不能用」，<br />画布管的是「Agent 怎么编排」。
        </div>
      </div>

      <!-- 右主体 -->
      <div class="studio-main">
        <div v-if="studio.error" class="empty" style="color: var(--bad)">
          {{ studio.error }}
          <button class="btn sm" @click="reloadAll">重试</button>
        </div>

        <ScriptView v-else-if="studio.tab === 'script'" />
        <AssetsView v-else-if="studio.tab === 'assets'" />
        <ProductionView v-else-if="studio.tab === 'production'" />
        <PreviewView v-else-if="studio.tab === 'preview'" />
      </div>
    </div>

    <div v-if="studio.notice" class="studio-toast">{{ studio.notice }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { studio, loadProject, loadAssets, loadSegments, loadScript, loadPreview, loadDetail, saveProject, toast } from "./studioStore.js";
import ScriptView from "./ScriptView.vue";
import AssetsView from "./AssetsView.vue";
import ProductionView from "./ProductionView.vue";
import PreviewView from "./PreviewView.vue";

defineEmits(["close", "canvas"]);

const TABS = [
  { key: "script", label: "剧本" },
  { key: "assets", label: "资产" },
  { key: "production", label: "制作" },
  { key: "preview", label: "预览" },
];

const episodes = computed(() => studio.project?.episodes || []);
const isMock = computed(() => String(studio.project?.project?.video_model || "").toLowerCase().startsWith("mock"));

const providerOpen = ref(false);
const providers = ref([]);
const activeModels = computed(() => {
  const current = providers.value.find((p) => p.active);
  return (current?.models || []).filter((m) => m.kind === "video");
});
const activeProviderLabel = computed(() => providers.value.find((p) => p.active)?.label || "真模型");
const currentModel = computed(() => studio.project?.project?.video_model || "");

async function openProviders() {
  providerOpen.value = true;
  try {
    const res = await fetch("/api/media/providers");
    const data = await res.json();
    providers.value = data.providers || [];
  } catch (err) {
    toast("读取媒体 Provider 失败", 3200);
  }
}

async function pickProvider(p) {
  try {
    const res = await fetch("/api/media/providers/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: p.id }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "切换失败");
    providers.value = data.providers || providers.value;
    // 换 Provider 后模型 id 也得换，否则会用上一个 Provider 的模型名去请求
    const videoModels = (data.providers || []).find((x) => x.id === p.id)?.models?.filter((m) => m.kind === "video") || [];
    if (videoModels.length) await saveProject({ video_model: videoModels[0].id });
    await loadProject();
    toast(`已切换到 ${p.label}`);
  } catch (err) {
    toast(err?.message || "切换失败", 3600);
  }
}

async function pickModel(modelId) {
  try {
    await saveProject({ video_model: modelId });
    await loadProject();
    toast(`出片模型：${modelId}`);
  } catch (err) {
    toast(err?.message || "保存失败", 3200);
  }
}
const assetTotal = computed(() => {
  const a = studio.project?.counts?.assets || {};
  return ["character", "scene", "prop"].reduce((n, k) => n + (a[k]?.total || 0), 0);
});
const assetReady = computed(() => {
  const a = studio.project?.counts?.assets || {};
  return ["character", "scene", "prop"].reduce((n, k) => n + (a[k]?.ready || 0), 0);
});

async function reloadAll() {
  studio.error = "";
  await loadProject();
  await Promise.all([loadAssets(), loadScript(), loadSegments(), loadPreview()]);
  if (studio.activeSegmentId) await loadDetail(studio.activeSegmentId);
}

async function switchTab(key) {
  studio.tab = key;
  if (key === "script" && !studio.script) await loadScript();
  if (key === "assets" && !studio.assets.character.length && !studio.assets.scene.length) await loadAssets();
  if (key === "production" && !studio.segments.length) await loadSegments();
  if (key === "preview" && !studio.preview) await loadPreview();
}

async function pickEpisode(no) {
  if (studio.project?.episode === no) return;
  try {
    await saveProject({ episode: no });
  } catch {
    /* 后端不支持切集时不影响查看 */
  }
  await reloadAll();
}

watch(
  () => studio.canvas,
  async (val) => {
    if (!val) return;
    studio.activeSegmentId = "";
    studio.detail = null;
    studio.script = null;
    studio.preview = null;
    await reloadAll();
  }
);

onMounted(async () => {
  if (studio.canvas) await reloadAll();
});
</script>

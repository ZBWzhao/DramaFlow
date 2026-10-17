<template>
  <div class="script-pane">
    <!-- ───────────────────────────── 左：剧本正文 -->
    <div class="panel" style="min-height: 0">
      <div class="panel-head">
        剧本正文
        <span class="sub">{{ script?.chars || 0 }} 字 · {{ script?.episodes?.length || 0 }} 集</span>
        <span class="grow"></span>
        <button class="btn primary sm" @click="importOpen = true">粘贴剧本</button>
        <button class="btn sm" :disabled="busy || producing || !script?.script" @click="produceOpen = true">
          {{ producing ? "Agent 制作中…" : "开始制作" }}
        </button>
        <button class="btn sm" :disabled="busy" @click="reload">重新读取</button>
      </div>
      <div class="panel-body">
        <div v-if="!script?.script" class="empty">
          还没有剧本<br />
          点右上角「粘贴剧本」把小说或分集剧本贴进来，会按「第 N 集」自动分集
        </div>
        <div v-else class="script-text">{{ script.script }}</div>
        <div v-if="script?.script && !segTotal" class="produce-hint">
          <span>
            还没有拆分镜。点「开始制作」，Agent 会把这一集拆成 8-15 秒的片段、提取角色 / 场景 / 道具、写好视频提示词
            —— <b>不生成任何画面，不花钱</b>。
          </span>
          <button class="btn primary sm" :disabled="producing" @click="produceOpen = true">开始制作</button>
        </div>
      </div>
    </div>

    <!-- ───────────────────────────── 右：项目与台词 -->
    <div class="script-side">
      <!-- 项目信息 -->
      <div class="panel" style="flex: none">
        <div class="panel-head">
          项目信息
          <span class="grow"></span>
          <button class="btn sm" :disabled="busy || !dirty" @click="save">保存</button>
        </div>
        <div class="panel-body">
          <div class="field">
            <label>标题</label>
            <div class="val">{{ studio.project?.title || "—" }}</div>
          </div>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px">
            <div class="field">
              <label>画面比例</label>
              <select v-model="form.aspect_ratio" class="mono" style="width: 100%; padding: 6px; background: var(--bg-overlay); color: var(--text); border: 1px solid var(--line); border-radius: var(--ui-radius-small)">
                <option value="9:16">9:16 竖屏</option>
                <option value="16:9">16:9 横屏</option>
                <option value="1:1">1:1 方形</option>
              </select>
            </div>
            <div class="field">
              <label>画质</label>
              <select v-model="form.quality" class="mono" style="width: 100%; padding: 6px; background: var(--bg-overlay); color: var(--text); border: 1px solid var(--line); border-radius: var(--ui-radius-small)">
                <option value="480P">480P</option>
                <option value="720P">720P</option>
                <option value="1080P">1080P</option>
              </select>
            </div>
            <div class="field">
              <label>地区</label>
              <input v-model="form.region" />
            </div>
            <div class="field">
              <label>题材</label>
              <input v-model="form.genre" />
            </div>
            <div class="field" style="grid-column: 1 / -1">
              <label>全剧画风（会拼进每一次生图提示词）</label>
              <input v-model="form.style_preset" placeholder="如：真人写实电影风格，浅景深，自然光" />
            </div>
            <div class="field" style="grid-column: 1 / -1">
              <label>视频模型</label>
              <input v-model="form.video_model" class="mono" />
            </div>
          </div>
        </div>
      </div>

      <!-- 台词清单 -->
      <div class="panel" style="min-height: 0">
        <div class="panel-head">
          台词清单
          <span class="sub">{{ script?.dialogue_count || 0 }} 句</span>
        </div>
        <div class="panel-body" style="padding: 8px 12px">
          <div v-for="(l, i) in script?.dialogue || []" :key="i" class="line-row">
            <span class="who">{{ l.speaker }}</span>
            <span class="say">{{ l.line }}</span>
            <span class="seg">E{{ l.episode_no }}-{{ String(l.segment_no).padStart(2, "0") }}</span>
          </div>
          <div v-if="!(script?.dialogue || []).length" class="empty" style="padding: 14px">
            没有识别到台词<br />台词写在分镜的 sound 里，用名字：「……」的格式
          </div>
        </div>
      </div>

      <!-- 资产概览 -->
      <div class="panel" style="flex: none">
        <div class="panel-head">
          资产
          <span class="sub">
            角色 {{ script?.assets?.character || 0 }} · 场景 {{ script?.assets?.scene || 0 }} · 道具
            {{ script?.assets?.prop || 0 }}
          </span>
        </div>
        <div class="panel-body" style="padding: 8px 12px">
          <div v-for="a in script?.asset_items || []" :key="a.node_id" class="asset-mini">
            <span class="tag" :class="a.ready ? 'ok' : ''" style="font-size: 10px">{{ a.ready ? "有图" : "缺图" }}</span>
            <span class="nm" :title="a.name">{{ cleanName(a.name) }}</span>
            <span style="font-size: 10px; color: var(--text-3)">{{ kindText(a.kind) }}</span>
            <button class="btn ghost sm" @click="goAsset(a)">处理</button>
          </div>
          <div v-if="!(script?.asset_items || []).length" class="empty" style="padding: 14px">还没有资产</div>
        </div>
      </div>
    </div>

    <!-- ───────────────────────────── 开始制作：确认 + 进度 -->
    <div v-if="produceOpen || producing" class="import-mask" @click.self="produceOpen = false">
      <div class="import-box" style="max-width: 620px">
        <div class="panel-head">
          {{ producing ? "Agent 正在制作第 " + episodeNo + " 集" : "让 Agent 开始制作" }}
          <span class="grow"></span>
          <button v-if="!producing" class="btn ghost sm" @click="produceOpen = false">取消</button>
        </div>
        <div class="panel-body" style="padding: 16px 18px">
          <template v-if="producing">
            <div class="progress-line" style="margin-bottom: 12px">
              <div class="bar"><i :style="{ width: producePercent + '%' }"></i></div>
            </div>
            <div class="sub" style="line-height: 1.9">
              {{ produceStage }}<br />
              状态 <b>{{ studio.produceJob?.status || "queued" }}</b> · 已用
              {{ studio.produceJob?.elapsed || 0 }}s
              <br />
              拆分镜是最慢的一步（要写完每段的分镜表和提示词），通常 1-3 分钟。
            </div>
          </template>
          <template v-else>
            <div class="sub" style="line-height: 2">
              针对 <b>第 {{ episodeNo }} 集</b> 的剧本，Agent 会：
              <br />① 按「一条分镜 = 8-15 秒片段、内含 2-4 个子镜头」把这一集拆开
              <br />② 从剧本里提取角色 / 场景 / 道具，写好外观锚点与一致性标签
              <br />③ 为每个片段写出带时间轴、带 <span class="mono">@资产</span> 引用的视频提示词
              <br />④ 建好生成槽位（<b>不会开始出图出片，不产生任何费用</b>）
              <br />
              <span style="color: var(--text-3)">台词与时长由系统校验，不合格的片段会拦在生成之前。</span>
            </div>
          </template>
        </div>
        <div class="import-foot">
          <span class="sub">{{ producing ? "可以先去画布页看 Agent 在做什么" : "有额外要求可以在这里补一句" }}</span>
          <span class="grow"></span>
          <button v-if="!producing" class="btn primary" :disabled="busy" @click="doProduce">开始</button>
        </div>
      </div>
    </div>

    <!-- ───────────────────────────── 粘贴剧本浮层 -->
    <div v-if="importOpen" class="import-mask" @click.self="importOpen = false">
      <div class="import-box">
        <div class="panel-head">
          粘贴剧本
          <span class="grow"></span>
          <label class="toggle">
            <input v-model="importSplit" type="checkbox" />
            按「第 N 集」自动分集
          </label>
          <button class="btn ghost sm" @click="importOpen = false">取消</button>
        </div>
        <textarea
          v-model="importText"
          class="import-area"
          rows="16"
          placeholder="把小说原文或分集剧本整段粘进来。分集标记写成「## 第1集」「第2章」这类都能识别。"
        ></textarea>
        <div class="import-foot">
          <span class="sub">{{ importText.length }} 字 · 预计 {{ estimate }} 集</span>
          <span class="grow"></span>
          <button class="btn primary" :disabled="busy || !importText.trim()" @click="doImport">
            导入并分集
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { studio, loadScript, saveProject, importScript, produceEpisode, toast } from "./studioStore.js";

const busy = ref(false);
const importOpen = ref(false);
const importText = ref("");
const importSplit = ref(true);
const produceOpen = ref(false);
const producing = ref(false);
const form = ref({ aspect_ratio: "9:16", quality: "480P", region: "", genre: "", style_preset: "", video_model: "" });

const episodeNo = computed(() => studio.project?.episode || 1);
const segTotal = computed(() => studio.project?.counts?.segments?.total || 0);

/* 后端只有一个「排队 → 运行中」的真实状态，没有逐段进度。
 * 这里按已用时间给出诚实的阶段性提示，不伪造百分比精度。 */
const produceStage = computed(() => {
  const s = studio.produceJob?.elapsed || 0;
  if (studio.produceJob?.status === "queued") return "排队中，马上开始";
  if (s < 30) return "正在读剧本、定节拍（先算总时长再切段）";
  if (s < 90) return "正在逐段写分镜表和视频提示词";
  return "正在收尾：建生成槽位、连参考图";
});
const producePercent = computed(() => {
  const s = studio.produceJob?.elapsed || 0;
  return Math.min(92, Math.round(8 + (s / 150) * 84));
});

async function doProduce() {
  producing.value = true;
  busy.value = true;
  try {
    const job = await produceEpisode(episodeNo.value);
    const added = job?.added || {};
    toast(
      `制作完成：${added.segments ?? "?"} 个片段 · ${added.assets ?? "?"} 个资产`,
      4200,
    );
    produceOpen.value = false;
    studio.tab = "production";
  } catch (err) {
    toast(err?.message || "Agent 制作失败", 5000);
  } finally {
    producing.value = false;
    busy.value = false;
  }
}

const EPISODE_RE = /^\s*(?:#{1,4}\s*)?第\s*[0-9一二三四五六七八九十百]+\s*[集章回]/gm;
const estimate = computed(() => {
  const hits = (importText.value.match(EPISODE_RE) || []).length;
  return importSplit.value ? Math.max(hits, 1) : 1;
});

async function doImport() {
  const text = importText.value.trim();
  if (!text) return;
  busy.value = true;
  try {
    const res = await importScript(text, importSplit.value);
    const n = (res?.episodes || []).length;
    importOpen.value = false;
    importText.value = "";
    toast(`已导入 ${n} 集`);
  } catch (err) {
    toast(err?.message || "导入失败", 3600);
  } finally {
    busy.value = false;
  }
}

const script = computed(() => studio.script);
const dirty = computed(() => {
  const p = studio.project?.project || {};
  return Object.keys(form.value).some((k) => (form.value[k] || "") !== (p[k] || ""));
});

function sync() {
  const p = studio.project?.project || {};
  form.value = {
    aspect_ratio: p.aspect_ratio || "9:16",
    quality: p.quality || "480P",
    region: p.region || "",
    genre: p.genre || "",
    style_preset: p.style_preset || "",
    video_model: p.video_model || "",
  };
}
watch(() => studio.project, sync, { immediate: true });

function cleanName(n) {
  return String(n || "").replace(/^(角色|场景|道具|风格)[·・.]\s*/, "");
}
function kindText(k) {
  return { character: "角色", scene: "场景", prop: "道具", style: "风格" }[k] || k;
}
function goAsset(a) {
  studio.assetTab = a.kind;
  studio.activeAssetId = a.node_id;
  studio.tab = "assets";
}

async function save() {
  busy.value = true;
  try {
    await saveProject({
      aspect_ratio: form.value.aspect_ratio,
      quality: form.value.quality,
      region: form.value.region,
      genre: form.value.genre,
      style_preset: form.value.style_preset,
      video_model: form.value.video_model,
    });
    toast("已保存");
  } catch (err) {
    toast(err?.message || "保存失败", 3600);
  } finally {
    busy.value = false;
  }
}

async function reload() {
  busy.value = true;
  try {
    await loadScript();
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  if (!studio.script) await loadScript();
});
</script>

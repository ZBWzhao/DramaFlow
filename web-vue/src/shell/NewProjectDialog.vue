<template>
  <div class="np-mask" @click.self="$emit('close')">
    <div class="np-box">
      <div class="np-head">
        <b>新建项目</b>
        <button class="np-x" type="button" @click="$emit('close')">×</button>
      </div>

      <div class="np-body">
        <div class="np-col">
          <label class="np-field">
            <span>项目名称 <i>*</i></span>
            <input v-model="form.title" type="text" placeholder="例如：外卖员深夜送单认出失散多年的姐姐" maxlength="60" />
          </label>

          <label class="np-field">
            <span>其他名称（选填）</span>
            <input v-model="form.alt_title" type="text" placeholder="英文名 / 别名 / 备案名" maxlength="60" />
          </label>

          <div class="np-row">
            <label class="np-field">
              <span>地区</span>
              <select v-model="form.region">
                <option v-for="v in REGIONS" :key="v" :value="v">{{ v }}</option>
              </select>
            </label>
            <label class="np-field">
              <span>类型</span>
              <select v-model="form.genre">
                <option value="">请选择</option>
                <option v-for="v in GENRES" :key="v" :value="v">{{ v }}</option>
              </select>
            </label>
          </div>

          <div class="np-row">
            <label class="np-field">
              <span>画风</span>
              <select v-model="form.style_preset">
                <option value="">请选择</option>
                <option v-for="v in STYLES" :key="v" :value="v">{{ v }}</option>
              </select>
            </label>
            <label class="np-field">
              <span>画面比例</span>
              <select v-model="form.aspect_ratio">
                <option v-for="v in RATIOS" :key="v.value" :value="v.value">{{ v.label }}</option>
              </select>
            </label>
          </div>

          <div class="np-row">
            <label class="np-field">
              <span>视频生成模型</span>
              <select v-model="form.video_model">
                <option v-for="m in videoModels" :key="m.id" :value="m.id">{{ m.label }}</option>
              </select>
            </label>
            <label class="np-field">
              <span>视频生成画质</span>
              <select v-model="form.quality">
                <option v-for="v in QUALITIES" :key="v.value" :value="v.value">{{ v.label }}</option>
              </select>
            </label>
          </div>

          <div class="np-note">
            <span class="np-note-dot" />
            画面比例与画质创建后仍可修改；资产形象会在进入资产页时按需生成。
          </div>
        </div>

        <div class="np-col">
          <div class="np-field">
            <span>
              脚本内容
              <em class="np-script-hint">支持 .md / .txt，粘贴或上传后按「第N集」自动分集</em>
            </span>
            <textarea
              v-model="form.script"
              class="np-script"
              placeholder="把剧本或小说粘进来。用「第1集 / 第2集 / ## 第3集 标题」这类小标题分隔集数，不写也能创建，之后再补。"
            />
          </div>
          <div class="np-script-foot">
            <label class="np-upload">
              <input type="file" accept=".md,.txt,.text" @change="onFile" />
              从文件导入
            </label>
            <span class="np-count">{{ form.script.length }} 字 · 预计 {{ estimate }} 集</span>
          </div>
          <label class="np-toggle">
            <input v-model="form.split" type="checkbox" />
            按「第N集」自动分集
          </label>
        </div>
      </div>

      <div class="np-foot">
        <span v-if="error" class="np-error">{{ error }}</span>
        <span v-else class="np-foot-tip">创建后会直接进入该项目的剧本页</span>
        <div>
          <button class="btn" type="button" @click="$emit('close')">取消</button>
          <button class="btn primary" type="button" :disabled="busy || !form.title.trim()" @click="submit">
            {{ busy ? "创建中…" : "确认创建" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { loadProjects, openProject } from "./shellStore.js";
import "./shell.css";

const emit = defineEmits(["close"]);

const REGIONS = ["国内", "海外"];
const GENRES = ["都市逆袭", "甜宠", "悬疑", "古装", "玄幻", "家庭伦理", "职场"];
const STYLES = ["真人写实", "电影质感", "3D CG", "日系动漫", "水墨国风", "赛博霓虹"];
const RATIOS = [
  { value: "9:16", label: "9:16 竖屏" },
  { value: "16:9", label: "16:9 横屏" },
  { value: "1:1", label: "1:1 方形" },
];
const QUALITIES = [
  { value: "480P", label: "480P（便宜，适合抽卡）" },
  { value: "720P", label: "720P" },
  { value: "1080P", label: "1080P" },
];

const form = ref({
  title: "",
  alt_title: "",
  region: "国内",
  genre: "",
  style_preset: "",
  aspect_ratio: "9:16",
  quality: "480P",
  video_model: "",
  script: "",
  split: true,
});

const busy = ref(false);
const error = ref("");
const providers = ref([]);

const videoModels = computed(() => {
  const active = providers.value.find((p) => p.active);
  const models = (active?.models || []).filter((m) => m.kind === "video");
  return models.length ? models : [{ id: "mock-video-v1", label: "Mock 占位（不产生费用）" }];
});

const estimate = computed(() => {
  const hits = form.value.script.match(/^\s*(?:#{1,4}\s*)?第\s*[0-9一二三四五六七八九十百]+\s*[集章回]/gm);
  return hits?.length || (form.value.script.trim() ? 1 : 0);
});

async function onFile(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  form.value.script = await file.text();
  if (!form.value.title.trim()) {
    form.value.title = file.name.replace(/\.(md|txt|text)$/i, "").slice(0, 40);
  }
  event.target.value = "";
}

async function submit() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const res = await fetch("/api/studio/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form.value),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `创建失败（${res.status}）`);
    await loadProjects();
    emit("close");
    openProject(data.canvas);
  } catch (err) {
    error.value = err?.message || String(err);
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  try {
    const res = await fetch("/api/media/providers");
    const data = await res.json();
    providers.value = data.providers || [];
    form.value.video_model = videoModels.value[0]?.id || "";
  } catch {
    form.value.video_model = "mock-video-v1";
  }
});
</script>

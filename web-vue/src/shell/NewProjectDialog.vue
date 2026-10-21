<script setup>
/* 新建项目（demo v4 弹窗）：风格 Skill 卡 + 剧本导入 + 参数。
 * 逻辑与旧版一致：POST /api/studio/projects（分集/模型/画质/画风），
 * 成功后 loadProjects + 进入该项目剧本页（路由跳转）。
 */
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { loadProjects, openProject } from "./shellStore.js";

const router = useRouter();
const emit = defineEmits(["close"]);

const REGIONS = ["国内", "海外"];
const GENRES = ["都市逆袭", "甜宠", "悬疑", "古装", "玄幻", "家庭伦理", "职场"];
/* 视觉风格 Skill：决定全片画风基调，注入每一段提示词（值 = 后端 style_preset 枚举） */
const STYLE_CARDS = [
  { value: "电影质感", label: "电影写实", small: "写实真人短剧", fx: "fx-a" },
  { value: "日系动漫", label: "日漫清亮", small: "动漫 2D", fx: "fx-b" },
  { value: "水墨国风", label: "古风水墨", small: "国风 / 修饰", fx: "fx-c" },
  { value: "真人写实", label: "真人写实", small: "实拍质感", fx: "fx-d" },
  { value: "3D CG", label: "3D CG", small: "三维渲染", fx: "fx-a" },
  { value: "赛博霓虹", label: "港风霓虹", small: "都市夜色", fx: "fx-b" },
];
const RATIOS = [
  { value: "9:16", label: "9:16 竖屏 · 短剧" },
  { value: "16:9", label: "16:9 横屏" },
  { value: "1:1", label: "1:1 方形" },
];
const QUALITIES = [
  { value: "480P", label: "480P" },
  { value: "720P", label: "720P" },
  { value: "1080P", label: "1080P" },
];

const form = ref({
  title: "",
  alt_title: "",
  region: "国内",
  genre: "",
  style_preset: "电影质感",
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

function pickStyle(card) {
  form.value.style_preset = card.value;
}

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
    router.push(`/project/${data.canvas}/script`);
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

<template>
  <div class="dlg-mask" @click.self="$emit('close')">
    <div class="dlg">
      <h2>新建项目 <button class="x" type="button" @click="$emit('close')">关闭 ✕</button></h2>

      <div class="frm">
        <label class="fld">
          <span>项目名称 <em>*</em></span>
          <input v-model="form.title" class="inp" type="text" maxlength="60" placeholder="例如：外卖员深夜送单认出失散多年的姐姐" />
        </label>
        <label class="fld">
          <span>其他名称（选填）</span>
          <input v-model="form.alt_title" class="inp" type="text" maxlength="60" placeholder="英文名 / 别名 / 备案名" />
        </label>

        <label class="fld">
          <span>地区</span>
          <select v-model="form.region" class="inp">
            <option v-for="v in REGIONS" :key="v" :value="v">{{ v }}</option>
          </select>
        </label>
        <label class="fld">
          <span>类型</span>
          <select v-model="form.genre" class="inp">
            <option value="">请选择</option>
            <option v-for="v in GENRES" :key="v" :value="v">{{ v }}</option>
          </select>
        </label>

        <div class="fld wide">
          <span>视觉风格 Skill <em>*</em> · 决定全片画风基调，注入每一段提示词</span>
          <div class="style-cards">
            <button
              v-for="c in STYLE_CARDS"
              :key="c.value"
              class="style-card"
              :class="{ on: form.style_preset === c.value }"
              type="button"
              @click="pickStyle(c)"
            >
              <span class="im" :class="c.fx"></span>
              <span class="nm">{{ c.label }}<small>{{ c.small }}</small></span>
            </button>
          </div>
        </div>

        <label class="fld">
          <span>画面比例 <em>*</em></span>
          <select v-model="form.aspect_ratio" class="inp">
            <option v-for="v in RATIOS" :key="v.value" :value="v.value">{{ v.label }}</option>
          </select>
        </label>
        <label class="fld">
          <span>画质 <em>*</em></span>
          <select v-model="form.quality" class="inp">
            <option v-for="v in QUALITIES" :key="v.value" :value="v.value">{{ v.label }}</option>
          </select>
        </label>

        <label class="fld wide">
          <span>视频生成模型</span>
          <select v-model="form.video_model" class="inp">
            <option v-for="m in videoModels" :key="m.id" :value="m.id">{{ m.label }}</option>
          </select>
        </label>

        <div class="fld wide">
          <span>剧本内容 <em>*</em> · .md / .txt，粘贴或上传后按「第N集」自动分集</span>
          <textarea
            v-model="form.script"
            class="inp script"
            placeholder="把剧本或小说粘进来。用「第1集 / 第2集 / ## 第3集 标题」这类小标题分隔集数，不写也能创建，之后再补。"
          ></textarea>
          <div class="script-foot">
            <label class="upload">
              <input type="file" accept=".md,.txt,.text" @change="onFile" />
              拖入文件，或点击选择
            </label>
            <span class="count num">{{ form.script.length }} 字 · 预计 {{ estimate }} 集</span>
          </div>
        </div>

        <label class="fld toggle">
          <input v-model="form.split" type="checkbox" />
          按「第N集」自动分集
        </label>
      </div>

      <div class="dlg-foot">
        <span v-if="error" class="err">{{ error }}</span>
        <span v-else class="note"><i>✓</i>创建后自动生成资产表 · 画面比例与画质创建后仍可修改</span>
        <button class="btn" type="button" @click="$emit('close')">取消</button>
        <button class="btn primary" type="button" :disabled="busy || !form.title.trim()" @click="submit">
          {{ busy ? "创建中…" : "确认创建" }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dlg-mask { position: fixed; inset: 0; background: rgba(5, 4, 3, .7); z-index: 50; display: flex; align-items: center; justify-content: center; backdrop-filter: blur(2px); }
.dlg, .dlg * { box-sizing: border-box; margin: 0; padding: 0; }
.dlg { background: var(--panel); border: 1px solid var(--line-2); border-radius: var(--r-3); width: 840px; max-width: 94vw; max-height: 88vh; overflow-y: auto; padding: var(--sp-5) var(--sp-6); }
.dlg h2 { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; margin-bottom: var(--sp-4); display: flex; align-items: center; color: var(--text); }
.dlg h2 .x { margin-left: auto; color: var(--text-3); font-size: var(--fs-13); background: none; border: none; cursor: pointer; }
.dlg h2 .x:hover { color: var(--text); }

.frm { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-4) var(--sp-5); }
.fld > span { display: block; font-size: var(--fs-11); letter-spacing: .1em; color: var(--text-3); margin-bottom: 6px; }
.fld em { color: var(--accent-hi); font-style: normal; }
.fld.wide { grid-column: 1 / -1; }
.inp { width: 100%; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); padding: 9px 12px; color: var(--text); font: inherit; font-size: var(--fs-13); }
.inp:focus { outline: none; border-color: rgba(201, 85, 61, .5); }
select.inp option { background: var(--panel); }
.inp.script { min-height: 110px; resize: vertical; line-height: 1.7; }

.style-cards { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }
.style-card { border: 1px solid var(--line); border-radius: var(--r-2); overflow: hidden; cursor: pointer; background: none; text-align: left; }
.style-card.on { border-color: var(--accent); }
.style-card .im { display: block; height: 64px; }
.fx-a { background: linear-gradient(165deg, #2A2622, #171310 55%, #1F1A14); }
.fx-b { background: linear-gradient(195deg, #241F2B, #131017 60%, #191420); }
.fx-c { background: linear-gradient(150deg, #1C2320, #0F1310 65%, #15201A); }
.fx-d { background: linear-gradient(170deg, #2B2320, #171210 50%, #241A16); }
.style-card .nm { display: flex; justify-content: space-between; align-items: center; gap: 4px; font-size: var(--fs-12); color: var(--text-2); padding: 6px 10px; }
.style-card .nm small { color: var(--text-3); font-size: 9px; }
.style-card.on .nm { color: var(--text); }

.script-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; }
.upload { font-size: var(--fs-12); color: var(--text-3); border: 1px dashed var(--line-2); border-radius: var(--r-1); padding: 7px 14px; cursor: pointer; }
.upload:hover { color: var(--text-2); border-color: var(--line-3); }
.upload input { display: none; }
.count { color: var(--text-3); font-size: var(--fs-11); }
.toggle { display: flex; align-items: center; gap: 8px; color: var(--text-2); font-size: var(--fs-12); grid-column: 1 / -1; }

.dlg-foot { display: flex; align-items: center; gap: var(--sp-3); margin-top: var(--sp-5); }
.dlg-foot .note { margin-right: auto; color: var(--text-3); font-size: var(--fs-12); display: flex; align-items: center; gap: 8px; }
.dlg-foot .note i { width: 13px; height: 13px; border-radius: 3px; background: var(--accent); display: inline-grid; place-items: center; color: #FFF7F0; font-size: 9px; font-style: normal; }
.dlg-foot .err { margin-right: auto; color: var(--bad); font-size: var(--fs-12); }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); cursor: pointer; background: none; border: none; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn:disabled { opacity: .5; pointer-events: none; }
</style>

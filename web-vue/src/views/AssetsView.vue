<script setup>
/* 项目资产（demo v4 屏4）：counted tabs 双栏 + 详情。
 *
 * 逻辑沿用旧 AssetsView：批量出图只挑一个形象都没有的资产（已出图绝不重画）；
 * 形象 based_on 参考链（特写以正面全身锚点为参考）；音色是「声音的参考图」，
 * PATCH 仅支持 voice_id（本期后端边界）；描述只读。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  createLook,
  exportLookPromptUrl,
  fetchTtsVoices,
  flattenAssets,
  generateLook,
  loadAll,
  patchAsset,
  patchLook,
  studio,
  toast,
} from "../studio/studioStore.js";
import { assetUrl as mediaUrl } from "../utils.js";

const route = useRoute();
const busy = ref(false);
const voiceList = ref([]);

const TABS = [
  { key: "character", label: "角色" },
  { key: "scene", label: "场景" },
  { key: "prop", label: "道具" },
  { key: "style", label: "风格" },
];

const currentList = computed(() => studio.assets[studio.assetTab] || []);
const active = computed(() => currentList.value.find((a) => a.node_id === studio.activeAssetId) || null);
const looks = computed(() => active.value?.looks || []);
const selectedLookId = ref("");
const currentLook = computed(() => looks.value.find((l) => l.id === selectedLookId.value) || looks.value[0] || null);
const lookDraft = ref({ name: "", prompt: "", locked: false });

const count = (key) => (studio.assets[key] || []).length;
const readyCount = (key) => (studio.assets[key] || []).filter((a) => a.look_ready).length;

/* 批量出图：只挑一个形象都没有图的（真实生成要花钱，已出图的绝不重画） */
const missingList = computed(() => flattenAssets().filter((a) => !a.look_ready));
const batchBusy = ref(false);
const batchDone = ref(0);

async function batchGenerate() {
  const targets = missingList.value.slice();
  if (!targets.length) return;
  batchBusy.value = true;
  batchDone.value = 0;
  let ok = 0;
  let failed = 0;
  for (const a of targets) {
    try {
      const first = (a.looks || [])[0];
      const look = activeLook(a) || first;
      const basedOn = look && first && look.id !== first.id ? first.id : "";
      await generateLook(a.node_id, look?.id || "", look?.prompt || "", basedOn);
      ok += 1;
    } catch {
      failed += 1;
    }
    batchDone.value += 1;
  }
  batchBusy.value = false;
  toast(`批量出图完成：成功 ${ok} 个${failed ? `，失败 ${failed} 个` : ""}`, 4000);
}

function activeLook(a) {
  const list = a.looks || [];
  return list.find((l) => l.id === a.active_look_id) || list[0] || null;
}

/* 形象生成（带 based_on 参考链） */
async function generate() {
  if (!active.value || !currentLook.value) return;
  busy.value = true;
  try {
    const first = looks.value[0];
    await generateLook(
      active.value.node_id,
      currentLook.value.id || "",
      lookDraft.value.prompt || "",
      first && currentLook.value.id !== first.id ? first.id : "",
    );
    toast("形象图已生成");
  } catch (err) {
    toast(err?.message || "生成失败", 3600);
  } finally {
    busy.value = false;
  }
}

/* 生图占位：不接真实 API，把提示词导出成文本文件，用户拿去任何生图工具用 */
function generateImagePlaceholder() {
  const look = currentLook.value;
  if (!active.value || !look) return;
  if (!(lookDraft.value.prompt || look.prompt || "").trim()) {
    toast("该形象还没有提示词——先在「形象描述」里写好外观锚点", 3600);
    return;
  }
  window.open(exportLookPromptUrl(active.value.node_id, look.id), "_blank");
  toast("生图 API 未接入，已导出提示词文件", 3600);
}

async function saveLook() {
  if (!active.value || !currentLook.value) return;
  busy.value = true;
  try {
    await patchLook(active.value.node_id, {
      name: lookDraft.value.name,
      prompt: lookDraft.value.prompt,
      image_path: currentLook.value.image_path || "",
      locked: lookDraft.value.locked,
    });
    toast("已保存");
  } catch (err) {
    toast(err?.message || "保存失败", 3600);
  } finally {
    busy.value = false;
  }
}

/* 音色：对白配音按它绑定嗓音 */
async function onVoiceChange(e) {
  if (!active.value) return;
  busy.value = true;
  try {
    await patchAsset(active.value.node_id, { voice_id: e.target.value });
    active.value.voice_id = e.target.value;
    toast("音色已保存 · 下次合成生效");
  } catch (err) {
    toast(err?.message || "保存失败");
  } finally {
    busy.value = false;
  }
}

/* 新增形象（换装 / 年龄段） */
const newOpen = ref(false);
const newForm = ref({ name: "", prompt: "", generate: true });
function openNewLook() {
  if (!active.value) return;
  const k = active.value.kind || studio.assetTab;
  newForm.value = {
    name: `形象 ${looks.value.length + 1}`,
    prompt: `${cleanName(active.value.name)}，${k === "character" ? "换装造型" : "另一版本"}`,
    generate: true,
  };
  newOpen.value = true;
}
async function submitNewLook() {
  if (!active.value) return;
  const name = (newForm.value.name || "").trim();
  if (!name) return toast("给这套形象起个名字", 2400);
  busy.value = true;
  try {
    const res = await createLook(active.value.node_id, { name, prompt: newForm.value.prompt, generate: newForm.value.generate });
    newOpen.value = false;
    selectedLookId.value = res?.look?.id || "";
    toast(res?.look?.image_path ? "新形象已出图" : "新形象已创建");
  } catch (err) {
    toast(err?.message || "创建失败", 3600);
  } finally {
    busy.value = false;
  }
}

function cleanName(n) {
  return String(n || "").replace(/^(角色|场景|道具|风格)[·・.]\s*/, "");
}
function kindText(k) {
  return { character: "角色", scene: "场景", prop: "道具", style: "风格" }[k] || k;
}
function roleText(r) {
  return { protagonist: "主角", supporting: "配角", extra: "龙套", lead: "主角" }[r] || r;
}
function syncDraft() {
  selectedLookId.value = currentLook.value?.id || "";
  lookDraft.value = {
    name: currentLook.value?.name || "",
    prompt: currentLook.value?.prompt || "",
    locked: Boolean(currentLook.value?.locked),
  };
}
watch([active, selectedLookId], syncDraft, { immediate: true });

onMounted(async () => {
  const name = String(route.params.name || "");
  if (name && studio.canvas !== name) studio.canvas = name;
  busy.value = true;
  try {
    await loadAll();
  } finally {
    busy.value = false;
  }
  try {
    const data = await fetchTtsVoices();
    voiceList.value = data.voices || [];
  } catch {
    /* 音色清单拉不到不挡资产页 */
  }
});
</script>

<template>
  <div class="ast-lay">
    <!-- ── 左：counted tabs + 网格 ── -->
    <aside class="ast-list">
      <div class="side-cap cap">项目资产 · {{ studio.project?.title }}</div>
      <div class="ast-tabs">
        <button
          v-for="t in TABS"
          :key="t.key"
          class="ast-tab"
          :class="{ on: studio.assetTab === t.key }"
          type="button"
          @click="studio.assetTab = t.key"
        >
          {{ t.label }}<b class="num">{{ readyCount(t.key) }}/{{ count(t.key) }}</b>
        </button>
      </div>

      <div class="ast-grid-wrap">
        <div class="ast-grid">
          <div
            v-for="a in currentList"
            :key="a.node_id"
            class="ast-cell"
            :class="{ cur: a.node_id === studio.activeAssetId }"
            @click="studio.activeAssetId = a.node_id"
          >
            <div class="im">
              <img v-if="activeLook(a)?.image_path" :src="mediaUrl(activeLook(a).image_path)" :alt="cleanName(a.name)" loading="lazy" />
              <span v-if="a.look_ready" class="cnt num">{{ a.look_ready }}/{{ a.look_total }} 形象</span>
              <span v-else class="cnt">缺图</span>
            </div>
            <div class="nm">{{ cleanName(a.name) }}</div>
          </div>
          <div v-if="!currentList.length" class="empty" style="grid-column: 1/-1">这一类还没有资产——从剧本里重新提取会补上</div>
        </div>
      </div>

      <div class="ast-foot">
        <button class="side-act" type="button" @click="toast('从剧本重新提取走「剧本页 → 开始制作」，Agent 会补齐缺失资产')">
          ＋ 从剧本提取 / 新增角色
        </button>
        <button class="side-act primary" type="button" :disabled="batchBusy || !missingList.length" @click="batchGenerate">
          {{ batchBusy ? `批量出图中 ${batchDone}/${missingList.length}` : `批量生成全部形象 (${missingList.length})` }}
        </button>
        <div class="foot-note">批量只画没有图的 · 已出图的形象绝不重画</div>
      </div>
    </aside>

    <!-- ── 右：详情 ── -->
    <div class="ast-detail">
      <div v-if="!active" class="empty">左侧选一个资产</div>

      <template v-else>
        <div class="astd-head">
          <h2>{{ cleanName(active.name) }}</h2>
          <span class="st acc"><i></i>{{ kindText(active.kind) }}</span>
          <span v-if="active.role" class="st" style="color: var(--text-2)"><i style="background: var(--text-3)"></i>{{ roleText(active.role) }}</span>
          <span v-if="active.kind === 'character'" class="st" style="color: var(--gold)"><i style="background: var(--gold)"></i><span class="num">音色 {{ active.voice_id || "默认" }}</span></span>
          <div class="grow"></div>
        </div>

        <!-- 形象 tabs -->
        <div class="look-tabs">
          <div
            v-for="lk in looks"
            :key="lk.id"
            class="look-tab"
            :class="{ cur: lk.id === selectedLookId }"
            @click="selectedLookId = lk.id"
          >
            <div class="im">
              <img v-if="lk.image_path" :src="mediaUrl(lk.image_path)" :alt="lk.name" loading="lazy" />
              <span v-else class="ph-mark">待生成</span>
            </div>
            <div class="nm">{{ lk.name || "默认形象" }}{{ lk.locked ? " · 锁" : "" }}</div>
          </div>
          <div class="look-tab add" @click="openNewLook"><div class="im">＋ 新形象</div><div class="nm">换装 / 年龄段</div></div>
        </div>

        <div v-if="currentLook" class="astd-sec">
          <h4>形象描述 <span class="grow"></span><button class="txt-link" type="button" @click="saveLook">保存修改</button></h4>
          <textarea v-model="lookDraft.prompt" class="desc-edit" rows="3"></textarea>
          <label class="lock-row">
            <input v-model="lookDraft.locked" type="checkbox" @change="saveLook" />
            锁定这套形象——重生成不覆盖（形象定稿后勾上）
          </label>
        </div>

        <div class="astd-sec">
          <h4>资产描述</h4>
          <div class="desc">{{ active.description || "—" }}</div>
        </div>

        <div v-if="active.kind === 'character'" class="astd-sec">
          <h4>音色 · 对白配音按它绑定嗓音</h4>
          <div class="astd-row">
            <select :value="active.voice_id || ''" :disabled="busy" @change="onVoiceChange($event)">
              <option value="">默认音色</option>
              <option v-for="v in voiceList" :key="v" :value="v">{{ v }}</option>
            </select>
            <button class="btn sm" type="button" @click="toast('试听需要 TTS 试听端点（后端暂未开放）；配音合成时会使用该音色')">
              试听
            </button>
            <span class="row-note">每角色固定音色 = 声音的参考图</span>
          </div>
        </div>

        <div class="astd-sec">
          <h4>出场章节</h4>
          <div class="astd-row wrap">
            <span v-for="(ap, i) in currentLook?.appearances || []" :key="i" class="ap-tag">
              {{ typeof ap === "string" ? ap : `${ap.episode ? `第${ap.episode}集` : ""}${ap.scene ? ` · ${ap.scene}` : ""}${ap.shot ? ` #${ap.shot}` : ""}` }}
            </span>
            <span v-if="!currentLook?.appearances?.length" class="desc">—</span>
          </div>
        </div>

        <div class="astd-sec">
          <h4>形象图库</h4>
          <div class="gallery">
            <div v-for="lk in looks.filter((l) => l.image_path)" :key="`g-${lk.id}`" class="gal-item">
              <img :src="mediaUrl(lk.image_path)" :alt="lk.name" loading="lazy" />
              <span class="when">{{ lk.name || "默认" }}</span>
            </div>
            <div v-if="!looks.some((l) => l.image_path)" class="desc">还没有出图——点下方生成</div>
          </div>
        </div>

        <div class="gen-bar">
          <span class="sel num">{{ studio.project?.video_model || "mock" }} · {{ studio.project?.aspect_ratio }}</span>
          <span class="hint">参考链：特写/后续形象以第一个形象（正面全身锚点）为参考</span>
          <button class="btn primary sm" type="button" @click="generateImagePlaceholder">
            生成形象图
          </button>
          <button class="btn sm" type="button" @click="generateImagePlaceholder">导出提示词</button>
        </div>
      </template>
    </div>

    <!-- 新形象弹窗 -->
    <div v-if="newOpen" class="mask" @click.self="newOpen = false">
      <div class="box">
        <h3>新增形象</h3>
        <label class="f"><span>形象名</span><input v-model="newForm.name" class="inp" /></label>
        <label class="f"><span>形象描述（外观锚点）</span><textarea v-model="newForm.prompt" class="inp" rows="3"></textarea></label>
        <label class="f row">
          <input v-model="newForm.generate" type="checkbox" />
          创建后立即生成
        </label>
        <div class="box-foot">
          <button class="btn sm" type="button" @click="newOpen = false">取消</button>
          <button class="btn primary sm" type="button" :disabled="busy" @click="submitNewLook">创建</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ast-lay { display: grid; grid-template-columns: 280px 1fr; height: 100%; min-height: 0; }
.ast-lay, .ast-lay * { box-sizing: border-box; }

.ast-list { background: var(--bg-2); border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
.side-cap { padding: var(--sp-3) var(--sp-3) 0; }
.ast-tabs { display: flex; padding: var(--sp-3) var(--sp-3) 0; border-bottom: 1px solid var(--line); }
.ast-tab { flex: 1; text-align: center; padding: 8px 0 10px; font-size: var(--fs-12); color: var(--text-3); border-bottom: 1px solid transparent; margin-bottom: -1px; background: none; border-top: none; border-left: none; border-right: none; cursor: pointer; }
.ast-tab.on { color: var(--text); border-bottom-color: var(--accent); }
.ast-tab b { display: block; font-weight: 500; font-size: var(--fs-13); }
.ast-grid-wrap { flex: 1; overflow-y: auto; padding: var(--sp-1) var(--sp-3) var(--sp-3); }
.ast-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.ast-cell { cursor: pointer; }
.ast-cell .im { aspect-ratio: 3/4; border-radius: var(--r-1); border: 1px solid var(--line); background: linear-gradient(150deg, #23201B, #14110E); position: relative; overflow: hidden; }
.ast-cell.cur .im { border-color: var(--accent); }
.ast-cell .im img { width: 100%; height: 100%; object-fit: cover; }
.ast-cell .cnt { position: absolute; top: 5px; left: 5px; font-size: 9px; color: var(--text-2); font-family: var(--mono); background: rgba(8, 7, 6, .55); padding: 0 5px; border-radius: 3px; }
.ast-cell .nm { font-size: var(--fs-11); color: var(--text-2); padding: 5px 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.empty { padding: var(--sp-4); color: var(--text-3); font-size: var(--fs-12); line-height: 1.8; }
.ast-foot { padding: var(--sp-3); border-top: 1px solid var(--line); display: flex; flex-direction: column; gap: var(--sp-1); }
.side-act { text-align: left; padding: 11px 12px; border-radius: var(--r-1); font-size: var(--fs-13); color: var(--text-2); border: 1px solid var(--line); background: none; cursor: pointer; }
.side-act:hover { border-color: var(--line-3); color: var(--text); }
.side-act.primary { background: var(--accent); border-color: var(--accent); color: #FFF7F0; text-align: center; }
.side-act.primary:hover { background: var(--accent-hi); }
.side-act:disabled { opacity: .5; pointer-events: none; }
.foot-note { color: var(--text-3); font-size: var(--fs-11); }

.ast-detail { overflow-y: auto; padding: var(--sp-5) var(--sp-6) 96px; min-height: 0; }
.astd-head { display: flex; align-items: baseline; gap: var(--sp-3); padding-bottom: var(--sp-3); border-bottom: 1px solid var(--line); max-width: 800px; }
.astd-head h2 { font-family: var(--serif); font-size: var(--fs-20); font-weight: 600; }
.grow { flex: 1; }
.st { display: inline-flex; align-items: center; gap: 6px; font-size: var(--fs-12); color: var(--text-3); }
.st i { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
.st.acc { color: var(--accent-hi); }

.look-tabs { display: flex; gap: 10px; padding: var(--sp-4) 0; max-width: 800px; flex-wrap: wrap; }
.look-tab { width: 84px; cursor: pointer; }
.look-tab .im { aspect-ratio: 3/4; border-radius: var(--r-1); border: 1px solid var(--line); background: linear-gradient(150deg, #23201B, #14110E); overflow: hidden; display: grid; place-items: center; }
.look-tab.cur .im { border-color: var(--accent); }
.look-tab .im img { width: 100%; height: 100%; object-fit: cover; }
.look-tab .ph-mark { font-size: 10px; color: var(--text-3); }
.look-tab .nm { font-size: 10px; color: var(--text-3); padding-top: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.look-tab.add .im { border-style: dashed; color: var(--text-3); font-size: var(--fs-11); }
.look-tab.add:hover .im { border-color: var(--line-3); color: var(--text-2); }

.astd-sec { padding: var(--sp-3) 0; border-top: 1px solid var(--line); max-width: 800px; }
.astd-sec > h4 { font-size: var(--fs-11); letter-spacing: .14em; color: var(--text-3); font-weight: 500; margin-bottom: 10px; display: flex; align-items: center; }
.astd-sec > h4 .grow { flex: 1; }
.txt-link { color: var(--accent-hi); font-size: var(--fs-12); background: none; border: none; cursor: pointer; }
.desc { font-size: var(--fs-13); color: var(--text-2); line-height: 1.9; }
.desc-edit { width: 100%; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); color: var(--text); font: inherit; font-size: var(--fs-13); padding: 8px 10px; resize: vertical; }
.desc-edit:focus { outline: none; border-color: rgba(201, 85, 61, .5); }
.lock-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; color: var(--text-3); font-size: var(--fs-12); }
.astd-row { display: flex; gap: var(--sp-3); align-items: center; font-size: var(--fs-13); color: var(--text-2); }
.astd-row.wrap { flex-wrap: wrap; }
.astd-row select { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); padding: 6px 10px; color: var(--text); font: inherit; font-size: var(--fs-12); }
.astd-row select option { background: var(--panel); }
.row-note { color: var(--text-3); font-size: var(--fs-11); }
.ap-tag { font-size: var(--fs-12); color: var(--text-2); border: 1px solid var(--line); border-radius: var(--r-1); padding: 3px 10px; }
.gallery { display: flex; gap: 10px; flex-wrap: wrap; }
.gal-item { width: 84px; aspect-ratio: 3/4; border-radius: var(--r-1); border: 1px solid var(--line); background: linear-gradient(150deg, #23201B, #14110E); position: relative; overflow: hidden; }
.gal-item img { width: 100%; height: 100%; object-fit: cover; }
.gal-item .when { position: absolute; bottom: 4px; left: 5px; font-size: 9px; color: var(--text-2); text-shadow: 0 1px 3px rgba(0, 0, 0, .8); }
.gen-bar { display: flex; align-items: center; gap: var(--sp-2); max-width: 800px; padding-top: var(--sp-3); }
.gen-bar .sel { padding: 7px 14px; border: 1px solid var(--line); border-radius: var(--r-1); font-size: var(--fs-13); color: var(--text-2); font-family: var(--mono); }
.gen-bar .hint { margin-left: auto; color: var(--text-3); font-size: var(--fs-11); }

.mask { position: fixed; inset: 0; background: rgba(5, 4, 3, .7); z-index: 50; display: grid; place-items: center; }
.box { background: var(--panel); border: 1px solid var(--line-2); border-radius: var(--r-3); width: 460px; max-width: 94vw; padding: var(--sp-5); }
.box h3 { font-family: var(--serif); font-size: var(--fs-16); margin-bottom: var(--sp-4); }
.f { display: block; margin-bottom: var(--sp-3); }
.f > span { display: block; font-size: var(--fs-11); letter-spacing: .1em; color: var(--text-3); margin-bottom: 6px; }
.f.row { display: flex; align-items: center; gap: 8px; color: var(--text-2); font-size: var(--fs-12); }
.inp { width: 100%; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); padding: 9px 12px; color: var(--text); font: inherit; font-size: var(--fs-13); resize: vertical; }
.inp:focus { outline: none; border-color: rgba(201, 85, 61, .5); }
.box-foot { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-4); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 20px; border-radius: var(--r-1); font-size: var(--fs-14); color: var(--text-2); cursor: pointer; background: none; border: none; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn.sm { padding: 6px 14px; font-size: var(--fs-13); }
.btn:disabled { opacity: .5; pointer-events: none; }
</style>

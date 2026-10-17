<template>
  <div class="assets-wrap">
    <!-- ───────────────────────────── 左：资产网格 -->
    <div style="display: flex; flex-direction: column; gap: 10px; min-height: 0">
      <div style="display: flex; align-items: center; gap: 8px">
        <div class="studio-tabs" style="margin-left: 0">
          <button
            v-for="t in TABS"
            :key="t.key"
            :class="{ on: studio.assetTab === t.key }"
            @click="studio.assetTab = t.key"
          >
            {{ t.label }}<span style="opacity: 0.55; margin-left: 4px">{{ count(t.key) }}</span>
          </button>
        </div>
        <span style="font-size: 11px; color: var(--text-3)">
          分子是已出图，分母是这段剧本需要的数量
        </span>
        <span style="flex: 1"></span>
        <button
          class="btn primary sm"
          :disabled="batchBusy || !missingList.length"
          :title="missingList.length ? '' : '所有资产都已经出图了'"
          @click="batchGenerate"
        >
          {{ batchBusy ? `批量出图中 ${batchDone}/${batchTotal}` : `批量生成全部形象 (${missingList.length})` }}
        </button>
      </div>

      <div class="asset-grid" style="overflow-y: auto; padding-right: 2px">
        <div
          v-for="a in currentList"
          :key="a.node_id"
          class="asset-card"
          :class="{ on: a.node_id === studio.activeAssetId }"
          @click="studio.activeAssetId = a.node_id"
        >
          <div class="thumb">
            <img v-if="activeLook(a)?.image_path" :src="mediaUrl(activeLook(a).image_path)" :alt="a.name" />
            <span v-else>{{ kindIcon(a.kind) }}</span>
            <span v-if="a.look_ready" class="badge">已就绪</span>
          </div>
          <div class="info">
            <div class="nm">{{ cleanName(a.name) }}</div>
            <div class="meta">
              <span class="tag role" v-if="a.role">{{ roleText(a.role) }}</span>
              <span>{{ a.look_ready }}/{{ a.look_total }} 形象</span>
            </div>
          </div>
        </div>
        <div v-if="!currentList.length" class="empty" style="grid-column: 1 / -1">
          这一类还没有资产<br />从剧本里重新提取会补上
        </div>
      </div>
    </div>

    <!-- ───────────────────────────── 右：详情 -->
    <div class="panel" style="min-height: 0">
      <div class="panel-head">
        {{ active ? cleanName(active.name) : "资产详情" }}
        <span v-if="active" class="tag">{{ kindText(active.kind) }}</span>
        <span class="grow"></span>
        <span v-if="active" class="tag" :class="active.look_ready ? 'ok' : 'warn'">
          {{ active.look_ready }}/{{ active.look_total }} 形象
        </span>
      </div>

      <div v-if="!active" class="empty">左侧选一个资产</div>

      <div v-else class="panel-body" style="display: flex; flex-direction: column; gap: 14px">
        <!-- 形象条 -->
        <div>
          <div class="studio-section-title" style="margin-bottom: 8px">
            <span>形象</span>
            <span style="text-transform: none; letter-spacing: 0">
              {{ looks.length }} 个 · 当前 {{ currentLook?.name || "默认" }}
            </span>
          </div>
          <div class="look-strip">
            <div
              v-for="lk in looks"
              :key="lk.id"
              class="look-thumb"
              :class="{ on: lk.id === selectedLookId }"
              @click="selectedLookId = lk.id"
            >
              <img v-if="lk.image_path" :src="mediaUrl(lk.image_path)" :alt="lk.name" />
              <div v-else style="height: 96px; display: grid; place-items: center; font-size: 10px; color: var(--text-3)">
                待生成
              </div>
              <div class="cap" :title="lk.name">{{ lk.name || "默认形象" }}</div>
            </div>
            <div class="look-thumb look-add" title="新增一套形象（换装 / 年龄段）" @click="openNewLook">
              <div style="height: 96px; display: grid; place-items: center; font-size: 20px; color: var(--text-3)">+</div>
              <div class="cap">新形象</div>
            </div>
          </div>
        </div>

        <!-- 当前形象字段 -->
        <div v-if="currentLook">
          <div class="field">
            <label>形象名（换装 / 年龄段用不同形象）</label>
            <input v-model="lookDraft.name" />
          </div>
          <div class="field">
            <label>出场</label>
            <div style="display: flex; flex-wrap: wrap; gap: 4px">
              <span v-for="(ap, i) in currentLook.appearances || []" :key="i" class="tag info">
                {{ ap.episode ? `第${ap.episode}集` : "" }}{{ ap.scene ? ` · ${ap.scene}` : "" }}{{
                  ap.shot ? ` #${ap.shot}` : typeof ap === "string" ? ap : ""
                }}
              </span>
              <span v-if="!(currentLook.appearances || []).length" style="font-size: 11px; color: var(--text-3)">
                还没统计出场
              </span>
            </div>
          </div>
          <div class="field">
            <label>形象提示词（生图用，改它能换气质）</label>
            <textarea v-model="lookDraft.prompt" rows="6" spellcheck="false"></textarea>
          </div>
          <div style="display: flex; gap: 8px; align-items: center">
            <button class="btn primary" :disabled="busy" @click="generate">
              {{ currentLook.image_path ? "重新生成" : "生成形象图" }}
            </button>
            <button class="btn" :disabled="busy" @click="saveLook">保存字段</button>
            <button
              class="btn"
              :class="{ primary: lookDraft.locked }"
              :disabled="busy"
              @click="toggleLock"
            >
              {{ lookDraft.locked ? "已锁定" : "锁定这个形象" }}
            </button>
            <span v-if="lookDraft.locked" class="tag ok">锁定后改剧本不会覆盖它</span>
          </div>
        </div>

        <!-- 资产本体 -->
        <div style="border-top: 1px solid var(--line); padding-top: 12px">
          <div class="field">
            <label>描述</label>
            <div class="val">{{ active.description || "—" }}</div>
          </div>
          <div class="field">
            <label>一致性标签</label>
            <div style="display: flex; flex-wrap: wrap; gap: 4px">
              <span v-for="t in active.consistency_tags || []" :key="t" class="tag">{{ t }}</span>
              <span v-if="!(active.consistency_tags || []).length" style="font-size: 11px; color: var(--text-3)">
                无
              </span>
            </div>
          </div>
          <div class="field">
            <label style="display: flex; align-items: center; gap: 6px; cursor: pointer" @click="showSpec = !showSpec">
              <span>{{ showSpec ? "▾" : "▸" }}</span> 资产规格（提词依据）
            </label>
            <div
              v-if="showSpec"
              class="mono"
              style="white-space: pre-wrap; line-height: 1.7; background: var(--card); padding: 9px; border-radius: var(--ui-radius-small); max-height: 220px; overflow: auto"
            >
              {{ active.spec || "—" }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ───────────────────────────── 新形象浮层 -->
    <div v-if="newOpen" class="import-mask" @click.self="newOpen = false">
      <div class="import-box" style="width: min(520px, 92vw)">
        <div class="panel-head">
          新增形象
          <span class="sub">{{ cleanName(active?.name || "") }}</span>
          <span class="grow"></span>
          <button class="btn ghost sm" @click="newOpen = false">取消</button>
        </div>
        <div class="panel-body" style="display: flex; flex-direction: column; gap: 10px">
          <div class="field">
            <label>形象名（同一个角色的不同造型，例如「神社巫女装束」「东京成人便装」）</label>
            <input v-model="newForm.name" />
          </div>
          <div class="field">
            <label>形象提示词（生图用，会自动补图种规矩与全剧画风）</label>
            <textarea v-model="newForm.prompt" rows="5" spellcheck="false"></textarea>
          </div>
          <label class="toggle">
            <input v-model="newForm.generate" type="checkbox" />
            创建后立刻出图
          </label>
        </div>
        <div class="import-foot">
          <span class="sub">{{ newForm.generate ? "会调用一次生图" : "只建空形象，稍后再生成" }}</span>
          <span class="grow"></span>
          <button class="btn primary" :disabled="busy" @click="submitNewLook">
            {{ newForm.generate ? "创建并出图" : "创建" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { studio, generateLook, patchLook, createLook, toast } from "./studioStore.js";

const TABS = [
  { key: "character", label: "角色" },
  { key: "scene", label: "场景" },
  { key: "prop", label: "道具" },
  { key: "style", label: "风格" },
];

const busy = ref(false);
const showSpec = ref(false);
const selectedLookId = ref("");
const lookDraft = ref({ name: "", prompt: "", locked: false });
const batchBusy = ref(false);
const batchDone = ref(0);
const batchTotal = ref(0);

/* 批量出图只挑「一个形象都还没有图」的资产。
 * 已经出过图的绝不重画：真实生成要花钱，重画还会打乱已经在镜头里用着的形象。 */
const missingList = computed(() =>
  ["character", "scene", "prop"]
    .flatMap((k) => studio.assets[k] || [])
    .filter((a) => !a.look_ready),
);

async function batchGenerate() {
  const targets = missingList.value.slice();
  if (!targets.length) return;
  batchBusy.value = true;
  batchDone.value = 0;
  batchTotal.value = targets.length;
  let ok = 0;
  let failed = 0;
  for (const a of targets) {
    try {
      const look = activeLook(a) || (a.looks || [])[0];
      await generateLook(a.node_id, look?.id || "", look?.prompt || "");
      ok += 1;
    } catch (err) {
      failed += 1;
    }
    batchDone.value += 1;
  }
  batchBusy.value = false;
  toast(`批量出图完成：成功 ${ok} 个${failed ? `，失败 ${failed} 个` : ""}`, 4000);
}

const currentList = computed(() => studio.assets[studio.assetTab] || []);
const active = computed(() => currentList.value.find((a) => a.node_id === studio.activeAssetId) || null);
const looks = computed(() => active.value?.looks || []);
const currentLook = computed(() => looks.value.find((l) => l.id === selectedLookId.value) || looks.value[0] || null);

function count(key) {
  return (studio.assets[key] || []).length;
}
function activeLook(a) {
  const list = a.looks || [];
  return list.find((l) => l.id === a.active_look_id) || list[0] || null;
}
function cleanName(n) {
  return String(n || "").replace(/^(角色|场景|道具|风格)[·・.]\s*/, "");
}
function kindText(k) {
  return { character: "角色", scene: "场景", prop: "道具", style: "风格" }[k] || k;
}
function kindIcon(k) {
  return { character: "人", scene: "景", prop: "物", style: "风格" }[k] || "?";
}
function roleText(r) {
  return { protagonist: "主角", supporting: "配角", extra: "龙套", lead: "主角" }[r] || r;
}
function mediaUrl(p) {
  if (!p) return "";
  if (/^(https?:|data:|blob:)/.test(p)) return p;
  const clean = String(p).replace(/^\/+/, "");
  return clean.startsWith("assets/") ? "/" + clean : "/assets/" + clean;
}

function syncDraft() {
  const lk = currentLook.value;
  selectedLookId.value = lk?.id || "";
  lookDraft.value = {
    name: lk?.name || "",
    prompt: lk?.prompt || "",
    locked: Boolean(lk?.locked),
  };
}

watch([active, selectedLookId], syncDraft, { immediate: true });

async function generate() {
  if (!active.value || !currentLook.value) return;
  busy.value = true;
  try {
    await generateLook(active.value.node_id, currentLook.value.id || "", lookDraft.value.prompt || "");
    toast("形象图已生成");
    syncDraft();
  } catch (err) {
    toast(err?.message || "生成失败", 3600);
  } finally {
    busy.value = false;
  }
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

async function toggleLock() {
  lookDraft.value.locked = !lookDraft.value.locked;
  await saveLook();
}

/* ── 新增形象 ───────────────────────────────── */
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
  if (!name) {
    toast("给这套形象起个名字", 2400);
    return;
  }
  busy.value = true;
  try {
    const res = await createLook(active.value.node_id, {
      name,
      prompt: newForm.value.prompt,
      generate: newForm.value.generate,
    });
    newOpen.value = false;
    selectedLookId.value = res?.look?.id || "";
    toast(res?.look?.image_path ? "新形象已出图" : "新形象已创建");
  } catch (err) {
    toast(err?.message || "创建失败", 3600);
  } finally {
    busy.value = false;
  }
}
</script>

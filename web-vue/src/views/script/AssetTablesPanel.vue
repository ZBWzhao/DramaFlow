<script setup>
/* 资产表（草稿区）：三 tab 网格 + 重新解析 / 同步资产库。
 *
 * 原则（与后端一致）：表是草稿、库是资产——重新解析只覆写表；同步只增不改不删。
 * 角色表按「角色×形象」平铺展示（存储是角色一条记录 + 形象子数组），
 * 同角色第 2+ 行前三列显示「同上」。行内直改，失焦整表保存。
 */
import { computed, ref } from "vue";
import { errorText } from "../../api.js";
import { parseAssetTables, saveAssetTables, studio, syncAssetTables } from "../../studio/studioStore.js";

const TABS = [
  { key: "characters", label: "角色" },
  { key: "scenes", label: "场景" },
  { key: "props", label: "道具" },
];
const tab = ref("characters");
const notice = ref("");
const error = ref("");

const tables = computed(() => studio.assetTables || { characters: [], scenes: [], props: [] });
const parsing = computed(() => studio.parsing.running);
const syncing = computed(() => studio.syncing);
const hasTable = computed(() => Boolean((tables.value.characters || []).length || (tables.value.scenes || []).length || (tables.value.props || []).length));
const skillTag = computed(() => {
  const s = tables.value.skill || {};
  return s.name ? `${s.name}@${s.version}` : "";
});
const syncedText = computed(() => (tables.value.synced_at ? new Date(tables.value.synced_at * 1000).toLocaleString() : "未同步"));

/* ---------- 同步状态：行指纹 vs 上次同步快照 ---------- */
/* 与后端 _row_sig 同算法：name␟desc␟bio␟形象名排序拼接 */
const rowSig = (row) => [
  row.name || "",
  row.description || "",
  row.bio || "",
  (row.looks || []).map((l) => l.name || "").sort().join("|"),
].join("␟");
const STATE_TIP = { synced: "已入库（与上次同步一致）", dirty: "同步后改过，还没同步", new: "未入库（同步后新增或从未同步）" };
/* 库里现有资产名（来自 /script 的 asset_items 投影），kinds: character/scene/prop */
const libNames = computed(() => {
  const set = new Set();
  for (const it of studio.script?.asset_items || []) set.add(`${it.kind}:${String(it.name || "").toLowerCase()}`);
  return set;
});
const KIND_TO_LIB = { characters: "character", scenes: "scene", props: "prop" };
function rowState(kind, row) {
  const name = String(row.name || "").trim();
  if (!name) return "new";
  const snap = tables.value.synced_state?.[kind] || {};
  if (!(name in snap)) return "new";
  if (snap[name] !== rowSig(row)) return "dirty";
  return libNames.value.has(`${KIND_TO_LIB[kind]}:${name.toLowerCase()}`) ? "synced" : "dirty";
}

/* ---------- 搜索 ---------- */
const q = ref("");
const qLower = computed(() => q.value.trim().toLowerCase());
const hitRow = (row) =>
  !qLower.value ||
  [row.name, row.description, row.bio, ...(row.looks || []).map((l) => l.name)].some((v) =>
    String(v || "").toLowerCase().includes(qLower.value),
  );
const charRows = computed(() =>
  (tables.value.characters || [])
    .map((c, ci) => ({ c, ci })) // 先带原始索引再过滤——删行/加形象按 ci 回写全表
    .filter(({ c }) => !qLower.value || hitRow(c))
    .flatMap(({ c, ci }) => {
      const looks = (c.looks || []).length ? c.looks : [{ name: "" }];
      return looks.map((l, li) => ({ c, ci, l, li, first: li === 0 }));
    }),
);
const plainRows = computed(() => (tables.value[tab.value] || []).filter(hitRow));
const shownCount = computed(() => (tab.value === "characters" ? charRows.value.filter((r) => r.first).length : plainRows.value.length));
const totalCount = computed(() => (tables.value[tab.value] || []).length);

function markSaved() {
  notice.value = "已保存";
  setTimeout(() => (notice.value = ""), 1500);
}

async function persist() {
  error.value = "";
  try {
    await saveAssetTables(JSON.parse(JSON.stringify(tables.value)));
    markSaved();
  } catch (err) {
    error.value = errorText(err);
  }
}

function addCharacter() {
  studio.assetTables.characters.push({ name: "", description: "", bio: "", looks: [] });
}
function addScene() {
  studio.assetTables.scenes.push({ name: "", description: "" });
}
function addProp() {
  studio.assetTables.props.push({ name: "", description: "" });
}
function addLook(row) {
  const c = studio.assetTables.characters[row.ci];
  if (!Array.isArray(c.looks)) c.looks = [];
  c.looks.push({ name: "" });
}
function removeCharacter(ci) {
  studio.assetTables.characters.splice(ci, 1);
  persist();
}
function removeLook(row) {
  studio.assetTables.characters[row.ci].looks.splice(row.li, 1);
  persist();
}

async function reparse() {
  error.value = "";
  notice.value = "";
  try {
    await parseAssetTables();
    notice.value = "解析完成——只改了表，资产库未动";
  } catch (err) {
    error.value = errorText(err);
  }
}

async function sync() {
  error.value = "";
  try {
    const report = await syncAssetTables();
    const merged = (report.merged || []).length ? `，归并 ${(report.merged || []).length} 条别名` : "";
    notice.value = `同步完成：新建资产 ${report.created_assets}、新增形象 ${report.created_looks}${merged}`;
  } catch (err) {
    error.value = errorText(err);
  }
}
</script>

<template>
  <div class="at">
    <div class="at-head">
      <button
        v-for="tb in TABS"
        :key="tb.key"
        class="tbtn"
        :class="{ on: tab === tb.key }"
        type="button"
        @click="tab = tb.key"
      >
        {{ tb.label }} {{ (tables[tb.key] || []).length || "" }}
      </button>
      <input v-model="q" class="q" type="search" placeholder="搜名称 / 描述…" />
      <span class="meta num">{{ skillTag ? `${skillTag} 解析` : "" }} · 上次同步 {{ syncedText }}</span>
      <span style="flex: 1"></span>
      <button class="btn sm" type="button" :disabled="parsing" @click="reparse">
        {{ parsing ? `解析中… 第${studio.parsing.episodeNo || ""}集` : hasTable ? "重新解析" : "重新解析（AI 从剧本抽取）" }}
      </button>
      <button class="btn sm primary" type="button" :disabled="syncing || !hasTable" @click="sync">
        {{ syncing ? "同步中…" : "同步资产库" }}
      </button>
    </div>

    <div v-if="notice" class="tip ok">{{ notice }}</div>
    <div v-if="error" class="tip err">{{ error }}</div>

    <div class="at-scroll">
      <div v-if="!hasTable" class="at-empty">
        还没有资产表。<b>重新解析</b>让 AI 从剧本抽取角色 / 场景 / 道具三表——
        只生成<b>草稿表</b>，不会生图；你在这里增删改，再点「同步资产库」入库。
      </div>

      <div v-else-if="!shownCount" class="at-empty">没有匹配「{{ q }}」的行</div>

      <!-- 角色：4 列网格，角色×形象平铺；名字前状态点 = 与资产库的同步关系 -->
      <div v-else-if="tab === 'characters'" class="grid c4">
        <div class="h">角色名</div><div class="h">角色描述</div><div class="h">人物小传</div><div class="h">形象名</div>
        <template v-for="row in charRows" :key="`${row.ci}-${row.li}`">
          <div v-if="row.first" class="cell-name" :class="rowState('characters', row.c)">
            <i class="st-dot" :title="STATE_TIP[rowState('characters', row.c)]"></i>
            <input v-model="row.c.name" class="cell" @blur="persist" @keydown.enter="persist" />
          </div>
          <div v-else class="cell dup">同上</div>
          <input
            v-if="row.first"
            v-model="row.c.description"
            class="cell"
            placeholder="一句身份"
            @blur="persist"
          />
          <div v-else class="cell dup">同上</div>
          <input v-if="row.first" v-model="row.c.bio" class="cell" placeholder="外形小传（身高体材/脸型发型）" @blur="persist" />
          <div v-else class="cell dup">同上</div>
          <div class="cell look">
            <input v-model="row.l.name" class="cell-in" placeholder="服装/状态形象名" @blur="persist" />
            <button v-if="row.first" class="mini" type="button" title="追加形象" @click="addLook(row)">＋</button>
            <button v-else class="mini" type="button" title="删除该形象" @click="removeLook(row)">✕</button>
          </div>
        </template>
        <div class="ops">
          <button class="btn sm" type="button" @click="addCharacter">＋角色</button>
          <span class="shown num">显示 {{ shownCount }} / {{ totalCount }}</span>
        </div>
      </div>

      <!-- 场景 / 道具：2 列网格 -->
      <div v-else class="grid c2">
        <div class="h">{{ tab === "scenes" ? "场景名" : "道具名" }}</div><div class="h">描述</div>
        <template v-for="(row, ri) in plainRows" :key="tables[tab].indexOf(row)">
          <div class="cell-name" :class="rowState(tab, row)">
            <i class="st-dot" :title="STATE_TIP[rowState(tab, row)]"></i>
            <input v-model="row.name" class="cell" @blur="persist" />
          </div>
          <div class="cell look">
            <input v-model="row.description" class="cell-in" placeholder="一句功能描述" @blur="persist" />
            <button class="mini" type="button" title="删除" @click="tables[tab].splice(tables[tab].indexOf(row), 1); persist()">✕</button>
          </div>
        </template>
        <div class="ops">
          <button class="btn sm" type="button" @click="tab === 'scenes' ? addScene() : addProp()">＋{{ tab === "scenes" ? "场景" : "道具" }}</button>
          <span class="shown num">显示 {{ shownCount }} / {{ totalCount }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.at { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.at-head { display: flex; align-items: center; gap: var(--sp-2); padding-bottom: var(--sp-3); }
.tbtn { border: none; background: none; color: var(--text-3); font-size: var(--fs-13); padding: 5px 10px; cursor: pointer; border-radius: var(--r-1); font-family: inherit; }
.tbtn:hover { color: var(--text); }
.tbtn.on { color: var(--text); background: var(--card-2); }
.at-head .meta { font-size: var(--fs-11); color: var(--text-3); margin-left: var(--sp-2); }
.q { width: 150px; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); color: var(--text); font-size: var(--fs-12); padding: 5px 9px; font-family: inherit; }
.q:focus { outline: none; border-color: rgba(194, 163, 107, .5); }

/* 行同步状态点：绿=已入库 · 琥珀=改过未同步 · 红棕=未入库 */
.cell-name { display: flex; align-items: center; gap: 6px; min-width: 0; }
.cell-name .cell { flex: 1; min-width: 0; }
.st-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--text-3); }
.cell-name.synced .st-dot { background: var(--ok, #6a994e); }
.cell-name.dirty .st-dot { background: var(--warn, #d9a441); }
.cell-name.new .st-dot { background: #c2553d; }
.shown { font-size: var(--fs-11); color: var(--text-3); margin-left: 10px; }

.tip { font-size: var(--fs-12); padding: 6px 10px; border-radius: var(--r-1); margin-bottom: var(--sp-2); }
.tip.ok { background: rgba(106, 153, 78, .12); color: #6a994e; }
.tip.err { background: rgba(196, 74, 58, .12); color: #c44a3a; }

.at-scroll { flex: 1; min-height: 0; overflow-y: auto; padding-bottom: var(--sp-4); }
.at-empty { padding: var(--sp-4); border: 1px dashed var(--line-2); border-radius: var(--r-2); color: var(--text-2); font-size: var(--fs-13); line-height: 1.8; }

.grid { display: grid; gap: 6px 10px; align-content: start; }
.grid.c4 { grid-template-columns: 1.1fr 1.6fr 2fr 1.4fr; }
.grid.c2 { grid-template-columns: 1.1fr 2.4fr; }
.grid .h { font-size: var(--fs-11); color: var(--text-3); padding: 2px 0; }
.cell { width: 100%; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); color: var(--text); font-size: var(--fs-13); padding: 7px 9px; font-family: inherit; }
.cell.dup { background: none; border: none; color: var(--text-3); display: flex; align-items: center; }
.cell.look { display: flex; gap: 5px; align-items: center; padding: 0; border: none; background: none; }
.cell-in { flex: 1; min-width: 0; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-1); color: var(--text); font-size: var(--fs-13); padding: 7px 9px; font-family: inherit; }
.mini { border: 1px solid var(--line-2); background: none; color: var(--text-3); border-radius: var(--r-1); width: 26px; height: 26px; cursor: pointer; flex: none; }
.mini:hover { color: var(--text); border-color: var(--gold); }
.ops { grid-column: 1 / -1; padding-top: var(--sp-2); }
</style>

<script setup>
/* 失败交互卡（红，spec §2 failed 行）：findings 列表 + 折叠草稿编辑器 + 盲重试/带修改重试。
 * 模板与文案逐字迁自 CreateRunPanel failbox（:306-356）；草稿编辑器逻辑（drafts GET、
 * JSON 守门、盲重试/带修改重试、切换 run 丢弃过期响应）逐字迁自 :168-239——这是
 * failure-recovery 已验收功能，不许简化。stage/findings 由事件帧 props 供给，
 * run/retryRun/drafts 读取全部 store 直读（归属判定 runHere 一并保留，守 watch 不串台）。 */
import { computed, ref, watch } from "vue";
import { errorText, getJSON } from "../../../api.js";
import { retryRun, studio } from "../../../studio/studioStore.js";
import { STAGE_LABELS } from "./useRunFeed.js";

const props = defineProps({
  stage: { type: String, default: "" },
  findings: { type: Array, default: () => [] },
});

const stageLabel = (s) => STAGE_LABELS[s] || s || "";

const run = computed(() => studio.run);
/* 归属判定（CreateRunPanel 同口径）：别项目活动 run 的失败卡绝不发重试/拉草稿 */
const runHere = computed(() => Boolean(run.value.id) && run.value.project_id === studio.canvas);

const retryError = ref("");
const retrying = ref(false);

async function retryFailedStage() {
  if (!run.value.stage) return;
  if (retrying.value) return;
  retrying.value = true;
  retryError.value = "";
  try {
    await retryRun(run.value.stage);
  } catch (err) {
    retryError.value = errorText(err);
  } finally {
    retrying.value = false;
  }
}

/* ── 失败卡草稿编辑器（failure-recovery Task 4，逐字迁移）─────────
 * 失败时（挂载即失败、或直播途中转失败都会触发 watch）拉取该阶段的重试种子
 * {draft, instruction} 预填编辑器；用户展开改稿后由「带着修改重试」随 retry
 * 发回。没动过草稿就不带 draft——后端按原样重跑，与旧盲重试行为完全一致；
 * 盲重试（空参）会清旧种子，所以每次转入 failed 都要重拉一次。 */
const draftOpen = ref(false);
const draftText = ref("");
const draftPrefill = ref(""); // 预填原文：编辑器文本与它不同 = 用户改过
const draftLoading = ref(false);
const draftLoadError = ref("");
const retryInstruction = ref("");

/* 解析 + 守门：draft 必须是 JSON 对象（后端 400 契约），坏 JSON 红字提示并
 * 禁用「带着修改重试」，不让注定失败的请求出门。 */
const draftParsed = computed(() => {
  const text = draftText.value;
  if (!text.trim()) return { ok: false, value: null, error: "草稿为空" };
  try {
    const value = JSON.parse(text);
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return { ok: false, value: null, error: "草稿必须是 JSON 对象（以 { 开头）" };
    }
    return { ok: true, value, error: "" };
  } catch (err) {
    return { ok: false, value: null, error: err?.message || String(err) };
  }
});
const draftDirty = computed(() => draftText.value !== draftPrefill.value);
const canRetryWithEdits = computed(
  () => Boolean(run.value.stage) && !retrying.value && (!draftDirty.value || draftParsed.value.ok),
);

watch(
  () => [run.value.id, run.value.status, run.value.stage],
  async () => {
    if (!runHere.value || run.value.status !== "failed" || !run.value.stage) return;
    const runId = run.value.id;
    const stage = run.value.stage;
    draftLoading.value = true;
    draftLoadError.value = "";
    try {
      const data = await getJSON(`/api/pipeline/runs/${runId}/drafts/${stage}`);
      if (run.value.id !== runId) return; // 期间已切换运行，丢弃过期响应
      draftPrefill.value = JSON.stringify(data?.draft ?? {}, null, 2);
      draftText.value = draftPrefill.value;
      retryInstruction.value = data?.instruction || "";
    } catch (err) {
      if (run.value.id === runId) draftLoadError.value = errorText(err);
    } finally {
      if (run.value.id === runId) draftLoading.value = false;
    }
  },
  { immediate: true },
);

/* 带着修改重试：改过草稿才带 draft（展开看了没动 = 信任原种子，不带）；
 * 意见非空即带。按钮在 draftDirty 且解析失败时已禁用，这里再守一道。 */
async function retryWithEdits() {
  if (!canRetryWithEdits.value) return;
  retrying.value = true;
  retryError.value = "";
  try {
    await retryRun(run.value.stage, {
      ...(draftDirty.value ? { draft: draftParsed.value.value } : {}),
      ...(retryInstruction.value.trim() ? { instruction: retryInstruction.value.trim() } : {}),
    });
  } catch (err) {
    retryError.value = errorText(err);
  } finally {
    retrying.value = false;
  }
}
</script>

<template>
  <div class="failcard">
    <div class="fail-cap">阶段「{{ stageLabel(stage) || "未知" }}」未通过</div>
    <div v-if="findings.length" class="findings">
      <div v-for="(f, i) in findings" :key="i" class="finding">
        <span v-if="f?.gate" class="gate-tag">{{ f.gate }}</span>{{ f?.detail || f?.message || "" }}
      </div>
    </div>
    <div v-else class="tip">没有返回具体原因，可查看下方事件流。</div>
    <div v-if="retryError" class="err">{{ retryError }}</div>
    <div v-if="draftLoadError" class="err">草稿读取失败：{{ draftLoadError }}</div>

    <!-- 折叠草稿编辑：默认收起；展开改稿后才能「带着修改重试」，不动则盲重试按原样重跑 -->
    <div class="draftbox">
      <button class="draft-toggle" type="button" @click="draftOpen = !draftOpen">
        {{ draftOpen ? "收起草稿" : "查看/编辑草稿" }}
      </button>
      <div v-if="draftOpen" class="draft-body">
        <div v-if="draftLoading" class="tip">草稿读取中…</div>
        <template v-else>
          <textarea
            v-model="draftText"
            class="draft-in"
            rows="10"
            spellcheck="false"
            placeholder="该阶段交给 Agent 的种子草稿（JSON 对象）……"
          />
          <div v-if="draftDirty && !draftParsed.ok" class="err">
            草稿不是合法的 JSON 对象：{{ draftParsed.error }}——修正后才能带着修改重试。
          </div>
          <div v-else class="draft-hint">改动会作为该阶段种子交给 Agent；不动草稿则按原样重试。</div>
          <input
            v-model="retryInstruction"
            class="draft-opinion"
            type="text"
            placeholder="给重试的意见（可选）"
          />
          <div class="acts">
            <button class="btn" type="button" :disabled="!canRetryWithEdits" @click="retryWithEdits">
              {{ retrying ? "重试中…" : "带着修改重试" }}
            </button>
          </div>
        </template>
      </div>
    </div>

    <div class="acts">
      <button class="btn primary" type="button" :disabled="!run.stage || retrying" @click="retryFailedStage">
        {{ retrying ? "重试中…" : "重试本阶段" }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 失败卡红调（CreateRunPanel .failbox 同款） */
.failcard {
  border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent);
  border-radius: var(--r-2);
  padding: var(--sp-3) var(--sp-4);
  background: var(--card);
}
.failcard, .failcard * { box-sizing: border-box; }
.fail-cap { color: var(--danger); font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; }
.findings { margin-top: var(--sp-2); display: flex; flex-direction: column; gap: 4px; }
.finding { font-size: var(--fs-12); color: var(--text-2); line-height: 1.7; word-break: break-all; }
.gate-tag { display: inline-block; margin-right: 8px; padding: 0 8px; background: var(--card-2); border-radius: 99px; color: var(--warn); font-size: var(--fs-11); }
.tip { margin: 6px 0 0; font-size: var(--fs-12); color: var(--text-2); line-height: 1.8; }
.err { margin-top: var(--sp-2); color: var(--danger); font-size: var(--fs-12); line-height: 1.7; }

/* 草稿编辑器：折叠开关（弱化的文字按钮）+ JSON textarea + 意见单行框（同款） */
.draftbox { margin-top: var(--sp-3); }
.draft-toggle { padding: 0; background: none; border: none; color: var(--text-3); font-family: inherit; font-size: var(--fs-12); cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }
.draft-toggle:hover { color: var(--text-2); }
.draft-body { margin-top: var(--sp-2); display: flex; flex-direction: column; gap: var(--sp-2); }
.draft-in { width: 100%; min-height: 140px; resize: vertical; padding: 10px 12px; border: 1px solid var(--line-2); border-radius: var(--r-1); background: var(--bg); color: var(--text); font-family: var(--mono); font-size: var(--fs-12); line-height: 1.7; }
.draft-in:focus { outline: none; border-color: var(--accent); }
.draft-hint { font-size: var(--fs-11); color: var(--text-3); line-height: 1.8; }
.draft-opinion { width: 100%; padding: 7px 10px; border: 1px solid var(--line-2); border-radius: var(--r-1); background: var(--bg); color: var(--text); font-family: inherit; font-size: var(--fs-12); }
.draft-opinion:focus { outline: none; border-color: var(--accent); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 7px 16px; border-radius: var(--r-1); font-size: var(--fs-13); color: var(--text-2); cursor: pointer; background: none; border: 1px solid var(--line-2); font-family: inherit; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn:disabled { opacity: .5; pointer-events: none; }
.acts { display: flex; gap: var(--sp-2); margin-top: var(--sp-3); flex-wrap: wrap; }
</style>

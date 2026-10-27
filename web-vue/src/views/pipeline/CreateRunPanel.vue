<script setup>
/* 创作入口卡：粘贴小说 → startRun；运行现场在 RunChat（chat/RunChat.vue）。 */
import { computed, ref } from "vue";
import { errorText } from "../../api.js";
import { startRun, studio } from "../../studio/studioStore.js";

const novel = ref("");
const starting = ref(false);
const entryError = ref("");
const charCount = computed(() => novel.value.trim().length);
const canStart = computed(() => charCount.value >= 50 && Boolean(studio.canvas) && !starting.value);

async function begin() {
  if (!canStart.value) return;
  if (!studio.canvas) {
    entryError.value = "缺少项目标识，请从项目列表进入本页";
    return;
  }
  starting.value = true;
  entryError.value = "";
  try {
    await startRun(studio.canvas, novel.value.trim());
  } catch (err) {
    entryError.value = errorText(err);
  } finally {
    starting.value = false;
  }
}
</script>

<template>
  <section class="crp">
    <!-- ① 入口：仅在项目没有活动 run 时由 PipelineView 挂载（有 run 时挂 RunChat）；
         开始后小说文本成为对话流里第一条用户气泡 -->
    <div class="card">
      <div class="cap">从小说开始<span class="sub">粘贴正文 → Agent 逐阶段改编 → 大纲停下来等你确认</span></div>
      <textarea
        v-model="novel"
        class="novel"
        rows="7"
        placeholder="粘贴小说正文（全文，至少 50 字）……"
        :disabled="starting"
      />
      <div class="bar">
        <span class="cnt num" :class="{ ready: charCount >= 50 }">{{ charCount }} / 50 字</span>
        <span v-if="entryError" class="err">{{ entryError }}</span>
        <button class="btn primary go" type="button" :disabled="!canStart" @click="begin">
          {{ starting ? "正在创建运行…" : "开始创作" }}
        </button>
      </div>
      <p class="hint">开始后先由 Agent 产出大纲并过质量门，然后停下来等你确认，确认后才会继续写剧本（需 Postgres 后端）。</p>
    </div>
  </section>
</template>

<style scoped>
.crp { display: flex; flex-direction: column; gap: var(--sp-4); margin-bottom: var(--sp-5); }
.crp, .crp * { box-sizing: border-box; }
.card { border: 1px solid var(--line); border-radius: var(--r-2); background: var(--card); padding: var(--sp-3) var(--sp-4); }
.cap { font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; margin-bottom: var(--sp-2); }
.cap .sub { font-family: var(--sans); font-size: var(--fs-11); color: var(--text-3); font-weight: 400; margin-left: 12px; }

.novel { width: 100%; min-height: 120px; resize: vertical; padding: 10px 12px; border: 1px solid var(--line-2); border-radius: var(--r-1); background: var(--bg); color: var(--text); font-family: var(--sans); font-size: var(--fs-13); line-height: 1.8; }
.novel:focus { outline: none; border-color: var(--accent); }
.novel:disabled { opacity: .6; }
.bar { display: flex; align-items: center; gap: var(--sp-3); margin-top: var(--sp-2); }
.cnt { font-size: var(--fs-12); color: var(--text-3); }
.cnt.ready { color: var(--ok); }
.err { color: var(--danger); font-size: var(--fs-12); line-height: 1.7; }
.go { margin-left: auto; }
.hint { margin: var(--sp-2) 0 0; font-size: var(--fs-11); color: var(--text-3); line-height: 1.8; }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 7px 16px; border-radius: var(--r-1); font-size: var(--fs-13); color: var(--text-2); cursor: pointer; background: none; border: 1px solid var(--line-2); font-family: inherit; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn:disabled { opacity: .5; pointer-events: none; }
</style>

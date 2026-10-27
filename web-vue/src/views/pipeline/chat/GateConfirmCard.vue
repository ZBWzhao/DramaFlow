<script setup>
/* 确认门交互卡（红线，spec §2 gate 行）：gate 之后 UI 停在这张鎏金卡上等人工，
 * 放行只有「确认，开写剧本」一个入口，不提供任何跳过路径。
 * 模板 = CreateRunPanel gate 段（:280-303）+ gated 大纲区（:377-381）合成：
 * 大纲草案不再是悬浮卡，而是卡内嵌 ArtifactCard type="outline"（默认展开，滚动目标）。
 * 脚本逻辑（confirmApprove/recordFeedback/scrollToOutline）逐字迁自
 * CreateRunPanel :124-152、:241-243；run 归属判定由流容器负责，这里不重复。 */
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { errorText } from "../../../api.js";
import { approveRun, sendRunFeedback } from "../../../studio/studioStore.js";
import { STAGE_LABELS } from "./useRunFeed.js";
import ArtifactCard from "./ArtifactCard.vue";

const props = defineProps({ stage: { type: String, default: "" } });

const stageLabel = (s) => STAGE_LABELS[s] || s || "";

const approving = ref(false);
const gateError = ref("");
const feedback = ref("");
const sendingFeedback = ref(false);
const outlineEl = ref(null); // 「查看大纲并确认」的滚动目标

async function confirmApprove() {
  if (approving.value) return;
  approving.value = true;
  gateError.value = "";
  try {
    await approveRun();
    ElMessage.success("已确认，继续写剧本");
  } catch (err) {
    gateError.value = errorText(err);
  } finally {
    approving.value = false;
  }
}

async function recordFeedback() {
  const text = feedback.value.trim();
  if (!text || sendingFeedback.value) return;
  sendingFeedback.value = true;
  gateError.value = "";
  try {
    await sendRunFeedback(text);
    feedback.value = "";
    ElMessage.success("意见已随本次运行记录，后续改稿可见；不会触发自动重写，确认仍需你点击");
  } catch (err) {
    gateError.value = errorText(err);
  } finally {
    sendingFeedback.value = false;
  }
}

function scrollToOutline() {
  outlineEl.value?.scrollIntoView({ behavior: "smooth", block: "start" });
}
</script>

<template>
  <div class="gcc">
    <div class="gcc-cap">「{{ stageLabel(stage) }}」已通过质量门，等你确认</div>
    <p class="tip">确认后才会继续写剧本；确认门不可跳过，也不会自动放行。对大纲有意见可以先记录意见——意见随本次运行留档、供后续改稿参考，不会触发自动重写。</p>
    <div v-if="gateError" class="err">{{ gateError }}</div>
    <div class="acts">
      <button class="btn" type="button" @click="scrollToOutline">查看大纲并确认</button>
      <button class="btn primary" type="button" :disabled="approving" @click="confirmApprove">
        {{ approving ? "正在放行…" : "确认，开写剧本" }}
      </button>
    </div>
    <div class="fb">
      <input
        v-model="feedback"
        class="fb-in"
        type="text"
        placeholder="对大纲的意见，如：第一集节奏太慢、主角出场再提前……"
        :disabled="sendingFeedback"
        @keyup.enter="recordFeedback"
      />
      <button class="btn" type="button" :disabled="!feedback.trim() || sendingFeedback" @click="recordFeedback">
        {{ sendingFeedback ? "记录中…" : "记录意见" }}
      </button>
    </div>

    <!-- 卡内产物区：大纲草案（滚动目标 + 确认对象）；store 在 gate 事件时已 loadPipeline(true) -->
    <ArtifactCard ref="outlineEl" class="gcc-artifact" type="outline" :open="true" />
  </div>
</template>

<style scoped>
/* 确认门卡：鎏金高亮——整条流最醒目的一块，停在这里等人工（CreateRunPanel .gate 同款） */
.gcc {
  border: 1px solid color-mix(in srgb, var(--gold) 45%, transparent);
  background: var(--warn-soft);
  border-radius: var(--r-2);
  padding: var(--sp-3) var(--sp-4);
}
.gcc, .gcc * { box-sizing: border-box; }
.gcc-cap { font-family: var(--serif); font-size: var(--fs-14); font-weight: 600; }
.tip { margin: 6px 0 0; font-size: var(--fs-12); color: var(--text-2); line-height: 1.8; }
.err { color: var(--danger); font-size: var(--fs-12); line-height: 1.7; }
.acts { display: flex; gap: var(--sp-2); margin-top: var(--sp-3); flex-wrap: wrap; }
.fb { display: flex; gap: var(--sp-2); margin-top: var(--sp-2); }
.fb-in { flex: 1; min-width: 0; padding: 7px 10px; border: 1px solid var(--line-2); border-radius: var(--r-1); background: var(--bg); color: var(--text); font-family: inherit; font-size: var(--fs-12); }
.fb-in:focus { outline: none; border-color: var(--gold); }

.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 7px 16px; border-radius: var(--r-1); font-size: var(--fs-13); color: var(--text-2); cursor: pointer; background: none; border: 1px solid var(--line-2); font-family: inherit; }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn:disabled { opacity: .5; pointer-events: none; }

.gcc-artifact { margin-top: var(--sp-3); background: var(--card); }
</style>

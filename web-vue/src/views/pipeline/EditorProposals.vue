<script setup>
/* 改稿脑审批卡（阶段 2 Task 8）：提案 → 审批 → 自动体检 的"审批"一拍。
 *
 * 数据源：GET /api/pipeline/projects/{canvas}/editor/pending（提案流水，pending
 * 在前）。状态机与后端 core/pipeline_editor 对齐：
 *   pending（待审批，带 应用/驳回）→ applied（已落库；script 附体检结果）
 *   / rejected（已驳回）/ failed（已回滚 + findings）/ expired（超时失效）。
 * 红线对齐：这里的应用按钮是落稿的唯一入口（agent 工具环没有 apply）；
 * script 应用后服务端自动重跑质量门，门不过已自动回滚——卡片只如实展示结果。
 * 刷新时机：挂载 + 父组件转发的助手工具事件（propose_edit 必发 tool_execution_end）
 * + 本卡操作后；不轮询（TTL 过期态在下次刷新时呈现）。
 */
import { computed, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { errorText } from "../../api.js";
import {
  applyEditorProposal,
  fetchEditorProposals,
  rejectEditorProposal,
  studio,
} from "../../studio/studioStore.js";

const props = defineProps({
  /* 助手面板的刷新信号（AiPanel canvas-changed 在每次 tool_execution_end 后发出）：
   * 传计数器即可，数值变化触发重拉。 */
  pulse: { type: Number, default: 0 },
});

const edits = ref([]);
const loading = ref(false);
const loadError = ref("");
const actingId = ref("");       // 正在应用/驳回的 edit_id（按钮 loading + 防双击）
const actionError = ref("");    // 操作失败的可见错误（按卡片存）
const cardErrors = ref({});     // edit_id -> error string

const TYPE_LABELS = {
  outline: "大纲", script: "剧本", characters: "角色设定",
  art: "美术设定", staging: "开场站位", storyboard: "分镜",
};
const STATUS_LABELS = {
  pending: "待审批", applying: "应用中…", applied: "已应用",
  rejected: "已驳回", failed: "已回滚", expired: "已过期",
};
const typeLabel = (t) => TYPE_LABELS[t] || t || "产物";
const fmtTime = (ts) => {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "";
  const d = new Date(n * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

const pending = computed(() => edits.value.filter((e) => e.status === "pending"));
const settled = computed(() => edits.value.filter((e) => e.status !== "pending").slice(0, 8));

async function refresh() {
  if (!studio.canvas) return;
  loading.value = true;
  loadError.value = "";
  try {
    const data = await fetchEditorProposals();
    edits.value = data.edits || [];
  } catch (err) {
    loadError.value = errorText(err);
  } finally {
    loading.value = false;
  }
}

async function apply(edit) {
  if (actingId.value) return;
  actingId.value = edit.edit_id;
  cardErrors.value = { ...cardErrors.value, [edit.edit_id]: "" };
  try {
    const res = await applyEditorProposal(edit.edit_id);
    if (res.ok) {
      ElMessage.success(
        `已落库「${typeLabel(edit.artifact_type)}」`
        + (res.gate ? ` · 质量门${res.gate.ok ? "通过" : "未过"}` : ""),
      );
    } else {
      ElMessage.warning(`「${typeLabel(edit.artifact_type)}」未落库${res.rolled_back ? "，已回滚" : ""}`);
    }
  } catch (err) {
    cardErrors.value = { ...cardErrors.value, [edit.edit_id]: errorText(err) };
  } finally {
    actingId.value = "";
    await refresh();
  }
}

async function reject(edit) {
  if (actingId.value) return;
  actingId.value = edit.edit_id;
  cardErrors.value = { ...cardErrors.value, [edit.edit_id]: "" };
  try {
    await rejectEditorProposal(edit.edit_id);
  } catch (err) {
    cardErrors.value = { ...cardErrors.value, [edit.edit_id]: errorText(err) };
  } finally {
    actingId.value = "";
    await refresh();
  }
}

/* 父组件侧的刷新信号（助手 propose 后 pulse 自增） */
watch(() => props.pulse, () => void refresh());
defineExpose({ refresh });
onMounted(refresh);
</script>

<template>
  <section class="ep-card">
    <header class="cap">
      改稿提案
      <button class="mini" type="button" :disabled="loading" @click="refresh">
        {{ loading ? "…" : "刷新" }}
      </button>
    </header>

    <div v-if="loadError" class="err">{{ loadError }}</div>
    <div v-else-if="!edits.length" class="empty">还没有改稿提案——对话里让助手改稿后，这里会出现待审批的提案。</div>

    <template v-else>
      <!-- 待审批：审批卡（落稿的唯一入口） -->
      <div v-for="e in pending" :key="e.edit_id" class="proposal pending">
        <div class="row">
          <span class="chip">{{ typeLabel(e.artifact_type) }}</span>
          <b class="st">待审批</b>
          <span class="time num">{{ fmtTime(e.proposed_at) }}</span>
        </div>
        <p class="summary">{{ e.summary }}</p>
        <p class="meta num">{{ e.content_chars }} 字节 · {{ e.preview }}</p>
        <div v-if="cardErrors[e.edit_id]" class="err">{{ cardErrors[e.edit_id] }}</div>
        <div class="acts">
          <button class="btn ghost" type="button" :disabled="Boolean(actingId)" @click="reject(e)">
            驳回
          </button>
          <button class="btn primary" type="button" :disabled="Boolean(actingId)" @click="apply(e)">
            {{ actingId === e.edit_id ? "应用中…" : "应用（落稿 + 体检）" }}
          </button>
        </div>
      </div>

      <!-- 已终结：结果流水（只读） -->
      <div
        v-for="e in settled"
        :key="e.edit_id"
        class="proposal"
        :class="e.status"
      >
        <div class="row">
          <span class="chip">{{ typeLabel(e.artifact_type) }}</span>
          <b class="st" :class="e.status">{{ STATUS_LABELS[e.status] || e.status }}</b>
          <span class="time num">{{ fmtTime(e.proposed_at) }}</span>
        </div>
        <p class="summary">{{ e.summary }}</p>
        <p v-if="e.status === 'failed' && e.findings?.length" class="findings num">
          {{ String(e.findings[0]?.detail || e.findings[0]?.message || "").slice(0, 220) }}
        </p>
        <p v-if="cardErrors[e.edit_id]" class="err">{{ cardErrors[e.edit_id] }}</p>
      </div>
    </template>
  </section>
</template>

<style scoped>
.ep-card { border-bottom: 1px solid var(--line); padding: var(--sp-3) var(--sp-4); background: var(--bg-2); }
.ep-card, .ep-card * { box-sizing: border-box; }
.cap { display: flex; align-items: center; gap: 8px; font-size: 10px; letter-spacing: .14em; color: var(--text-3); padding-bottom: 8px; }
.mini { margin-left: auto; border: none; background: none; color: var(--text-3); font-size: var(--fs-11); cursor: pointer; padding: 2px 6px; border-radius: var(--r-1); }
.mini:hover { color: var(--text); background: var(--card-2); }
.empty { color: var(--text-3); font-size: var(--fs-12); line-height: 1.7; padding: 4px 0 2px; }
.err { color: var(--danger); font-size: var(--fs-12); margin-top: 4px; word-break: break-all; }

.proposal { border: 1px solid var(--line-2); border-radius: var(--r-2); padding: 8px 10px; margin-bottom: 8px; background: var(--card); }
.proposal.pending { border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
.row { display: flex; align-items: center; gap: 8px; }
.chip { font-size: var(--fs-11); color: var(--accent); border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent); border-radius: 99px; padding: 0 8px; line-height: 18px; }
.st { font-size: var(--fs-12); color: var(--text-2); }
.st.applied { color: var(--ok); }
.st.failed { color: var(--danger); }
.st.rejected, .st.expired { color: var(--text-3); }
.time { margin-left: auto; font-size: var(--fs-11); color: var(--text-3); }
.summary { margin: 6px 0 0; font-size: var(--fs-13); color: var(--text); line-height: 1.6; }
.meta { margin: 4px 0 0; font-size: var(--fs-11); color: var(--text-3); overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.findings { margin: 6px 0 0; font-size: var(--fs-11); color: var(--danger); word-break: break-all; }
.acts { display: flex; gap: 8px; justify-content: flex-end; margin-top: 8px; }

.btn { border: 1px solid var(--line-2); background: none; border-radius: var(--r-1); font-size: var(--fs-12); padding: 4px 12px; cursor: pointer; color: var(--text-2); }
.btn:hover { color: var(--text); background: var(--card-2); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #FFF7F0; }
.btn.primary:hover { background: var(--accent-hi); }
.btn:disabled { opacity: .5; pointer-events: none; }
</style>

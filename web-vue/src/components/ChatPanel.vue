<script setup>
/* 对话页：按事件流的**发生顺序**渲染一条 feed。
 *
 * 运行期间的三处"活着"信号（这轮补的，之前只靠一个静止的"正在生成…"）：
 *   · 输入框上方状态条：实时计时 + 当前活动 + "已 Ns 无新事件"告警
 *   · 消息内常驻活动行（AssistantMessage 里）
 *   · 连第一条回复都还没到时，也有一行占位（否则用户发完就只看到自己的消息）
 */
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { actions, state } from "../store.js";
import AssistantMessage from "./AssistantMessage.vue";
import QuestionCard from "./QuestionCard.vue";
import ProductionProgress from "./ProductionProgress.vue";

const draft = ref("");
const streamEl = ref(null);
const stickBottom = ref(true);

const modeOptions = computed(() => [
  { value: "agent", label: "普通 Agent" },
  ...state.teams.map((t) => ({ value: `team:${t.name}`, label: `团队 · ${t.display_name || t.name}` })),
]);

const policyOptions = [
  { value: "ask", label: "逐步询问后审批" },
  { value: "auto_review", label: "自动准备后审批" },
  { value: "plan_only", label: "仅策划不生成" },
];
const currentProvider = computed(() => state.mediaProviders.find((item) => item.id === state.mediaProvider));
const modelOptions = computed(() => currentProvider.value?.models || []);
const providerLabel = computed(() => currentProvider.value?.label || state.mediaProvider);
const sessionLabel = (item) => `${item.title}${item.messages ? ` · ${item.messages} 条` : ""}`;

async function onSessionChange(sessionId) {
  try {
    await actions.openSession(sessionId);
  } catch (err) {
    ElMessage.error(`打开会话失败：${err.message || err}`);
  }
}

async function onNewSession() {
  try {
    await actions.createSession();
    ElMessage.success("已创建新会话");
  } catch (err) {
    ElMessage.error(`创建会话失败：${err.message || err}`);
  }
}

async function onRenameSession() {
  if (!state.sessionId) return;
  try {
    const { value } = await ElMessageBox.prompt("输入新的会话名称", "重命名会话", {
      inputValue: state.sessions.find((item) => item.id === state.sessionId)?.title || "",
      inputPattern: /\S+/,
      inputErrorMessage: "名称不能为空",
      confirmButtonText: "保存",
      cancelButtonText: "取消",
    });
    await actions.renameSession(value);
    ElMessage.success("会话已重命名");
  } catch (err) {
    if (err !== "cancel" && err !== "close") ElMessage.error(`重命名失败：${err.message || err}`);
  }
}

async function onDeleteSession() {
  if (!state.sessionId) return;
  try {
    await ElMessageBox.confirm("该会话及其子 Agent 记录会被永久删除，确定继续吗？", "删除会话", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
      confirmButtonClass: "el-button--danger",
    });
    await actions.deleteSession();
    ElMessage.success("会话已删除");
  } catch (err) {
    if (err !== "cancel" && err !== "close") ElMessage.error(`删除失败：${err.message || err}`);
  }
}

async function deleteMessage(item) {
  try {
    await ElMessageBox.confirm("将从这条消息开始隐藏后续内容，历史分支仍保留在会话文件中。", "删除消息及后续", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    await actions.mutateMessage(item.entryId, "delete");
  } catch (err) {
    if (err !== "cancel" && err !== "close") ElMessage.error(`删除消息失败：${err.message || err}`);
  }
}

async function resendMessage(item) {
  try {
    await actions.mutateMessage(item.entryId, "resend");
  } catch (err) {
    ElMessage.error(`重发失败：${err.message || err}`);
  }
}

async function switchBranch(item, offset) {
  const index = Number(item.branchIndex || 0) + offset;
  const target = item.branches?.[index];
  if (!target) return;
  try {
    await actions.mutateMessage(target, "branch");
  } catch (err) {
    ElMessage.error(`切换分支失败：${err.message || err}`);
  }
}

async function approveGeneration() {
  try {
    const res = await actions.approveGeneration();
    ElMessage.success(`已批准并完成 ${res.done}/${res.total} 个生成任务`);
  } catch (err) {
    ElMessage.error(`生成失败：${err.message}`);
  }
}

const liveAssistant = computed(() => {
  for (let i = state.feed.length - 1; i >= 0; i -= 1) {
    const item = state.feed[i];
    if (item.type === "assistant" && item.streaming) return item;
  }
  return null;
});

/** 已经开始跑，但还没有任何 assistant 消息可承载活动提示 → 自己起一行。 */
const showLiveRow = computed(() => state.running && !liveAssistant.value);

const elapsed = computed(() =>
  state.running && state.runStartedAt ? (state.nowTick - state.runStartedAt) / 1000 : 0
);

/** 心跳每 5s 一次；12s 没有任何事件就该提醒用户（可能卡住或断线）。 */
const silent = computed(() => {
  if (!state.running || !state.lastEventAt) return 0;
  const gap = (state.nowTick - state.lastEventAt) / 1000;
  return gap >= 12 ? Math.round(gap) : 0;
});

const statusText = computed(() => {
  if (state.running) return `${elapsed.value.toFixed(1)}s · ${state.activity || "处理中…"}`;
  if (state.endReason === "limited") return "已暂停，进度已保存";
  if (state.endReason === "waiting_approval") return "创作包已验收，等待生成审批";
  if (state.endReason === "unknown") return "上次运行未正常结束";
  if (state.status === "error") return "上次运行出错";
  return "空闲";
});

const usageText = computed(() => {
  const usage = state.usage;
  if (!usage) return "";
  const cached = usage.cached ? ` · 缓存命中 ${usage.cached}` : "";
  return `输入 ${usage.input} · 输出 ${usage.output}${cached} · 合计 ${usage.total}`;
});

const lastActivity = computed(() => {
  const last = state.feed[state.feed.length - 1];
  if (!last) return 0;
  if (last.type !== "assistant") return last.type === "user" ? 1 : 0;
  return last.parts.reduce((sum, part) => sum + (part.text?.length || 0) + (part.status?.length || 0), 0);
});

/* 计时器：只在运行期间走，驱动所有"实时秒数" */
let ticker = null;
function stopTicker() {
  if (ticker) clearInterval(ticker);
  ticker = null;
}
watch(
  () => state.running,
  (running) => {
    state.nowTick = Date.now();
    stopTicker();
    if (running) ticker = setInterval(() => (state.nowTick = Date.now()), 200);
  },
  { immediate: true }
);
onUnmounted(stopTicker);

function onScroll() {
  const el = streamEl.value;
  if (!el) return;
  stickBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
}

function scrollToBottom(force = false) {
  nextTick(() => {
    const el = streamEl.value;
    if (el && (force || stickBottom.value)) el.scrollTop = el.scrollHeight;
  });
}

async function onSend() {
  const text = draft.value.trim();
  if (!text) return;
  if (state.running && state.mode !== "agent") {
    ElMessage.warning("团队运行暂不支持追加要求，可停止后重新发送");
    return;
  }
  draft.value = "";
  stickBottom.value = true;
  try {
    await actions.send(text);
  } catch (err) {
    draft.value = text;
    ElMessage.error(`发送失败：${err.message || err}`);
  }
}

async function onContinue() {
  if (state.productionRun?.status === "limited") {
    try {
      await actions.resumeProduction();
    } catch (err) {
      ElMessage.error(`继续制作失败：${err.message || err}`);
    }
    return;
  }
  const instruction = state.resumeInstruction
    || "继续当前任务：先读取当前画布和已有产物，核对未完成项；不要重复创建已有节点，只补齐缺失内容并最终给出交付总结。";
  stickBottom.value = true;
  try {
    await actions.send(instruction);
  } catch (err) {
    ElMessage.error(`继续制作失败：${err.message || err}`);
  }
}

function onKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    onSend();
  }
}

watch(() => state.feed.length, () => scrollToBottom(true));
watch(lastActivity, () => scrollToBottom());
</script>

<template>
  <div class="chat-view">
    <div class="session-strip">
      <span class="session-caption">对话记录</span>
      <el-select
        :model-value="state.sessionId"
        :loading="state.sessionLoading"
        :disabled="state.running"
        placeholder="新会话（发送后自动保存）"
        size="small"
        aria-label="当前对话会话"
        @update:model-value="onSessionChange"
      >
        <el-option
          v-for="session in state.sessions"
          :key="session.id"
          :label="sessionLabel(session)"
          :value="session.id"
        />
      </el-select>
      <el-button
        circle
        size="small"
        :disabled="state.running"
        title="新建会话"
        aria-label="新建会话"
        @click="onNewSession"
      >
        <el-icon><Plus /></el-icon>
      </el-button>
      <el-button
        circle
        size="small"
        :disabled="state.running || !state.sessionId"
        title="重命名会话"
        aria-label="重命名会话"
        @click="onRenameSession"
      >
        <el-icon><Edit /></el-icon>
      </el-button>
      <el-button
        circle
        size="small"
        type="danger"
        plain
        :disabled="state.running || !state.sessionId"
        title="删除会话"
        aria-label="删除会话"
        @click="onDeleteSession"
      >
        <el-icon><Delete /></el-icon>
      </el-button>
      <span v-if="state.stats?.context_percent != null" class="context-usage" :class="{ warn: state.stats.context_percent >= 70 }">
        上下文 {{ state.stats.context_percent }}%
      </span>
    </div>
    <div ref="streamEl" class="chat-stream" @scroll="onScroll">
      <ProductionProgress />
      <div v-if="!state.feed.length" class="chat-empty">
        <h2>做一集短剧</h2>
        <p>“把『外卖员深夜送单，发现店员是失散多年的姐姐』做成 60 秒短剧生产图”</p>
        <p class="dim">或：“核对分镜与提示词的时长口径，并修正不一致”</p>
      </div>

      <template v-for="(item, index) in state.feed" :key="index">
        <div v-if="item.type === 'user'" class="msg user">
          <div class="who">你</div>
          <div class="bubble">{{ item.text }}</div>
          <div v-if="item.entryId && !state.running" class="message-actions">
            <template v-if="item.branches?.length > 1">
              <el-button link size="small" :disabled="item.branchIndex <= 0" @click="switchBranch(item, -1)">‹</el-button>
              <span class="dim mono">{{ item.branchIndex + 1 }}/{{ item.branches.length }}</span>
              <el-button link size="small" :disabled="item.branchIndex >= item.branches.length - 1" @click="switchBranch(item, 1)">›</el-button>
            </template>
            <el-button link size="small" @click="resendMessage(item)">重发</el-button>
            <el-button link size="small" type="danger" @click="deleteMessage(item)">删除</el-button>
          </div>
        </div>

        <AssistantMessage v-else-if="item.type === 'assistant'" :item="item" />

        <QuestionCard v-else-if="item.type === 'question'" :item="item" />

        <div v-else-if="item.type === 'note'" class="part part-note">
          <b>{{ item.title }}</b>
          <span class="dim">{{ item.text }}</span>
        </div>
      </template>

      <div v-if="state.generationReview" class="generation-review">
        <div class="review-head">
          <b>生成前需要你拍板</b>
          <span :class="currentProvider?.mock ? 'mock-pill' : 'cost-pill'">
            {{ currentProvider?.mock ? "Mock · 不扣费" : "真实 Provider · 可能产生费用" }}
          </span>
        </div>
        <p>
          画布 {{ state.generationReview.canvas }} 有
          <b>{{ state.generationReview.ready }}/{{ state.generationReview.total }}</b>
          个节点可生成，将使用 {{ providerLabel }} / {{ state.mediaModel }}。
        </p>
        <div class="review-actions">
          <el-button size="small" @click="actions.dismissGenerationReview()">暂不生成</el-button>
          <el-button size="small" type="primary" :loading="state.canvasBusy" @click="approveGeneration">
            批准并开始生成
          </el-button>
        </div>
      </div>

      <!-- 还没收到任何事件时的占位：发完消息不该只有一句提问留在屏幕上 -->
      <div v-if="showLiveRow" class="msg assistant">
        <div class="msg-head">
          <span class="who">Agent</span>
          <span class="live-timer"><i class="pulse" />{{ state.activity || "连接中…" }} · {{ elapsed.toFixed(1) }}s</span>
        </div>
        <div class="msg-body">
          <div class="waiting">
            <el-icon class="spin"><Loading /></el-icon>
            <span>{{ state.activity || "正在处理…" }}</span>
            <span v-if="silent" class="silent">已 {{ silent }}s 无新事件</span>
          </div>
        </div>
      </div>
    </div>

    <footer class="composer">
      <div class="composer-card">
        <el-input
          v-model="draft"
          class="composer-input"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 6 }"
          resize="none"
          :placeholder="state.running ? '补充要求会加入 Follow-up 队列…' : '输入消息，描述剧情或让 Agent 修改画布…'"
          @keydown="onKeydown"
        />
        <div class="composer-bar">
          <el-popover placement="top-start" :width="300" trigger="click">
            <template #reference>
              <el-button circle size="small" title="Agent 与媒体生成设置">
                <el-icon><Setting /></el-icon>
              </el-button>
            </template>
            <div class="agent-settings-popover">
              <b>创作与生成</b>
              <label>协作方式</label>
              <el-select
                :model-value="state.mediaPolicy"
                size="small"
                @update:model-value="actions.setMediaPolicy"
              >
                <el-option v-for="opt in policyOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
              </el-select>
              <label>媒体 Provider</label>
              <el-select
                :model-value="state.mediaProvider"
                size="small"
                @update:model-value="actions.setMediaProvider"
              >
                <el-option
                  v-for="provider in state.mediaProviders"
                  :key="provider.id"
                  :label="provider.configured ? provider.label : `${provider.label}（未配置）`"
                  :value="provider.id"
                  :disabled="!provider.configured"
                />
              </el-select>
              <label>生成模型</label>
              <el-select
                :model-value="state.mediaModel"
                size="small"
                @update:model-value="actions.setMediaModel"
              >
                <el-option
                  v-for="model in modelOptions"
                  :key="model.id"
                  :label="`${model.label || model.id} · ${model.kind}`"
                  :value="model.id"
                />
              </el-select>
              <p>真实媒体生成仍会在执行前请求你的确认。</p>
            </div>
          </el-popover>
          <el-select v-model="state.mode" size="small" class="agent-mode-select">
            <el-option v-for="opt in modeOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <span class="composer-status" :class="{ running: state.running, warn: silent > 0 || state.endReason === 'limited' }">
            <i v-if="state.running" class="pulse" />
            {{ statusText }}
          </span>
          <span v-if="usageText" class="dim mono usage-strip">{{ usageText }}</span>
          <span style="flex: 1" />
          <el-button v-if="state.endReason === 'limited' && !state.running" size="small" type="warning" plain @click="onContinue">
            继续制作
          </el-button>
          <el-button v-if="state.feed.length && !state.running" link size="small" @click="actions.clearFeed()">
            清屏
          </el-button>
          <el-button size="small" :disabled="!state.running" @click="actions.stop()">停止</el-button>
          <el-button circle size="small" type="primary" :disabled="!draft.trim() || (state.running && state.mode !== 'agent')" :title="state.running ? '加入 Follow-up' : '发送'" @click="onSend">
            <el-icon><Promotion /></el-icon>
          </el-button>
        </div>
      </div>
    </footer>
  </div>
</template>

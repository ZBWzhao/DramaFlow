# 管线对话流实施计划：RunChat（微信式气泡 × 站内风格）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 制片台运行过程改为正序对话流：用户小说气泡 + 阶段消息（工具折叠卡/门行/结束章）+ 确认门/失败/完成交互卡 + 产物卡内联；四页签与后端零改动。

**Architecture:** 纯前端。新组件全部落 `web-vue/src/views/pipeline/chat/`，props 驱动、只读消费 `studioStore`（run.events 正序分组是纯函数 `buildFeed`）；交互卡从 `CreateRunPanel.vue` 逐字迁移（确认门文案与唯一入口不动）；`GateMarkers` 加 blocking 变体；studioStore 仅三处 additive 小改（novel 字段、stage_done degraded 键、DEV 调试暴露）。失败免疫计划（`2026-10-26-failure-immunity.md`）先行落地，其 additive 事件键（degraded/blocking/fallback）在本计划消费。

**Tech Stack:** Vue3 `<script setup>` + 站内 CSS 变量；零新依赖。

**Spec:** `docs/superpowers/specs/2026-10-26-pipeline-chat-feed-design.md`（用户已批，含微信气泡规格）

## Global Constraints

- 禁改：`store.js`、`canvas2/`、`ai/`、`components/ChatPanel.vue` 及其子件（AssistantMessage/ToolPart/ThinkingPart/SubAgentPart/QuestionCard/MarkdownText）——只复刻视觉模式
- 禁改：四页签面板（CastPanel/ArtPanel/StagingPanel/StoryboardPanel）、`PipelineView.vue` 页签区与运行详情抽屉、后端全部
- studioStore 只允许本计划 Task 2 列出的三处 additive 修改，其余零触碰；事件账本结构（append-only 原始帧）不动，消息列表是纯 computed 派生
- 确认门红线：放行唯一入口「确认，开写剧本」，文案与行为从 CreateRunPanel 逐字保留；意见记录不触发自动重写的提示语原样
- 气泡规格：头像 36px、气泡贴头像 10px、`padding: 9px 13px`、圆角 10px（贴头像侧 4px）、`max-width: 68%`、`--fs-13`/`line-height: 1.65`；用户气泡 `color-mix(in srgb, var(--accent) 12%, var(--card))`、Agent 气泡 `var(--card)` + `1px var(--line)`；不引入站外色
- 事件帧无时间戳：v1 不渲染时间戳（spec §3 允许）
- 未知事件类型渲染灰气泡 + JSON 截 160 字，整流不许崩（describe 全防御取值口径照 CreateRunPanel）
- 每任务 `cd web-vue && npm run build` 零错误；Commits lowercase conventional prefix

---

### Task 1: 分组器 + 阶段消息原子件（不接线）

**Files:**
- Create: `web-vue/src/views/pipeline/chat/useRunFeed.js`
- Create: `web-vue/src/views/pipeline/chat/ChatAvatar.vue`、`SystemNote.vue`、`ToolCard.vue`、`GateRow.vue`、`UserNovelBubble.vue`、`StageMessage.vue`
- Test: 无（build + Task 4 CDP）

**Interfaces:**
- Produces: `buildFeed(events, novel) -> list`，item kind ∈ `user / system / stage / gate-card / fail-card / done-card / unknown`；stage item = `{kind:"stage", stage, label, status:"running"|"done"|"failed", degraded:bool, blocking:int, parts:[{kind:"tool",name,argsSummary,preview,done} | {kind:"gate",ok,round,advisory,fallback}]}`；`STAGE_LABELS` 导出。全部组件 props 驱动：ChatAvatar `{who:"user"|"bot"}`；SystemNote `{text}`；ToolCard `{part}`；GateRow `{row}`；UserNovelBubble `{text}`；StageMessage `{item, nowTick, since}`（since=本会话观察到该阶段首帧的时刻 ms，运行中计时用）

- [ ] **Step 1: useRunFeed.js** 全文：

```js
/* run.events（append-only 原始帧）→ 对话流消息列表。纯函数，RunChat computed 调用；
 * 正序输出（旧运行卡的倒序口径废弃）。分组规则见 spec §2：
 * stage 开新消息并聚合后续 tool/tool_result/stage_gate 直到 stage_done；gate/failed/
 * done 是流内交互卡；hello/retry 是居中系统行；未知类型灰气泡，绝不让整流崩掉。 */
export const STAGE_LABELS = {
  outline: "大纲", script: "剧本", characters: "角色设定",
  art: "美术设定", staging: "开场站位", storyboard: "分镜",
};

export function buildFeed(events, novel) {
  const feed = [];
  if (novel) feed.push({ kind: "user", text: String(novel) });
  let stage = null;
  const closeStage = () => { if (stage) { feed.push(stage); stage = null; } };
  for (const ev of Array.isArray(events) ? events : []) {
    const t = ev?.type;
    if (t === "hello") {
      closeStage();
      feed.push({ kind: "system", text: `已接入 · 状态「${ev.status || "?"}」` });
    } else if (t === "stage") {
      closeStage();
      stage = { kind: "stage", stage: ev.stage, label: STAGE_LABELS[ev.stage] || ev.stage || "?",
                status: "running", degraded: false, blocking: 0, parts: [] };
    } else if (t === "tool") {
      if (stage) stage.parts.push({ kind: "tool", name: ev.name || "工具",
        argsSummary: String(ev.args_summary || "").slice(0, 110), preview: "", done: false });
    } else if (t === "tool_result") {
      const card = stage
        ? [...stage.parts].reverse().find((p) => p.kind === "tool" && !p.done)
        : null;
      if (card) { card.preview = String(ev.preview || ""); card.done = true; }
      else if (stage) stage.parts.push({ kind: "tool", name: ev.name || "工具",
        argsSummary: "", preview: String(ev.preview || ""), done: true });
    } else if (t === "stage_gate") {
      if (stage) stage.parts.push({ kind: "gate", ok: Boolean(ev.ok), round: ev.round,
        advisory: Number(ev.advisory) || 0, fallback: Boolean(ev.fallback) });
    } else if (t === "stage_done") {
      if (stage) {
        stage.status = ev.ok ? "done" : "failed";
        stage.degraded = Boolean(ev.degraded);
        stage.blocking = Number(ev.blocking) || 0;
      }
      closeStage();
    } else if (t === "gate") {
      closeStage();
      feed.push({ kind: "gate-card", stage: ev.stage });
    } else if (t === "retry") {
      closeStage();
      feed.push({ kind: "system", text: `重试「${STAGE_LABELS[ev.stage] || ev.stage || ""}」` });
    } else if (t === "failed") {
      closeStage();
      feed.push({ kind: "fail-card", stage: ev.stage,
        findings: Array.isArray(ev.findings) ? ev.findings : [] });
    } else if (t === "done") {
      closeStage();
      feed.push({ kind: "done-card" });
    } else {
      closeStage();
      feed.push({ kind: "unknown", raw: JSON.stringify(ev)?.slice(0, 160) || "" });
    }
  }
  if (stage) feed.push(stage);
  return feed;
}
```

- [ ] **Step 2: 原子件**——ChatAvatar（36px 圆：`user`= accent 底白字「你」靠右、`bot`= `--card-2` 底 + Tools 图标靠左）；SystemNote（居中 `--fs-11` `--text-3`）；GateRow（一行状态：未过 `第 {round} 轮质量门：未过` 红 / 过 `第 {round} 轮质量门：通过` 绿 / advisory>0 `…：通过 · {advisory} 条内容标记` 鎏金色（`--gold`）/ `fallback` 时行尾加 `（兜底门）` 灰字）；ToolCard（复刻 ToolPart 视觉：折叠头 = ▸ 旋转 + 图标 + `{name}` + argsSummary + `已完成/调用中`；展开 = preview `<pre>` + 复制按钮——pipeline 事件只有摘要/预览，没有全量参数，展开区标题用「回执」；`part.done=false` 显示旋转 Loading）；UserNovelBubble（右侧气泡：`{{ text }}`，默认 `display:-webkit-box; -webkit-line-clamp:6` 折叠 + 「展开全文/收起」文字开关）
- [ ] **Step 3: StageMessage.vue**——头部 = ChatAvatar(bot) + `{label}` + 状态徽（运行中：`<i class="pulse" /> 运行中 {((nowTick-since)/1000).toFixed(1)}s`；done：绿 ✓；failed：红 ✗；degraded：黄 ⛔「产物已落库 · {blocking} 项阻断标记」）；主体 = parts 逐个 ToolCard/GateRow；`max-width: 68%` 气泡内滚动不需要（回执 preview 已截断）
- [ ] **Step 4: build + 提交**

```bash
cd web-vue && npm run build
cd .. && git add web-vue/src/views/pipeline/chat/
git commit -m "feat: run feed grouping and stage message atoms"
```

---

### Task 2: 交互卡 + 产物卡 + GateMarkers ⛔ 变体 + store 三处增补（不接线）

**Files:**
- Modify: `web-vue/src/studio/studioStore.js`（仅三处，见下）
- Modify: `web-vue/src/views/pipeline/GateMarkers.vue`（⛔ 变体）
- Create: `web-vue/src/views/pipeline/chat/GateConfirmCard.vue`、`FailCard.vue`、`DoneCard.vue`、`ArtifactCard.vue`
- Test: 无（build + Task 4 CDP）

**Interfaces:**
- Produces: GateConfirmCard `{stage}`；FailCard `{stage, findings}`；DoneCard 无 props；ArtifactCard `{type, open}`（open 默认 false；outline 确认卡传 true）。store：`studio.run.novel`、`stages[s].degraded/.blocking`、DEV 下 `window.__studio = studio`

- [ ] **Step 1: store 三处增补**
  1. run 初始形状（~:53）加 `novel: ""`；`startRun`（~:708）置 `novel: novel`；`_reattachRun`（~:693）与恢复语义一致置 `novel: ""`（历史正文不回放 → 无用户气泡）
  2. `stage_done` 分支（~:759）末尾加：

```js
      // 失败免疫：兜底通过的阶段带 degraded/blocking（可选键，缺省不影响既有路径）
      if (msg.degraded) { s.degraded = true; s.blocking = Number(msg.blocking) || 0; }
```

  3. 文件尾加 DEV 调试暴露（CDP 验收与未来排障用，生产构建剔除）：

```js
if (import.meta.env.DEV) window.__studio = studio;
```

- [ ] **Step 2: GateMarkers ⛔ 变体**——script 增：

```js
const blockingCount = computed(() => items.value.filter((f) => f?.severity === "blocking").length);
```

  折叠头文案改为三元：有 blocking → `⛔ {{ items.length }} 条内容标记（{{ blockingCount }} 条阻断 · 请人工复核或重跑本阶段）` 且根元素加 `class="gm blocking"`（`.gm.blocking .fold-head { color: var(--danger); }`，条目 rule 标签同变红）；无 blocking → 现有 ⚠️ 文案逐字不变
- [ ] **Step 3: ArtifactCard.vue**——props `{type, open}`；内部 `openRef = ref(props.open)`；头部行 = 「{label} · 已落库」+ 摘要 + ▸ 开关；摘要函数：characters → `{content.characters?.length ?? 0} 个角色`、art → `{Object.keys(content?.images || {}).length || content?.art?.length || 0} 项设定`（防御取值，取不到就显示「详情展开查看」）、staging/storyboard 同口径（数组长度或集数）；内容区：characters/art/staging/storyboard → 对应四面板 `<CastPanel :content="content" :findings="findings" />` 等（import 自 `../CastPanel.vue` 同级）；outline → `<PipelineOutlinePanel :outline="content" />`（`../../script/PipelineOutlinePanel.vue`）+ `<GateMarkers :findings="findings" />`；面板缺 content → 「产物加载中，请稍候……若长时间为空请刷新页面。」（迁 CreateRunPanel :380 文案）。detail 取自 `studio.pipeline?.detail?.[type]`（store 单例直读，与 CreateRunPanel 同模式：`import { studio } from "../../../studio/studioStore.js"`）
- [ ] **Step 4: GateConfirmCard.vue**——迁移 CreateRunPanel :280-303 gate 段 + :377-381 大纲区，合成为一张鎏金卡：标题「「{label}」已通过质量门，等你确认」+ tip 原文 + 错误行 + 动作行（「查看大纲并确认」滚动到卡内产物区、`确认，开写剧本` 唯一放行）+ 意见输入行；卡内产物区 = `<ArtifactCard type="outline" :open="true" ref="outlineEl" />`；脚本逻辑（approving/gateError/feedback/confirmApprove/recordFeedback/scrollToOutline）从 CreateRunPanel :124-152、:241-243 逐字迁移，`run` 改从 store 直读
- [ ] **Step 5: FailCard.vue**——迁移 CreateRunPanel :306-356 failbox + :168-239 草稿编辑器逻辑，props `{stage, findings}` 供事件帧 findings，其余（run、retryRun、drafts GET、retryWithEdits）store 直读逐字迁移；文案原样（含「带着修改重试」「盲重试按原样重跑」）
- [ ] **Step 6: DoneCard.vue**——绿卡「本轮运行完成，产物已导入，可在下方各页签查看。」；degraded 派生：`const degradedStages = Object.entries(studio.run.stages).filter(([, s]) => s.degraded)` 非空 → 黄卡「本轮运行完成 · {n} 个阶段带 ⛔ 阻断标记（产物卡内有明细）」
- [ ] **Step 7: build + 提交**

```bash
cd web-vue && npm run build
cd .. && git add web-vue/src/studio/studioStore.js web-vue/src/views/pipeline/
git commit -m "feat: run chat cards — confirm gate, failure, done, artifact; blocking marker variant"
```

---

### Task 3: RunChat 容器 + 接线（运行卡退役）

**Files:**
- Create: `web-vue/src/views/pipeline/chat/RunChat.vue`
- Modify: `web-vue/src/views/PipelineView.vue`（挂载替换 ~:181）
- Modify: `web-vue/src/views/pipeline/CreateRunPanel.vue`（②③ 退役，留入口卡）

**Interfaces:**
- Consumes: Task 1 全部原子件 + Task 2 全部卡片；`buildFeed(events, studio.run.novel)`
- Produces: RunChat 无 props（store 直读 + `runActiveHere` 归属判定内聚）；PipelineView 创作区 = `<RunChat v-if="runActiveHere" />` `<CreateRunPanel v-else-if="!hasArtifacts" />`

- [ ] **Step 1: RunChat.vue**——script：

```js
/* 运行对话流容器：feed 正序渲染 + 活的信号三件套 + 断线横幅 + 恢复说明。
 * 计时口径：事件帧无时间戳，只有本会话观察到的阶段才有时刻（stageSince）。 */
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { studio } from "../../../studio/studioStore.js";
import { buildFeed } from "./useRunFeed.js";
import ChatAvatar from "./ChatAvatar.vue";
import SystemNote from "./SystemNote.vue";
import StageMessage from "./StageMessage.vue";
import UserNovelBubble from "./UserNovelBubble.vue";
import GateConfirmCard from "./GateConfirmCard.vue";
import FailCard from "./FailCard.vue";
import DoneCard from "./DoneCard.vue";

const run = computed(() => studio.run);
const feed = computed(() => buildFeed(run.value.events, run.value.novel));

/* 活的信号：200ms tick 只在 running 时走；12s 无新事件提示（照 ChatPanel 口径） */
const nowTick = ref(Date.now());
let ticker = null;
function stopTicker() { if (ticker) { clearInterval(ticker); ticker = null; } }
watch(() => run.value.status === "running", (on) => {
  nowTick.value = Date.now(); stopTicker();
  if (on) ticker = setInterval(() => { nowTick.value = Date.now(); }, 200);
}, { immediate: true });
onUnmounted(stopTicker);
const lastEventAt = ref(0);
watch(() => run.value.events.length, () => { lastEventAt.value = Date.now(); });
const silent = computed(() => (run.value.status === "running" && lastEventAt.value
  && nowTick.value - lastEventAt.value >= 12000)
  ? Math.round((nowTick.value - lastEventAt.value) / 1000) : 0);

/* 运行中阶段的计时起点：本会话首次观察到该 stage 帧的时刻 */
const stageSince = ref({});
watch(() => run.value.events.length, () => {
  const evs = run.value.events;
  const last = evs[evs.length - 1];
  if (last?.type === "stage" && !stageSince.value[last.stage]) {
    stageSince.value = { ...stageSince.value, [last.stage]: Date.now() };
  }
});

/* 恢复说明：reattach 路径 link='restored' 时一次性提示（历史帧不回放） */
const restoredNote = ref(false);
watch(() => [run.value.id, run.value.link], ([, link]) => {
  if (link === "restored") restoredNote.value = true;
  if (link === "") restoredNote.value = false;
}, { immediate: true });

/* stick-bottom 滚动（照 ChatPanel：距底 90px 内才跟随） */
const streamEl = ref(null);
const stickBottom = ref(true);
function onScroll() {
  const el = streamEl.value;
  if (el) stickBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
}
watch(() => [feed.value.length, run.value.events.length], () => {
  nextTick(() => {
    const el = streamEl.value;
    if (el && stickBottom.value) el.scrollTop = el.scrollHeight;
  });
});
```

  template：

```vue
<section class="rc">
  <div v-if="run.link === 'reconnecting'" class="rc-banner warn">直播连接断开，自动重连中…</div>
  <div v-else-if="run.link === 'lost'" class="rc-banner err">直播连接已断开（服务可能已重启），请刷新页面查看最新进度</div>
  <div ref="streamEl" class="rc-stream" @scroll="onScroll">
    <SystemNote v-if="restoredNote" text="已接入运行现场，此前过程不回放" />
    <div v-if="!feed.length" class="rc-none">正在连接执行器，等待第一帧…</div>
    <template v-for="(item, i) in feed" :key="i">
      <div v-if="item.kind === 'user'" class="rc-row user">
        <UserNovelBubble :text="item.text" /><ChatAvatar who="user" />
      </div>
      <div v-else-if="item.kind === 'stage'" class="rc-row bot">
        <ChatAvatar who="bot" />
        <StageMessage :item="item" :now-tick="nowTick"
                      :since="stageSince[item.stage] || 0" />
      </div>
      <GateConfirmCard v-else-if="item.kind === 'gate-card'" :stage="item.stage" />
      <FailCard v-else-if="item.kind === 'fail-card'"
                :stage="item.stage" :findings="item.findings" />
      <DoneCard v-else-if="item.kind === 'done-card'" />
      <SystemNote v-else-if="item.kind === 'system'" :text="item.text" />
      <div v-else class="rc-row bot">
        <ChatAvatar who="bot" />
        <div class="rc-bubble unknown">{{ item.raw }}</div>
      </div>
    </template>
    <div v-if="silent" class="rc-silent">已 {{ silent }}s 无新事件，可能卡住或断线</div>
  </div>
</section>
```

  样式：`.rc-row.user { flex-direction: row-reverse; }`；`.rc-bubble.unknown` = Agent 气泡灰调 + `--mono` + `word-break: break-all`；气泡/头像规格按 Global Constraints
- [ ] **Step 2: PipelineView 接线**——import RunChat；:181 `<CreateRunPanel v-if="!hasArtifacts || runActiveHere" />` 替换为 `<RunChat v-if="runActiveHere" />` + `<CreateRunPanel v-else-if="!hasArtifacts" />`；其余（页签/状态条/抽屉）零改动
- [ ] **Step 3: CreateRunPanel 瘦身**——删 ②运行卡（:268-374）与 ③大纲区（:376-382）模板；删对应脚本（run/runHere/reversedEvents/failEvent/outlineContent/STATUS_TEXT/KIND_LABELS/describe/lineClass/confirmApprove/recordFeedback/retryFailedStage/草稿编辑器段 :168-239/scrollToOutline；保留 novel/starting/entryError/canStart/begin 与 ①入口卡模板）；文件头注释改为「创作入口卡：粘贴小说 → startRun；运行现场在 RunChat（chat/RunChat.vue）」
- [ ] **Step 4: build + 提交**

```bash
cd web-vue && npm run build
cd .. && git add web-vue/src/views/PipelineView.vue web-vue/src/views/pipeline/CreateRunPanel.vue web-vue/src/views/pipeline/chat/RunChat.vue
git commit -m "feat: wire RunChat into pipeline view; retire inline run card"
```

---

### Task 4: CDP 真机验收 + 文档

**Files:**
- Modify: `docs/pipeline/phase2-acceptance.md`（队列新小节「管线对话流」）
- 证据：截图落 `C:\Users\Lenovo\AppData\Local\Temp\cdp\`

- [ ] **Step 1: Desktop 合并 + 后端重启**（controller；核验 mtime < 进程启动；本计划纯前端但依赖失败免疫先行合并）
- [ ] **Step 2: CDP 五场景**（driver 模式照 `webview-cdp-probe-technique` 记忆：TEMP/cdp/driver.mjs 文件传参；hash 路由同 URL 用 location.reload()）：
  1. 一次性项目真模型跑全程：正序渲染、阶段消息聚合工具卡、确认门卡唯一入口、放行后后续阶段继续入流、完成卡
  2. SQL 造 findings（advisory + blocking 各一条：`UPDATE pipeline_artifacts SET findings='[{"gate":"validate","rule":"risk-flag","severity":"advisory","detail":"…"},{"gate":"validate","rule":"refs","severity":"blocking","detail":"…"}]' WHERE …`）→ ArtifactCard 内 GateMarkers ⛔ 文案与红样式
  3. F5 恢复态：mid-run 刷新 → 「已接入运行现场，此前过程不回放」+ 状态延续，不留白屏
  4. `window.__studio.run.events.push({type:"mystery",x:1})` → 灰气泡不崩；`push({type:"stage_done",stage:"script",ok:true,degraded:true,blocking:2})` → 阶段消息 ⛔ 结束章 + 完成卡黄态
  5. 截图自检：全流程一图 + 确认门卡一图 + ⛔ 标记一图（空态/等待/错误态在场景 1/3 里逐一目检）
- [ ] **Step 3: 文档记录 + 提交**

```bash
git add docs/pipeline/phase2-acceptance.md
git commit -m "docs: pipeline chat feed — wechat-style run timeline acceptance record"
```

---

## 验收清单

- [ ] 正序对话流：小说气泡（6 行折叠）→ 阶段消息（工具卡/门行/结束章）→ 确认门卡 → 后续阶段 → 完成卡
- [ ] 确认门：唯一入口「确认，开写剧本」逐字保留；意见记录不触发重写的提示原样
- [ ] 失败卡：findings + 草稿编辑器 + 盲重试/带修改重试全可用（逻辑逐字迁移）
- [ ] ⛔ blocking / ⚠️ advisory 两态 GateMarkers；DoneCard degraded 黄态；stage_done degraded 键消费
- [ ] 边界态：恢复说明、断线横幅、空 feed 占位、未知事件灰气泡、12s 静默提示
- [ ] 红线：冻结文件零 diff（store.js/canvas2/ai/ChatPanel 家族/四页签面板/后端）；studioStore 仅三处增补

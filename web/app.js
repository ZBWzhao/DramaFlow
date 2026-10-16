/* 前端：读 NDJSON 事件流并渲染。
 *
 * 对应 Toonflow 的 apps/web —— 但这里只有一层薄渲染：
 * 服务端吐一行 JSON，前端按类型画一块 UI。事件契约就是前后端的全部约定。
 */

const state = {
  sessionId: null,
  running: false,
  currentBubble: null,
  lastRunStatus: "idle",
  limitedNoticeShown: false,
  tools: new Map(),   // toolCallId? -> element（这里用 name 做 key，够用）
  subs: new Map(),
};

const $ = (id) => document.getElementById(id);
const streamEl = () => $("stream");

function clearEmpty() {
  const empty = streamEl().querySelector(".empty");
  if (empty) empty.remove();
}

function scrollBottom() {
  streamEl().scrollTop = streamEl().scrollHeight;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function addUser(text) {
  const wrap = el("div", "msg user");
  wrap.appendChild(el("div", "who", "你"));
  wrap.appendChild(el("div", "bubble", text));
  streamEl().appendChild(wrap);
  scrollBottom();
}

function addRunNotice(text, kind = "") {
  clearEmpty();
  const notice = el("div", `run-notice ${kind}`.trim(), text);
  streamEl().appendChild(notice);
  scrollBottom();
}

function applyRunStatus(status) {
  state.lastRunStatus = status || "completed";
  const continueBtn = $("continue-btn");
  if (state.lastRunStatus === "limited") {
    setStatus("已暂停，可继续", "status-limited");
    continueBtn.classList.remove("hidden");
  } else if (state.lastRunStatus === "aborted") {
    setStatus("已中止", "status-run");
    continueBtn.classList.remove("hidden");
  } else if (state.lastRunStatus === "error") {
    setStatus("出错", "status-error");
    continueBtn.classList.remove("hidden");
  } else {
    setStatus("已完成", "status-idle");
    continueBtn.classList.add("hidden");
  }
}

function addAssistant() {
  const wrap = el("div", "msg assistant");
  wrap.appendChild(el("div", "who", "Agent"));
  const bubble = el("div", "bubble");
  wrap.appendChild(bubble);
  streamEl().appendChild(wrap);
  state.currentBubble = bubble;
  scrollBottom();
}

function addTool(name, args, sub) {
  const card = el("div", "tool" + (sub ? " sub" : ""));
  const head = el("div", "head");
  head.appendChild(el("span", "dot pulsing"));
  head.appendChild(el("span", "name", name));
  head.appendChild(el("span", "args", args || ""));
  if (sub) head.appendChild(el("span", "tag", "sub agent"));
  head.onclick = () => card.classList.toggle("open");
  card.appendChild(head);
  const body = el("div", "body");
  card.appendChild(body);
  streamEl().appendChild(card);
  state.tools.set(name, card);
  scrollBottom();
}

function finishTool(name, isError, details) {
  const card = state.tools.get(name);
  if (!card) return;
  card.classList.remove("pulsing");
  card.classList.add(isError ? "error" : "done", "open");
  const dot = card.querySelector(".dot");
  if (dot) dot.classList.remove("pulsing");
  const body = card.querySelector(".body");
  const text = details && Object.keys(details).length ? JSON.stringify(details, null, 2) : "（无附加信息）";
  if (body) body.textContent = text;
}

function addSubBlock(name) {
  const block = el("div", "subagent");
  block.appendChild(el("div", "title", `子 Agent · ${name}`));
  const meta = el("div", "meta", "启动中…");
  block.appendChild(meta);
  streamEl().appendChild(block);
  state.subs.set(name, block);
  scrollBottom();
  return block;
}

function subReport(name, summary) {
  let block = state.subs.get(name);
  if (!block) block = addSubBlock(name);
  block.appendChild(el("div", "report", summary));
  scrollBottom();
}

function subEnd(name, usage) {
  const block = state.subs.get(name);
  if (!block) return;
  const meta = block.querySelector(".meta");
  if (meta) meta.textContent = `完成 · 本次增量 ${usage ?? "?"} tokens`;
}

function setStatus(text, cls) {
  const node = $("status");
  node.textContent = text;
  node.className = cls || "";
}

function updateUsage(stats) {
  if (!stats) return;
  $("runs").textContent = stats.runs ?? 0;
  $("compactions").textContent = stats.compactions ?? 0;
  $("chars").textContent = stats.chars ?? 0;
  $("usage-main").textContent = stats.main?.total_tokens ?? 0;
  const box = $("usage-subs");
  box.innerHTML = "";
  for (const [name, value] of Object.entries(stats.sub_agents || {})) {
    const row = el("div", "sub-row");
    row.appendChild(el("span", "", name));
    row.appendChild(el("b", "", `${value.last?.total_tokens ?? 0} / 累计 ${value.cumulative?.total_tokens ?? 0}`));
    box.appendChild(row);
  }
}

function addTeamStep(member, role, tools) {
  const block = el("div", "subagent");
  block.appendChild(el("div", "title", `成员 · ${member}（${role}）`));
  block.appendChild(el("div", "meta", `工具白名单：${(tools || []).join(", ") || "无"}`));
  streamEl().appendChild(block);
  state.subs.set(`team:${member}:${role}`, block);
  scrollBottom();
}

function handleEvent(ev) {
  switch (ev.type) {
    case "session":
      state.sessionId = ev.session_id;
      $("session-id").textContent = ev.session_id;
      break;
    case "agent_start":
      if (!ev.sub_agent) {
        state.lastRunStatus = "running";
        setStatus("运行中", "status-run");
      }
      break;
    case "message_start":
      if (!ev.sub_agent && ev.role === "assistant") addAssistant();
      break;
    case "message_update":
      if (!ev.sub_agent && state.currentBubble) {
        state.currentBubble.textContent += ev.delta || "";
        scrollBottom();
      }
      break;
    case "tool_execution_start":
      addTool(ev.tool, ev.args, !!ev.sub_agent);
      break;
    case "tool_execution_end": {
      const { tool, isError, ...details } = ev;
      finishTool(tool, isError, details);
      // 画布工具改完图会在结果里带 canvas_changed —— 前端据此刷新，不需要额外推送通道
      if (details.canvas_changed) CanvasView.load(details.canvas_changed);
      break;
    }
    case "sub_agent_start":
      addSubBlock(ev.sub_agent);
      break;
    case "sub_agent_report":
      subReport(ev.sub_agent, ev.summary);
      break;
    case "sub_agent_end":
      subEnd(ev.sub_agent, null);
      break;
    case "sub_agent_usage":
      subEnd(ev.sub_agent, ev.total_tokens);
      break;
    case "team_step_start":
      addTeamStep(ev.member, ev.role, ev.tools);
      break;
    case "team_done":
      updateUsage(ev.stats);
      if (ev.result) {
        addAssistant();
        if (state.currentBubble) state.currentBubble.textContent = ev.result;
      }
      if (ev.usage) {
        const block = el("div", "subagent");
        block.appendChild(el("div", "title", "团队用量"));
        block.appendChild(
          el("div", "meta", `增量 ${ev.usage.total_tokens} tokens｜全量口径 ${ev.usage_all?.total_tokens ?? "-"} tokens`)
        );
        streamEl().appendChild(block);
      }
      break;
    case "sub_agent_state":
      refreshSubagents();
      break;
    case "limited":
      if (!ev.sub_agent) {
        state.lastRunStatus = "limited";
        if (!state.limitedNoticeShown) {
          addRunNotice(ev.message || "本次运行达到安全限制，已保存进度，可继续制作。", "warning");
          state.limitedNoticeShown = true;
        }
        applyRunStatus("limited");
      }
      break;
    case "checkpoint":
      // 检查点由后端持久化；前端只负责明确告诉用户可以无损继续。
      if (!ev.sub_agent) $("continue-btn").classList.remove("hidden");
      break;
    case "done":
      updateUsage(ev.stats);
      refreshSubagents();
      applyRunStatus(ev.status);
      break;
    case "aborted":
      setStatus("已中止", "status-run");
      break;
    case "error":
      setStatus("出错", "status-run");
      break;
    default:
      break;
  }
}

async function readStream(res) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        handleEvent(JSON.parse(line));
      } catch (err) {
        console.warn("坏事件", line, err);
      }
    }
  }
}

async function send(promptOverride = null) {
  if (state.running) return;
  const input = $("input");
  const prompt = (typeof promptOverride === "string" ? promptOverride : input.value).trim();
  if (!prompt) return;

  clearEmpty();
  addUser(prompt);
  input.value = "";
  state.running = true;
  state.lastRunStatus = "running";
  state.limitedNoticeShown = false;
  $("continue-btn").classList.add("hidden");
  $("send-btn").disabled = true;
  $("stop-btn").disabled = false;
  setStatus("运行中", "status-run");

  // 模式值：agent / team:<团队名>（支持切到 dramaTeam 这类多成员团队）
  const modeValue = $("mode").value;
  const teamName = modeValue.startsWith("team:") ? modeValue.slice(5) : null;
  try {
    const res = await fetch(teamName ? "/api/team" : "/api/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        teamName
          ? { task: prompt, team: teamName, session_id: state.sessionId }
          : { prompt, session_id: state.sessionId }
      ),
    });
    await readStream(res);
    if (state.lastRunStatus === "running") applyRunStatus("completed");
  } catch (err) {
    setStatus("出错", "status-run");
    console.error(err);
  } finally {
    state.running = false;
    $("send-btn").disabled = false;
    $("stop-btn").disabled = true;
    if (state.sessionId) refreshStats();
  }
}

async function stop() {
  if (!state.sessionId) return;
  await fetch("/api/agent/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: state.sessionId }),
  });
  setStatus("已请求停止", "status-run");
}

async function refreshStats() {
  const res = await fetch(`/api/sessions/${state.sessionId}/stats`);
  if (res.ok) updateUsage(await res.json());
}

async function refreshSubagents() {
  if (!state.sessionId) return;
  const res = await fetch(`/api/sessions/${state.sessionId}/subagents`);
  if (!res.ok) return;
  const data = await res.json();
  const list = $("subagent-list");
  list.innerHTML = "";
  if (!data.sub_agents.length) {
    list.appendChild(el("li", "", "（本次还没有委派）"));
    return;
  }
  for (const sub of data.sub_agents) {
    const li = el("li");
    const status = sub.interrupted ? `${sub.status} · 已中断` : sub.status;
    li.appendChild(el("div", "name", `${sub.name} · ${status}`));
    const inc = sub.last ? sub.last.total_tokens : 0;
    const total = sub.cumulative ? sub.cumulative.total_tokens : 0;
    const reports = sub.reports && sub.reports.length ? `｜上报 ${sub.reports.length}` : "";
    li.appendChild(el("div", "desc", `增量 ${inc} / 累计 ${total} tokens${reports}`));
    if (sub.error) li.appendChild(el("div", "desc", `错误：${sub.error}`));
    list.appendChild(li);
  }
}

async function loadTools() {
  const res = await fetch("/api/tools");
  const data = await res.json();
  $("workspace").textContent = data.workspace.split(/[\\/]/).filter(Boolean).pop(); // 只显示最后一段，chip 才不会被撑爆
  const list = $("tool-list");
  list.innerHTML = "";
  for (const tool of data.tools) {
    const li = el("li");
    li.appendChild(el("div", "name", `${tool.name} · v${tool.version}`));
    li.appendChild(el("div", "desc", tool.description || tool.load_error || ""));
    list.appendChild(li);
  }
}

async function loadSkills() {
  const res = await fetch("/api/skills");
  const data = await res.json();
  $("skill-chars").textContent = `${data.prompt_chars} / ${data.body_chars}`;
  const list = $("skill-list");
  list.innerHTML = "";
  for (const skill of data.skills) {
    const li = el("li");
    li.appendChild(el("div", "name", skill.name));
    li.appendChild(el("div", "desc", `${skill.description}（${skill.body_chars} 字，按需读）`));
    list.appendChild(li);
  }
}

$("send-btn").onclick = send;
$("stop-btn").onclick = stop;
$("continue-btn").onclick = () => send(
  "继续当前任务：先读取上次运行检查点和当前画布，核对已有产物，只完成尚未完成的部分；不要重复创建已有节点，完成后给出交付总结。"
);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
function switchView(name) {
  document.querySelectorAll(".tab").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === name));
  $("view-chat").classList.toggle("hidden", name !== "chat");
  $("view-canvas").classList.toggle("hidden", name !== "canvas");
  location.hash = name === "canvas" ? "#canvas" : "";
  if (name === "canvas") CanvasView.load(CanvasView.s.name || "main");
}

document.querySelectorAll(".tab").forEach((btn) => {
  btn.onclick = () => switchView(btn.dataset.view);
});

// 信息抽屉（会话统计 / 技能 / 子 Agent / 工具）
$("info-toggle").onclick = () => $("info-drawer").classList.toggle("hidden");
$("info-close").onclick = () => $("info-drawer").classList.add("hidden");

loadTools();
loadSkills();
CanvasView.init();
CanvasView.initPicker();
// 默认视图就是画布，所以必须主动拉一次数据（不是等切换才加载）
CanvasView.load("main");
if (location.hash === "#chat") switchView("chat"); // #chat 可直接分享对话视图

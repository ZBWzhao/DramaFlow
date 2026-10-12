/* 对话渲染自测（不需要浏览器、不花 API 费用）
 *
 * 对话页的正确性全在"事件 → parts 归约"这一步，而它在浏览器里没法方便地复现
 * （要真跑一次模型）。这里用固定的事件流打桩 fetch，直接断言归约结果。
 *
 * 两个场景：
 *   一、正常跑完：parts 顺序、思考自动收起、同名工具不串卡、画布变更、子 Agent 嵌套、结束章
 *   二、**流中断（没有 done）**：必须明确标成"未正常结束"，且不留下永远转圈的工具卡
 *
 * 跑法：cd web-vue && node scripts/stream-selftest.mjs
 */

globalThis.location = { hash: "", search: "" };

const PING = (elapsed) => ({ type: "ping", session_id: "selftest-1", elapsed, dropped: 0 });

const EVENTS = [
  { type: "session", session_id: "selftest-1" },
  { type: "agent_start", agent: "main" },
  // 上下文压缩：压缩本身是一次模型请求，可能很久 —— 界面必须显示出来
  { type: "compaction_start", chars: 18000, compacted: 12 },
  { type: "compaction_end", chars: 6000, count: 1 },
  { type: "compaction_rejected", reason: "empty_summary" },
  { type: "turn_start", turn: 1 },

  { type: "message_start", role: "assistant", block_id: "b1" },
  { type: "thinking_update", block_id: "b1", delta: "先看看画布" },
  { type: "thinking_update", block_id: "b1", delta: "，再核对分镜。" },
  { type: "thinking_end", block_id: "b1", chars: 12 },
  { type: "tool_execution_start", tool: "get_canvas", tool_call_id: "call_a", index: 0, args: '{"canvas":"main"}' },
  PING(6),
  {
    type: "tool_execution_end",
    tool: "get_canvas",
    tool_call_id: "call_a",
    isError: false,
    duration_ms: 4,
    result: '{"node_count":12,"edge_count":25}',
    result_chars: 33,
    canvas_changed: "main",
    nodes: 12,
    edges: 25,
  },
  { type: "message_update", block_id: "b1", delta: "画布正常。" },
  { type: "message_end", role: "assistant", block_id: "b1", chars: 5, content: "画布正常。" },

  { type: "turn_start", turn: 2 },
  { type: "message_start", role: "assistant", block_id: "b2" },
  { type: "tool_execution_start", tool: "get_canvas", tool_call_id: "call_b", index: 0, args: '{"canvas":"main"}' },
  { type: "tool_execution_end", tool: "get_canvas", tool_call_id: "call_b", isError: false, duration_ms: 6, result: "ok", result_chars: 2 },
  // 同一轮里同名工具再调一次 —— 只有靠 tool_call_id 才能不串卡
  { type: "tool_execution_start", tool: "get_canvas", tool_call_id: "call_b2", index: 1, args: '{"canvas":"main"}' },
  { type: "tool_execution_end", tool: "get_canvas", tool_call_id: "call_b2", isError: false, duration_ms: 5, result: "ok2", result_chars: 3 },
  { type: "tool_execution_start", tool: "subagent", tool_call_id: "call_c", index: 2, args: '{"name":"researcher","task":"核对时长"}' },
  { type: "sub_agent_start", sub_agent: "researcher" },
  { type: "tool_execution_start", tool: "read_file", tool_call_id: "sub_1", sub_agent: "researcher", args: '{"path":"a.json"}' },
  PING(12),
  { type: "tool_execution_end", tool: "read_file", tool_call_id: "sub_1", sub_agent: "researcher", isError: false, duration_ms: 812, result: "{}", result_chars: 2 },
  { type: "sub_agent_report", sub_agent: "researcher", summary: "3 处时长口径不一致" },
  { type: "sub_agent_usage", sub_agent: "researcher", total_tokens: 3434 },
  { type: "sub_agent_end", sub_agent: "researcher" },
  { type: "tool_execution_end", tool: "subagent", tool_call_id: "call_c", isError: false, duration_ms: 9000, result: "核对完成", result_chars: 4, sub_agent: "researcher", status: "done" },
  { type: "tool_execution_start", tool: "connect", tool_call_id: "call_d", index: 3, args: '{"source":"a","target":"b"}' },
  { type: "tool_execution_end", tool: "connect", tool_call_id: "call_d", isError: true, duration_ms: 2, result: "这条边会形成环", result_chars: 8 },
  { type: "message_update", block_id: "b2", delta: "分镜已修好，" },
  { type: "message_update", block_id: "b2", delta: "**60s**。\n\n- 时长一致\n- 编号已展开" },
  { type: "message_end", role: "assistant", block_id: "b2", chars: 30, content: "分镜已修好，**60s**。" },

  { type: "usage", input_tokens: 12000, output_tokens: 260, total_tokens: 12260, cached_tokens: 1024, label: "main", block_id: "b2" },
  { type: "done", stats: { runs: 2, compactions: 0, chars: 4210, main: { input_tokens: 12000, output_tokens: 260, total_tokens: 12260 }, sub_agents: {} } },
];

// 场景二：跑到一半**流就断了**（没有 done / agent_end）——这正是"没完成也不显示结束"的现场
const TRUNCATED = [
  { type: "session", session_id: "selftest-1" },
  { type: "agent_start", agent: "main" },
  { type: "turn_start", turn: 3 },
  { type: "message_start", role: "assistant", block_id: "b3" },
  { type: "thinking_update", block_id: "b3", delta: "再确认一次。" },
  { type: "tool_execution_start", tool: "get_canvas", tool_call_id: "call_stuck", index: 0, args: '{"canvas":"main"}' },
  PING(18),
];

let call = 0;
let capturedBodies = [];
let canvasRefreshes = 0;

globalThis.fetch = async (url, options = {}) => {
  const target = String(url);
  if (target.includes("/api/agent")) {
    call += 1;
    capturedBodies.push(JSON.parse(options.body || "{}"));
    const events = call === 1 ? EVENTS : TRUNCATED;
    const stream = new ReadableStream({
      start(controller) {
        const encoder = new TextEncoder();
        const lines = events.map((e) => JSON.stringify(e) + "\n");
        const third = Math.ceil(lines.length / 3);
        controller.enqueue(encoder.encode(lines.slice(0, third).join("")));
        controller.enqueue(encoder.encode(lines.slice(third, third * 2).join("")));
        controller.enqueue(encoder.encode(lines.slice(third * 2).join("")));
        controller.close(); // 场景二：直接结束，没有终态事件
      },
    });
    return new Response(stream, { status: 200 });
  }
  if (target.includes("/api/canvas?")) {
    canvasRefreshes += 1;
    return new Response(JSON.stringify({ name: "main", nodes: [], edges: [], node_spec: {}, topo_order: [], problems: [] }), { status: 200 });
  }
  if (target.includes("/stats")) return new Response(JSON.stringify({ runs: 2 }), { status: 200 });
  if (target.includes("/subagents")) return new Response(JSON.stringify({ sub_agents: [] }), { status: 200 });
  return new Response("{}", { status: 200 });
};

const { actions, state } = await import("../src/store.js");

const failures = [];
const check = (label, ok, extra = "") => {
  console.log(`${ok ? "✓" : "✗"} ${label}${extra ? `　${extra}` : ""}`);
  if (!ok) failures.push(label);
};

// ============ 场景一：正常跑完 ============
console.log("=== 场景一：正常跑完 ===\n");
await actions.send("核对分镜与提示词");

const messages = state.feed.filter((i) => i.type === "assistant");
const notes = state.feed.filter((i) => i.type === "note");
const kindsOf = (item) => item.parts.map((p) => p.kind).join(" → ");
const describe = (item) =>
  item.parts
    .map((p) => (p.kind === "tool" ? `tool[${p.name}#${p.id}]` : p.kind === "sub" ? `sub[${p.name}:${p.steps.length}步]` : p.kind))
    .join(" → ");
console.log("第一条：", kindsOf(messages[0]));
console.log("第二条：", describe(messages[1]), "\n");

check("压缩开始/结束都在界面上有痕迹", notes.some((n) => n.title === "正在压缩上下文") && notes.some((n) => n.title === "上下文已压缩"));
check("压缩被拒也有痕迹（不是静默跳过）", notes.some((n) => n.title === "未压缩上下文" && n.text.includes("empty_summary")));
check("压缩结束后 compaction 状态复位", state.compacting === false);
check("请求体带上了 prompt", capturedBodies[0]?.prompt?.includes("核对分镜"));
check("session_id 被记录", state.sessionId === "selftest-1");
check("首条是 user", state.feed[0].type === "user");
check("两条 assistant 消息各自独立（block_id 分组）", messages.length === 2);

const [think1, tool1, text1] = messages[0].parts;
check("第一条 parts 顺序是 思考→工具→正文", kindsOf(messages[0]) === "thinking → tool → text", kindsOf(messages[0]));
check("思考内容完整拼接", think1.text === "先看看画布，再核对分镜。");
check("思考流式结束后自动收起", think1.streaming === false && think1.expanded === false);
check("工具卡是成功态且带耗时与结果", tool1.status === "success" && tool1.durationMs === 4 && tool1.result.includes("edge_count"));
check("工具卡带画布变更标记", tool1.canvas?.nodes === 12);
check("画布变更触发了前端刷新", canvasRefreshes >= 1, `刷新 ${canvasRefreshes} 次`);
check("正文内容正确", text1.text === "画布正常。");

const parts2 = messages[1].parts;
const tools2 = parts2.filter((p) => p.kind === "tool");
const [tA, tB, tC, tD] = tools2;
check("第二次 message_start 会收尾上一条", messages[0].streaming === false);
check("同一轮同名工具两次调用各成一张卡", tools2.filter((p) => p.name === "get_canvas").length === 2);
check("两张同名卡各自绑到自己的 id 与结果", tA.id === "call_b" && tA.result === "ok" && tB.id === "call_b2" && tB.result === "ok2");
check("委托工具卡是成功态", tC.name === "subagent" && tC.status === "success");
check("失败卡保留错误文本", tD.status === "error" && tD.result.includes("会形成环"));
check("失败卡不会误标画布变更", !tD.canvas);

const sub = parts2.find((p) => p.kind === "sub");
check("子 Agent 块存在且终态正确", !!sub && sub.status === "done");
check("子 Agent 的 report 与用量写回", sub.reports[0] === "3 处时长口径不一致" && sub.tokens === 3434);
check("子 Agent 的工具进它自己的 steps", sub.steps.length === 1 && sub.steps[0].name === "read_file");
check(
  "父的委托结果没有跑进子块（sub_agent 字段歧义曾经导致这个 bug）",
  !sub.steps.some((step) => step.name === "subagent"),
  `子块里的步骤：${sub.steps.map((s) => s.name).join("、")}`
);

const text2 = parts2.filter((p) => p.kind === "text").pop();
check("第二段正文按 delta 累积", text2.text.includes("分镜已修好，") && text2.text.includes("**60s**"));
check("usage 事件更新了用量条", state.usage?.total === 12260 && state.usage?.cached === 1024);
check("心跳被记录但不进 feed（只更新活着信号）", state.pings === 2, `pings=${state.pings}`);
check("结束原因是 done", state.endReason === "done", `endReason=${state.endReason}`);
check("最后一条消息盖了结束章", messages[1].ended?.reason === "done" && messages[1].ended?.elapsedMs >= 0);
check("活动提示已清空（不再显示运行中）", state.activity === null);
check("没有残留的 streaming 标记", state.feed.every((i) => i.type !== "assistant" || i.streaming === false));
check("没有残留的运行中工具卡", state.feed.every((i) => i.type !== "assistant" || i.parts.every((p) => p.kind !== "tool" || p.status !== "running")));

// ============ 场景二：流中断，没有终态事件 ============
console.log("\n=== 场景二：流中断（没有 done）===\n");
const before = state.feed.length;
await actions.send("再核对一次");

const last = state.feed[state.feed.length - 1];
const stuck = state.feed[state.feed.length - 2];
check("中断也被明确标记", state.endReason === "unknown", `endReason=${state.endReason}`);
check("界面上给出未正常结束的提示", last.type === "note" && last.title.includes("未正常结束"));
check("运行中被打断的工具卡被标成中断", stuck.parts.find((p) => p.kind === "tool")?.status === "interrupted");
check("中断后不再显示为运行中", state.running === false && state.activity === null);
check("中断的最后一条消息也盖了结束章", stuck.ended?.reason === "unknown");
check("feed 增加了本次的内容", state.feed.length > before);
check("思考块在中断时也被收尾（不留转圈）", stuck.parts.find((p) => p.kind === "thinking")?.streaming === false);

console.log(`\n${failures.length ? `✗ ${failures.length} 项未通过：${failures.join("、")}` : "✓ 全部通过"}`);
process.exit(failures.length ? 1 : 0);

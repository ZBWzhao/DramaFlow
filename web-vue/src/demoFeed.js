/* 前端开发用假数据：`?demo=1` 时把一条"完整形态"的对话塞进 feed。
 *
 * 存在的理由：调界面时要看到 **思考块 / 工具成功 / 工具失败 / 画布变更 / 子 Agent /
 * Markdown 正文** 全部形态，但每跑一次真模型都要花钱花时间。
 * 这里用固定数据把这些形态一次性铺出来，改样式时直接刷新即可。
 *
 * 它**不参与正常路径**：只有显式带 ?demo=1 才会用到。
 */
import { state } from "./store.js";

export function loadDemoFeed() {
  const now = Date.now();
  state.sessionId = "demo-session";
  state.status = "idle";
  state.usage = { input: 12173, output: 267, cached: 1024, total: 12440, label: "main" };
  state.stats = {
    runs: 1,
    compactions: 0,
    chars: 4210,
    main: { input_tokens: 12173, output_tokens: 267, total_tokens: 12440 },
    sub_agents: { researcher: { last: { total_tokens: 3434 }, cumulative: { total_tokens: 3434 } } },
  };
  state.subagents = [
    { name: "researcher", status: "done", last: { total_tokens: 3434 }, cumulative: { total_tokens: 3434 }, reports: ["扫到 3 处时长口径不一致"] },
  ];

  state.feed = [
    { type: "user", text: "核对分镜与提示词的对应关系，把时长口径修一致" },
    {
      type: "assistant",
      blockId: "b1",
      streaming: false,
      startedAt: now - 42000,
      parts: [
        {
          kind: "thinking",
          text: "先看画布现状，确认分镜与提示词节点之间的连线是否完整。\n\n如果连线断了，提示词就取不到分镜数据，时长自然对不上。",
          duration: 6.2,
          streaming: false,
          expanded: true,
          startedAt: now - 42000,
        },
        {
          kind: "tool",
          id: "call_1",
          name: "get_canvas",
          args: '{"canvas": "main"}',
          status: "success",
          result: '{"nodes": 12, "edges": 25, "problems": []}',
          resultChars: 44,
          durationMs: 3,
          details: {},
          canvas: { name: "main", nodes: 12, edges: 25 },
          expanded: false,
        },
        {
          kind: "sub",
          name: "researcher",
          status: "done",
          tokens: 3434,
          reports: ["扫到 3 处时长口径不一致：分镜 50s / 提示词 5×6s / 目标 60s"],
          text: "",
          expanded: true,
          steps: [
            {
              kind: "tool",
              id: "call_s1",
              name: "read_file",
              args: '{"path": "assets/storyboard/scene-1.json"}',
              status: "success",
              result: '{"shots": 5, "target_duration": 50}',
              resultChars: 36,
              durationMs: 812,
              expanded: false,
            },
            {
              kind: "tool",
              id: "call_s2",
              name: "get_canvas",
              args: '{"canvas": "main"}',
              status: "success",
              result: "…",
              resultChars: 44,
              durationMs: 4,
              expanded: false,
            },
          ],
        },
        {
          kind: "text",
          text: "核对完成，有两处不一致：\n\n1. **分镜头写 50.0s，实际 60s** —— 上次改时长没同步分镜\n2. **片段1 的时长切分与分镜不符**（分镜 5/6/5s，提示词写 5/5/5s）\n\n先修分镜（改为 60s）：",
          streaming: false,
        },
        {
          kind: "tool",
          id: "call_2",
          name: "set_storyboard",
          args: '{"node_id": "n_a844d9", "target_duration": 60}',
          status: "success",
          result: "已写入第1场分镜：5 个镜头，目标时长 60s",
          resultChars: 24,
          durationMs: 6,
          details: {},
          canvas: { name: "main", nodes: 12, edges: 25 },
          expanded: false,
        },
        {
          kind: "tool",
          id: "call_3",
          name: "connect",
          args: '{"source": "n_10b4a6", "source_handle": "prompt", "target": "n_02d089", "target_handle": "in"}',
          status: "error",
          result: "这条边会形成环：剧本正文 → 第1场分镜 → 片段1·推门进店 → 剧本正文，流程将无法排序",
          resultChars: 45,
          durationMs: 2,
          details: {},
          expanded: true,
        },
        {
          kind: "text",
          text: "分镜已修好（**60s**，写入成功）。\n\n另一次连线被服务端拒绝——它会造成环，我改成连到正确的上游后再试。",
          streaming: false,
        },
      ],
    },
    { type: "note", title: "提示", text: "这是 ?demo=1 的开发用假数据，不消耗 API" },
  ];
}

/** `?demo=live`：把界面置于"**正在运行**"状态，用来检查"活着"信号是否直观。
 *
 * 这是这轮修的问题的专用视图：计时器在走、当前活动可见、运行中的工具卡有实时耗时、
 * 并且故意把 lastEventAt 设成 16 秒前，触发"已 Ns 无新事件"告警。
 */
export function loadLiveFeed() {
  loadDemoFeed();
  const now = Date.now();
  state.running = true;
  state.status = "running";
  state.runStartedAt = now - 42000;
  state.lastEventAt = now - 16000;
  state.activity = "执行工具 get_canvas";
  state.canvasActivity = {
    phase: "running",
    tool: "set_storyboard",
    summary: "Agent 正在操作画布 · set_storyboard",
    at: now,
  };
  state.changedNodeIds = ["n_a844d9"];
  state.canvasRevision += 1;
  state.pings = 5;
  state.endReason = null;
  state.nowTick = now;
  state.feed.push({
    type: "assistant",
    blockId: "b9",
    streaming: true,
    parts: [
      {
        kind: "thinking",
        text: "画布结构已恢复，接着核对时长口径。",
        duration: 3.1,
        streaming: false,
        expanded: false,
        startedAt: now,
      },
      {
        kind: "tool",
        id: "call_live",
        name: "get_canvas",
        args: '{"canvas":"main"}',
        status: "running",
        result: null,
        resultChars: 0,
        startedAt: now - 16000,
        durationMs: null,
        details: null,
        canvas: null,
        expanded: false,
      },
    ],
  });
}

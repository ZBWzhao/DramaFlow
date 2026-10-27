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

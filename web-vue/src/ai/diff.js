/* AI 修改建议的解析与应用（纯函数，node --test 可测）。
 *
 * 约定：AI 回复里可用 ```dramaflow-diff 围栏给出结构化修改建议，
 * 每块一个 JSON：{"segmentId":"...","field":"站位","old":"...","new":"..."}。
 * 面板把围栏块渲染成 diff 卡片（旧值划线 / 新值高亮 / 应用·放弃），
 * 正文其余部分按普通 markdown 文本显示。
 *
 * 「应用」= 把字段值写回该片段的完整提示词（【字段】行格式），
 * 再走 POST /segments/{id}/prompt（人改优先，服务端不再覆盖）。
 */

const FENCE = /```dramaflow-diff\s*\n([\s\S]*?)```/g;

/** 从回复文本中抽出全部 diff 围栏块。解析失败的块按普通文本保留，不抛错。 */
export function parseDiffs(text) {
  const diffs = [];
  const plain = String(text || "").replace(FENCE, (m, body) => {
    try {
      const d = JSON.parse(body);
      if (d && typeof d.field === "string" && typeof d.new === "string") {
        diffs.push({
          segmentId: String(d.segmentId || ""),
          field: String(d.field),
          old: String(d.old ?? ""),
          new: String(d.new),
        });
        return "";
      }
    } catch {
      /* 非 JSON 围栏：当普通文本 */
    }
    return m;
  });
  return { plain: plain.replace(/\n{3,}/g, "\n\n").trim(), diffs };
}

/** 把【field】字段的值替换为 value；没有该字段行时追加到末尾。 */
export function applyField(promptText, field, value) {
  const head = `【${field}】`;
  const text = String(promptText || "");
  if (!text.trim()) return head + value;
  const lines = text.split("\n");
  let hit = false;
  const next = lines.map((line) => {
    if (!hit && line.startsWith(head)) {
      hit = true;
      return head + value;
    }
    return line;
  });
  if (!hit) next.push(head + value);
  return next.join("\n");
}

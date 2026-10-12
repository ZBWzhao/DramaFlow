/* Markdown 渲染 + 消毒。
 *
 * 两个必须做的事：
 *   1. **消毒**：模型输出是不可信文本，v-html 前必须过 DOMPurify（否则一句
 *      `<img onerror=...>` 就能在页面上执行脚本）。
 *   2. **别每个 delta 都重解析**：流式时每来一个字都重排整段 Markdown 会卡。
 *      按"内容 + 是否仍在流式"缓存，并让调用方（MarkdownText）做节流。
 */
import { marked } from "marked";
import DOMPurify from "dompurify";

marked.setOptions({ gfm: true, breaks: true });

const cache = new Map();
const MAX_CACHE = 300;

export function renderMarkdown(text) {
  const source = String(text ?? "");
  const hit = cache.get(source);
  if (hit !== undefined) return hit;

  let html = "";
  try {
    html = DOMPurify.sanitize(marked.parse(source), { USE_PROFILES: { html: true } });
  } catch (err) {
    console.warn("Markdown 渲染失败，降级为纯文本", err);
    const div = document.createElement("div");
    div.textContent = source;
    html = div.innerHTML.replace(/\n/g, "<br>");
  }

  if (cache.size >= MAX_CACHE) cache.clear();
  cache.set(source, html);
  return html;
}

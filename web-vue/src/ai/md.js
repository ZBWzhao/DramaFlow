/* 极简 Markdown 渲染：先整体 HTML 转义再转换，杜绝注入。
 *
 * 为什么不引 markdown-it：聊天只需要标题/加粗/列表/行内码/代码块这几种，
 * 一个 60 行的渲染器省一个依赖，也省一次 supply-chain 审查。
 * 支持范围：# 标题、**加粗**、*斜体*、`行内码`、``` 代码块、- / 1. 列表、空行分段。
 */

function escapeHtml(source) {
  return String(source || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function renderMd(source) {
  const lines = escapeHtml(source).split("\n");
  const out = [];
  let list = null; // "ul" | "ol" | null
  let inCode = false;
  const codeBuf = [];

  const inline = (s) =>
    s
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<i>$2</i>")
      .replace(/`([^`\n]+)`/g, "<code class=\"md-code\">$1</code>");

  const flushList = () => {
    if (list) {
      out.push(`</${list}>`);
      list = null;
    }
  };

  for (const raw of lines) {
    if (/^\s*```/.test(raw)) {
      if (inCode) {
        out.push(`<pre class="md-pre"><code>${codeBuf.join("\n")}</code></pre>`);
        codeBuf.length = 0;
        inCode = false;
      } else {
        flushList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeBuf.push(raw);
      continue;
    }
    const line = raw.replace(/\s+$/, "");
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushList();
      out.push(`<div class="md-h">${inline(heading[2])}</div>`);
      continue;
    }
    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    if (ul) {
      if (list !== "ul") {
        flushList();
        out.push('<ul class="md-ul">');
        list = "ul";
      }
      out.push(`<li>${inline(ul[1])}</li>`);
      continue;
    }
    const ol = line.match(/^\s*\d+[.、)〕]\s+(.*)$/);
    if (ol) {
      if (list !== "ol") {
        flushList();
        out.push('<ol class="md-ol">');
        list = "ol";
      }
      out.push(`<li>${inline(ol[1])}</li>`);
      continue;
    }
    flushList();
    if (!line.trim()) {
      out.push('<div class="md-gap"></div>');
      continue;
    }
    out.push(`<p class="md-p">${inline(line)}</p>`);
  }
  if (inCode && codeBuf.length) {
    out.push(`<pre class="md-pre"><code>${codeBuf.join("\n")}</code></pre>`);
  }
  flushList();
  return out.join("");
}

/* API 层：所有后端调用集中在这里，组件不直接 fetch。
 *
 * 两条约定：
 *   · REST → getJSON / postJSON（失败抛带 detail 的 Error，调用方决定怎么提示）
 *   · NDJSON 事件流 → streamNDJSON（一行一个事件，边收边回调，符合后端的流式契约）
 */

const BASE = "";

async function toError(res) {
  let detail = `${res.status} ${res.statusText}`;
  let payload = null;
  try {
    payload = await res.json();
    if (payload && payload.detail) detail = payload.detail;
  } catch {
    /* 非 JSON 响应就用状态行 */
  }
  const error = new Error(detail);
  error.status = res.status;
  error.code = payload?.code || "";
  error.payload = payload;
  return error;
}

export async function getJSON(path) {
  const res = await fetch(BASE + path);
  if (!res.ok) throw await toError(res);
  return res.json();
}

export async function postJSON(path, body) {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw await toError(res);
  return res.json();
}

/** 读 NDJSON 流；异常断开后带 last_event_id 转入补发接口，最多重试 5 次。 */
export async function streamNDJSON(path, body, onEvent, onError, signal, options = {}) {
  let sessionId = body?.session_id || null;
  let runId = null;
  let lastEventId = options.lastEventId || "0-0";
  let terminal = false;
  let attempts = 0;
  const seen = new Set();

  const consume = async (res) => {
    sessionId = res.headers.get("X-Session-ID") || sessionId;
    runId = res.headers.get("X-Run-ID") || runId;
    options.onConnection?.({ sessionId, runId, lastEventId, reconnected: attempts > 0 });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const accept = (line) => {
      if (!line.trim()) return;
      try {
        const event = JSON.parse(line);
        if (event.event_id) {
          if (seen.has(event.event_id)) return;
          seen.add(event.event_id);
          lastEventId = event.event_id;
          options.onCursor?.(lastEventId);
        }
        if (event.session_id) sessionId = event.session_id;
        if (event.run_id) runId = event.run_id;
        if (event.type === "done" || event.type === "team_done") terminal = true;
        onEvent(event);
      } catch {
        console.warn("坏事件（已跳过）", line.slice(0, 120));
      }
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) accept(line);
    }
    buffer += decoder.decode();
    if (buffer.trim()) accept(buffer);
  };

  while (!signal?.aborted && !terminal) {
    const reconnecting = attempts > 0;
    const requestPath = reconnecting
      ? `/api/events/stream?${new URLSearchParams({
        session_id: sessionId || "",
        run_id: runId || "",
        last_event_id: lastEventId,
      })}`
      : path;
    try {
      const res = await fetch(BASE + requestPath, reconnecting ? { signal } : {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body ?? {}),
        signal,
      });
      if (!res.ok) throw await toError(res);
      await consume(res);
      if (terminal || signal?.aborted) return;
      // 兼容旧后端/测试桩：没有 X-Run-ID 时无法安全续传，交由调用方按
      // “缺少终态事件”统一收尾，避免把同一故障重复提示两次。
      if (!runId) return;
    } catch (err) {
      if (err?.name === "AbortError" || signal?.aborted) return;
      if (!sessionId || !runId) {
        onError?.(err);
        return;
      }
    }

    attempts += 1;
    if (attempts > 5 || !sessionId || !runId) {
      onError?.(new Error("事件流重连失败，已保留最后事件游标"));
      return;
    }
    options.onReconnect?.({ attempt: attempts, sessionId, runId, lastEventId });
    await new Promise((resolve) => setTimeout(resolve, Math.min(300 * (2 ** (attempts - 1)), 2400)));
  }
}

/** 上传原始文件；文件名/类型放在查询与 Content-Type，避免额外 multipart 依赖。 */
export async function postFile(path, file) {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  if (!res.ok) throw await toError(res);
  return res.json();
}

/* P0-5 的纯函数：视频卡的时长标签与首帧地址。
 * 刻意不依赖 DOM，方便 `npm run test:unit` 直接回归。
 */

/** 秒 → `m:ss`。非法值、0、负数都返回空串：卡片上宁可不显示，也不显示假的 `00:04`。 */
export function formatDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "";
  const whole = Math.round(total);
  if (whole <= 0) return "";
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

/** 给视频 URL 加 `#t=0.1`：浏览器只取第 0.1 秒附近的一小段数据画首帧，
 *  而不是为了画一张缩略图把整段 MP4 拖下来（几十个视频节点时的关键）。
 *  已经有 hash 的 URL 原样返回，避免把别人的播放指令覆盖掉。 */
export function videoFrameUrl(src, at = 0.1) {
  const value = typeof src === "string" ? src.trim() : "";
  if (!value) return "";
  if (value.includes("#")) return value;
  const offset = Number(at);
  const seconds = Number.isFinite(offset) && offset > 0 ? offset : 0.1;
  return `${value}#t=${seconds}`;
}

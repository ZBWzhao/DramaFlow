// 和 React 版一致的几何计算（阶段 D 按 spec 调整：标签行 22 高 + 6 间距）。
export const CARD_W = 248;
export const CAPTION_H = 22;
export const CAPTION_GAP = 6;

export const clamp = (v, min, max) => Math.min(max, Math.max(min, v));

export function cardHeight(node) {
  if (node.plain) return CAPTION_H + CAPTION_GAP + 132;
  return CAPTION_H + CAPTION_GAP + Math.round(CARD_W * (9 / 16));
}

/* 端口纵向分布：卡片标题栏以下、卡底以上，按端口数量均分。
 * 只有 1 个口时保持「垂直居中」，老连线的落点不变。
 * 返回值以「节点左上角」为原点；NodeCard 里画圆点时再减去卡体偏移。 */
export function portOffset(node, index = 0, count = 1) {
  const h = cardHeight(node);
  const bodyTop = CAPTION_H + CAPTION_GAP;
  if (!(count > 1)) return bodyTop + (h - bodyTop) / 2;
  const top = bodyTop + 16;
  const bottom = h - 16;
  return top + ((bottom - top) * (index + 0.5)) / count;
}

export function nodeAnchor(node, side, index = 0, count = 1) {
  const x = side === "left" ? node.x : node.x + CARD_W;
  return { x, y: node.y + portOffset(node, index, count) };
}

export function edgePath(from, to) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  // 真实画布上一张卡会连到很远的目标，曲率不能按 dx 无限放大（会甩出整屏的大弧线，
  // 从别的卡上面横穿过去）。把控制柄限制在一个卡片宽度左右，线贴着两端走。
  const c = Math.min(220, Math.max(48, Math.abs(dx) * 0.42));
  // 目标在左后方：两侧控制柄都朝右，线从两张卡之间绕过去，不会穿过卡片群。
  if (dx < -40) {
    return `M ${from.x} ${from.y} C ${from.x + c} ${from.y}, ${to.x + c} ${to.y}, ${to.x} ${to.y}`;
  }
  return `M ${from.x} ${from.y} C ${from.x + c} ${from.y}, ${to.x - c} ${to.y}, ${to.x} ${to.y}`;
}

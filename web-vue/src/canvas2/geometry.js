// 和 React 版完全一致的几何计算，保证两个 demo 长得一模一样。
export const CARD_W = 248;
export const CAPTION_H = 24;

export const clamp = (v, min, max) => Math.min(max, Math.max(min, v));

export function cardHeight(node) {
  if (node.plain) return 132;
  return CAPTION_H + Math.round(CARD_W * (9 / 16));
}

export function nodeAnchor(node, side) {
  const h = cardHeight(node);
  const x = side === "left" ? node.x : node.x + CARD_W;
  return { x, y: node.y + h / 2 };
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

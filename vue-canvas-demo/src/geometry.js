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
  const c = Math.max(40, Math.abs(to.x - from.x) * 0.45);
  return `M ${from.x} ${from.y} C ${from.x + c} ${from.y}, ${to.x - c} ${to.y}, ${to.x} ${to.y}`;
}

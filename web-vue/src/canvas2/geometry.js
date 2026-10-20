// 卡片几何（V2）：标签行 22 + 间距 6；卡体宽高比固定，宽度只用 300 / 360 / 420 三档。
// 高度 = 宽度 ÷ 比例 —— 禁止自由高度；文本卡用 3:2，媒体卡用 16:9。
export const CAPTION_H = 22;
export const CAPTION_GAP = 6;
export const CARD_WDEFAULT = 300;

// 三档宽度：视频类最宽（要看画面），图片次之，文本/提示词最窄
const WIDE_TYPES = new Set(["video", "video_gen", "timeline"]);
const MID_TYPES = new Set(["image", "image_gen", "asset"]);

export const clamp = (v, min, max) => Math.min(max, Math.max(min, v));

export function cardWidth(node) {
  if (WIDE_TYPES.has(node?.type)) return 420;
  if (MID_TYPES.has(node?.type)) return 360;
  return CARD_WDEFAULT;
}

/* 兼容旧常量：没有 node 上下文的地方（默认新节点宽度）用 300 */
export const CARD_W = CARD_WDEFAULT;

/* 宽高比：文本卡 3:2（横排文字更好读），媒体卡 16:9 */
export function cardAspect(node) {
  return node?.plain ? 3 / 2 : 16 / 9;
}

export function bodyHeight(node) {
  const width = cardWidth(node);
  return Math.round(width / cardAspect(node));
}

export function cardHeight(node) {
  return CAPTION_H + CAPTION_GAP + bodyHeight(node);
}

/* 端口纵向分布：卡体内部按端口数量均分（标签行不参与）。
 * 只有 1 个口时保持「卡体垂直居中」，老连线的落点相对卡体不变。
 * 返回值以「节点左上角」为原点。 */
export function portOffset(node, index = 0, count = 1) {
  const bodyTop = CAPTION_H + CAPTION_GAP;
  const bodyH = bodyHeight(node);
  if (!(count > 1)) return bodyTop + bodyH / 2;
  const top = bodyTop + 16;
  const bottom = bodyTop + bodyH - 16;
  return top + ((bottom - top) * (index + 0.5)) / count;
}

export function nodeAnchor(node, side, index = 0, count = 1) {
  const x = side === "left" ? node.x : node.x + cardWidth(node);
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

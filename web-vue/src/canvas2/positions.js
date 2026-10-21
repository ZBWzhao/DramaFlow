/* 画布坐标的纯函数。
 *
 * P0-3 的 bug 不在"算坐标"，而在写路径：`store.moveNode` 只把服务端返回的 revision 写回本地，
 * 没有把新坐标写回 `state.canvas.nodes[].position`，卡片松手后 computed 又按旧坐标渲染，
 * 于是"拖到哪弹回哪"。把"落点怎么算"抽成纯函数有两个好处：
 *   1. 写回路径只有一个来源（store 用 normalizePosition，画板用 dragMoves），不会再各算一份；
 *   2. 能用 `node --test` 直接覆盖，不需要浏览器。
 */

/** 取整并拒绝非法坐标（NaN / 无限大 / 缺字段一律返回 null，调用方自行决定怎么处理）。 */
export function normalizePosition(position) {
  const x = Number(position?.x);
  const y = Number(position?.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x: Math.round(x), y: Math.round(y) };
}

/** 画布节点用 `position`，画板派生卡用 `x/y`；两种形状都要能算出画布坐标。 */
export function nodePosition(node) {
  if (!node) return null;
  return normalizePosition(node.position ?? node);
}

/**
 * 一次拖动的落点列表：主卡和同组卡片都按"同一个屏幕位移（已换算成画布坐标）"平移。
 * `nodes` 必须是本次要一起移动的节点（调用方传选区），不会顺手把画布上别的卡也挪走。
 */
export function dragMoves(nodes, primaryId, dx, dy) {
  const list = Array.isArray(nodes) ? nodes : [];
  const shiftX = Number(dx) || 0;
  const shiftY = Number(dy) || 0;
  const moves = [];
  for (const node of list) {
    const base = nodePosition(node);
    if (!base) continue;
    const target = normalizePosition({ x: base.x + shiftX, y: base.y + shiftY });
    if (!target) continue;
    moves.push({ id: node.id ?? primaryId, position: target });
  }
  return moves;
}

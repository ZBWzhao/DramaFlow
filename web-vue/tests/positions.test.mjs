/* P0-3 的纯函数回归：拖动落点必须真的移动，且多选时整组同位移。
 * 运行：cd web-vue && npm run test:unit
 */
import test from "node:test";
import assert from "node:assert/strict";

import { dragMoves, nodePosition, normalizePosition } from "../src/canvas2/positions.js";

test("normalizePosition 取整并拒绝非法值", () => {
  assert.deepEqual(normalizePosition({ x: 100.4, y: -20.6 }), { x: 100, y: -21 });
  assert.equal(normalizePosition({ x: Number.NaN, y: 0 }), null);
  assert.equal(normalizePosition({ x: 0, y: Infinity }), null);
  assert.equal(normalizePosition(null), null);
});

test("nodePosition 同时认得画布节点的 position 和画板卡的 x/y", () => {
  assert.deepEqual(nodePosition({ position: { x: 10, y: 20 } }), { x: 10, y: 20 });
  assert.deepEqual(nodePosition({ x: 10, y: 20 }), { x: 10, y: 20 });
  assert.equal(nodePosition({}), null);
});

test("拖动位移真的落到新坐标上（回归：松手弹回原位）", () => {
  const node = { id: "a", position: { x: 100, y: 200 } };
  assert.deepEqual(dragMoves([node], "a", 40, -30), [
    { id: "a", position: { x: 140, y: 170 } },
  ]);
});

test("多选拖动：整组按同一位移平移，不在列表里的节点不会被挪", () => {
  const nodes = [
    { id: "a", position: { x: 100, y: 100 } },
    { id: "b", x: 300, y: 50 },
  ];
  const moves = dragMoves(nodes, "a", -25, 12);
  assert.deepEqual(moves, [
    { id: "a", position: { x: 75, y: 112 } },
    { id: "b", position: { x: 275, y: 62 } },
  ]);
  assert.ok(moves.every((move) => move.id === "a" || move.id === "b"));
});

test("非法拖动量按 0 处理，非法坐标的卡片直接跳过", () => {
  assert.deepEqual(dragMoves([{ id: "a", position: { x: 5, y: 5 } }], "a", "bad", undefined), [
    { id: "a", position: { x: 5, y: 5 } },
  ]);
  assert.deepEqual(dragMoves([{ id: "bad", position: { x: Number.NaN, y: 1 } }], "bad", 10, 10), []);
});

import assert from "node:assert/strict";
import { test } from "node:test";

import { applyField, parseDiffs } from "../src/ai/diff.js";

test("parseDiffs: 抽出围栏块并从正文移除", () => {
  const raw = [
    "看了片段 1-2 的分镜，建议如下：",
    "```dramaflow-diff",
    '{"segmentId":"seg-12","field":"站位","old":"林越立在台前。","new":"雨夜。林越立在台前。"}',
    "```",
    "确认后我再应用到画布。",
  ].join("\n");
  const { plain, diffs } = parseDiffs(raw);
  assert.equal(diffs.length, 1);
  assert.equal(diffs[0].segmentId, "seg-12");
  assert.equal(diffs[0].field, "站位");
  assert.equal(diffs[0].new, "雨夜。林越立在台前。");
  assert.equal(plain, "看了片段 1-2 的分镜，建议如下：\n\n确认后我再应用到画布。");
});

test("parseDiffs: 多块与非法 JSON 的降级", () => {
  const raw = [
    "```dramaflow-diff",
    '{"segmentId":"a","field":"画面","old":"x","new":"y"}',
    "```",
    "中间的话",
    "```dramaflow-diff",
    '{"broken json',
    "```",
  ].join("\n");
  const { plain, diffs } = parseDiffs(raw);
  assert.equal(diffs.length, 1);
  assert.ok(plain.includes("中间的话"));
  assert.ok(plain.includes("broken json")); // 解析失败的块保留在正文里
});

test("parseDiffs: 无围栏时原样返回", () => {
  const { plain, diffs } = parseDiffs("就是一段普通回复");
  assert.equal(diffs.length, 0);
  assert.equal(plain, "就是一段普通回复");
});

test("applyField: 替换已有字段行", () => {
  const prompt = "【禁止】无字幕\n【站位】林越立在台前。\n【画面】递袋";
  assert.equal(
    applyField(prompt, "站位", "雨夜。林越立在台前。"),
    "【禁止】无字幕\n【站位】雨夜。林越立在台前。\n【画面】递袋",
  );
});

test("applyField: 字段不存在时追加到末尾", () => {
  assert.equal(applyField("【禁止】无字幕", "道具", "热豆浆杯"), "【禁止】无字幕\n【道具】热豆浆杯");
  assert.equal(applyField("", "画风", "电影感写实"), "【画风】电影感写实");
});

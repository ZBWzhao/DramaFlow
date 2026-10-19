/* P0-5 的纯函数回归：视频时长标签必须来自真实元数据，首帧地址必须带时间片段。
 * 运行：cd web-vue && npm run test:unit
 */
import test from "node:test";
import assert from "node:assert/strict";

import { formatDuration, videoFrameUrl } from "../src/canvas2/video.js";

test("formatDuration 输出 m:ss，非法值不显示假时长", () => {
  assert.equal(formatDuration(6), "0:06");
  assert.equal(formatDuration(6.4), "0:06");
  assert.equal(formatDuration(65), "1:05");
  assert.equal(formatDuration(600), "10:00");
  assert.equal(formatDuration(0), "");
  assert.equal(formatDuration(-3), "");
  assert.equal(formatDuration(Number.NaN), "");
  assert.equal(formatDuration(null), "");
  assert.equal(formatDuration(undefined), "");
});

test("formatDuration 小于 1 秒也四舍五入到 0:00 以外或空串", () => {
  assert.equal(formatDuration(0.4), "");
  assert.equal(formatDuration(0.6), "0:01");
});

test("videoFrameUrl 给无 hash 的地址加 #t，保持已有 hash 不变", () => {
  assert.equal(videoFrameUrl("/assets/generated/a.mp4"), "/assets/generated/a.mp4#t=0.1");
  assert.equal(videoFrameUrl("/assets/a.mp4", 0.5), "/assets/a.mp4#t=0.5");
  assert.equal(videoFrameUrl("/assets/a.mp4#t=2"), "/assets/a.mp4#t=2");
  assert.equal(videoFrameUrl("  /assets/a.mp4  "), "/assets/a.mp4#t=0.1");
});

test("videoFrameUrl 对空地址返回空串，非法 at 退回 0.1", () => {
  assert.equal(videoFrameUrl(""), "");
  assert.equal(videoFrameUrl(null), "");
  assert.equal(videoFrameUrl(undefined), "");
  assert.equal(videoFrameUrl("/a.mp4", Number.NaN), "/a.mp4#t=0.1");
  assert.equal(videoFrameUrl("/a.mp4", -1), "/a.mp4#t=0.1");
});

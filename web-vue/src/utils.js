/* 小工具函数。
 *
 * assetUrl 存在的原因：画布 JSON 里产物路径是**工作区相对路径**（`assets/generated/x.png`），
 * 这是正确的存储格式（可移植）；而访问它需要拼上后端挂载点 `/assets/`。
 * 直接拼会得到 `/assets/assets/...` —— 这个 bug 曾经让所有缩略图 404，
 * 而且因为"图没显示"看起来像浏览器渲染问题，被误判过一次。所以统一走这一个函数。
 */

export function assetUrl(path) {
  if (!path) return "";
  return "/assets/" + String(path).replace(/^\/?assets\//, "").replace(/^\//, "");
}

/** 从节点的 outputs 里取媒体产物（有就是有，没有返回 null）。
 *
 * `ref` 是 asset 节点的输出端口（角色/场景定妆图）：它承载的也是图片，
 * 所以一并当作 image 处理 —— 否则资产卡永远显示不出自己的那张图。
 */
export function mediaOf(node) {
  const outputs = node?.outputs || {};
  for (const port of ["image", "video", "audio", "ref"]) {
    const value = outputs[port]?.value;
    if (value?.path) {
      return { port: port === "ref" ? "image" : port, path: value.path, mock: !!value.mock };
    }
  }
  return null;
}

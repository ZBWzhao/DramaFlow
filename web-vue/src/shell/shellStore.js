/* 系统外壳状态：决定「先看到什么」。
 *
 * 三层结构，画布不再是入口：
 *   home    项目列表 / 新建项目   ← 打开系统看到的第一屏
 *   studio  制片台（剧本·资产·制作·预览）← 制作者真正工作的地方
 *   canvas  画布（结构视图）      ← Agent 编排的内部表示，人偶尔来看一眼
 *
 * 这个顺序是产品判断，不是导航细节：谁在入口，谁就是主体。
 */
/* 跳转全部走 vue-router（shell.view 是迁移前的内部状态机，已无人消费——
 * 之前这里只写字段不推路由，画布顶栏的返回/制片台/素材库全是死按钮）。 */
import { reactive } from "vue";
import { router } from "../router.js";
import { studio } from "../studio/studioStore.js";

export const shell = reactive({
  view: "home", // home | studio | canvas
  projects: [],
  loading: false,
  error: "",
  newOpen: false,
});

export async function loadProjects() {
  shell.loading = true;
  shell.error = "";
  try {
    const res = await fetch("/api/studio/projects");
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `读取项目失败（${res.status}）`);
    shell.projects = data.projects || [];
  } catch (err) {
    shell.error = err?.message || String(err);
  } finally {
    shell.loading = false;
  }
}

/** 进入一个项目：把画布名交给制片台，由它自己拉派生视图。 */
export function openProject(canvas) {
  if (!canvas) return;
  studio.canvas = canvas;
  studio.tab = "script";
  studio.project = null;
  studio.script = null;
  studio.segments = [];
  studio.assets = { character: [], scene: [], prop: [], style: [] };
  studio.preview = null;
  studio.activeSegmentId = "";
  studio.detail = null;
  studio.produceJob = null;
  studio.error = "";
  shell.view = "studio";
}

export function goHome() {
  router.push("/");
}

/** 画布降级为「结构视图」：从制片台主动进来，看完能回去。 */
export function openCanvas() {
  const name = studio.canvas;
  router.push(name ? `/project/${name}/canvas` : "/");
}

/** 切到制片台（制作页）。canvasName 兜底：直接从 URL 进画布时 studio.canvas 还没绑上。 */
export function openStudioForCurrent(canvasName) {
  const name = canvasName || studio.canvas;
  if (!name) return;
  router.push(`/project/${name}/production`);
}

/** 切到制片台并指定页签对应的路由（素材库等入口用）。返回是否成功。 */
export function openStudioTab(tab, canvasName) {
  const name = canvasName || studio.canvas;
  if (!name) return false;
  studio.tab = tab;
  const path = (
    { script: "script", production: "production", wall: "wall", assets: "assets", preview: "preview", dashboard: "dashboard" }[tab]
    || "production"
  );
  router.push(`/project/${name}/${path}`);
  return true;
}

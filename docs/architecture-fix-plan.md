# AI 短剧无限画布 · 架构诊断与修复方案

> **审阅对象**：`toonflow-py-lab`（基线 commit `fb2fa9d` + 当前工作区未提交改动）
> **技术栈实况**：Python/FastAPI 单进程应用 + SQLite/Postgres（可选）+ Vue 3；画布默认是自研渲染引擎（`web-vue/src/canvas2/`），`@vue-flow` 版本只在 `?legacy=1` 时挂载。
> **审计方式**：只读静态审阅（后端 2.2k 行 `server/app.py`、2.4k 行 `server/studio.py`、1.25k 行 `core/canvas.py`、任务/媒体/合成模块；前端 823 行画布引擎、1589 行 store、738 行旧 Vue Flow 版）。**未运行应用、未跑测试**，结论以代码事实为准，个别运行时行为标注为「待验证」。
> **本文只给逻辑与架构设计，不含实现代码。**

---

## 0. 执行摘要

### 0.1 一句话结论

这个项目**不是「缺功能」，而是「三套状态、两条链路、一次写全量」**：
画布节点状态存在 `canvases.content` 的 JSON 里，任务状态存在 `media_jobs` 表里，制片台的生成状态存在进程内存里；同一件事（生成一个镜头）有画布链路和制片台链路两个实现；而几乎每个交互都触发整份画布 JSON 的全量重写 + 全量刷新。功能骨架已经相当完整（端口协议、审批冻结、幂等任务、租约、防串代、takes 版本都做了），但**收益链路（付费生成）和编辑链路（拖拽/连线/删除）缺少一致性保障**，导致「用户一拖动，生成结果就可能丢」。

### 0.2 先肯定：这 6 件事已经做对了，修复时不要推倒

| 能力 | 位置 | 价值 |
|---|---|---|
| 节点端口类型协议（11 类节点 / STRING·IMAGE·VIDEO·AUDIO） | `core/canvas.py:37-93` | 连线合法性的唯一真相，前端只做预检 |
| 服务端连线校验（类型/跨故事/自环/成环/重复边幂等） | `core/canvas.py:453-499` | 非法连线在服务端就被 400 挡住，不是靠前端自觉 |
| 生成前的「计划 → 审批 → 冻结 payload → 入队」 | `server/app.py:1016-1129` | 生成参数可审计、可按审批哈希重放，成本可控 |
| 任务幂等 + 租约 + `unknown` 禁自动重试 + 防串代 `generation_id` | `core/media_job_store.py:337-493`、`server/app.py:1455-1670` | 这是同类产品普遍缺失的部分，方向完全正确 |
| 画布 revision 乐观锁（文件侧车锁 + Postgres CAS 双实现） | `core/canvas_repository.py:111-131/197-230` | 多端/Agent 并发编辑的正确底座 |
| takes 版本历史 + 当前版指针 | `server/app.py:1618-1641`、`1965-2000` | 节点内「切版本」的数据基础已具备 |

### 0.3 最高优先级 10 条（按「丢钱/丢数据/阻断」排序）

| # | 问题 | 一句话后果 |
|---|---|---|
| P0-1 | Worker 回写画布无 CAS 重试（`server/app.py:1506/1518/1533/1542/1589/1643` 均为裸 `save`） | 生成 30 分钟里用户拖一下节点，产物写不回去，费用白花、节点永久 error |
| P0-2 | 所有写接口的 `revision` 可为 `None` 即放行（`server/app.py:277-279`） | 不带 revision 的客户端（Agent/脚本/漏传的前端）永远 last-write-wins，静默覆盖别人的编辑 |
| P0-3 | 拖拽不写回本地坐标（`web-vue/src/store.js:236-248` 只更新 revision） | 松手后卡片弹回原位；下一拖以旧坐标为基准，位移累积丢失 |
| P0-4 | 「生成」是同步阻塞调用，无取消/超时/单飞（`store.js:357-430`） | 按钮卡住十几分钟、可重复点击产生双任务双计费、页面只能刷新 |
| P0-5 | 视频节点被塞进 `<img>` 播放（`canvas2/NodeCard.vue:105-112`） | 视频卡是破图，播放键无 handler，也没有懒加载/海报图 |
| P0-6 | 画布没有「删除节点」接口与交互 | 画布只增不减；节点右键的删除项直接注明「服务端暂无删除接口」 |
| P0-7 | 生成节点「无连线时按自身提示词生成」不成立（`core/canvas.py:1083-1110`：`ready=bool(prompt_text)` 且 prompt 只取入边文本，忽略 `data.prompt`） | 单卡生成本该是最常用路径，现在要么不能生成，要么用错提示词 |
| P0-8 | 审批先消耗、后入队，非事务（`server/app.py:1053-1105`） | 中途冲突/异常时审批已作废、任务未入队，用户既没生成也无法重试，且不知道发生了什么 |
| P0-9 | 画布读不到「新式」提示词正文：`read_text` 只认 `content_path`/`outputs["text"]`（`core/canvas.py:639-649`），S5 后的正文在 `data.prompt`/`outputs["prompt"]` | 画布生成/导出拿到空提示词；同时 `canvas.validate` 仍要求 prompt 有 `content_path` 且 ≥500 字（`canvas.py:800-810`），新画布体检必失败 |
| P0-10 | 画布生成硬编码尺寸与画幅（`server/app.py:1568` 的 `1024x576`/`1280x720`，前端 `store.js:365-368` 的 `16:9/720p`），不读项目比例与分镜时长 | 竖屏短剧（9:16）从画布出片会变成横屏，分镜时长被丢弃 |

### 0.4 与用户五个板块的判定速览

| 板块 | 判定 |
|---|---|
| 一、画布基础设施（缩放/平移/小地图/框选/对齐/分组/Undo/快捷键/右键） | 缩放平移框选右键 = 半实现；小地图/对齐/分组/Undo/Redo/删除/复制粘贴 = **缺失** |
| 二、连线与数据流（端口校验/Payload 组装/多模态解析） | 端口校验 = 已实现（服务端严格、前端弱）；Payload 组装 = 半实现（角色/槽位/顺序缺失）；多模态解析 = 半实现（无音频、无首尾帧、缺参考文件静默降级） |
| 三、短剧生成与节点内交互（生成分支/内联输入/首尾帧/角色卡） | 内联输入 = 半实现（有输入框但不参与生成、无失焦保存）；首尾帧 = 仅制片台有；角色卡 = 靠 asset 节点+`{{ref N}}` 半实现，无一致性保障 |
| 四、异步与状态流转（状态机/队列/轮询/历史版本） | 队列 = 已实现（自研，非 Celery）；生命周期 = 半实现（缺 queued/failed 的节点级表达与重试）；通信 = 轮询；历史版本 = 半实现（takes 有，切版不写 revision、合成/上传不进版本） |
| 五、性能与持久化（懒加载/自动保存/素材库/导出） | 懒加载 = 缺失（视频压根没加载）；持久化 = 半实现（逐动作全量重写，无字段级 patch）；素材库 = 仅数据表无读取；导出 = 仅 Markdown 提示词 |

---

## 一、现有架构与逻辑缺陷清单（按优先级）

### 1.1 优先级定义

- **P0｜阻断/丢钱/丢数据**：不修则核心业务不可用，或会造成付费产物/用户编辑丢失。
- **P1｜体验断裂/语义错误**：能用但结果不可信、交互反人性，用户会反复踩。
- **P2｜性能与工程债**：数据量上去会垮，或安全/审计缺口。
- **P3｜产品增强**：决定上限的缺失能力（分组、协作、导出等）。

### 1.2 P0 缺陷（10 条，必须先修）

**P0-1｜Worker 回写画布无 CAS 重试 → 付费产物被一次拖动丢掉**
> 状态：已修复（`docs/p0-repro-verify-plan.md` §8）。新增 `_mutate_canvas_with_retry`（`server/app.py:301-334`），Worker 6 处写回改为"重载 → 只重放本任务增量 → 重试"，takes/坐标/正文不再互相覆盖。
- 现象：视频生成期间用户拖动任意节点 → 画布 `revision` 变化 → worker 完成时 `save` 抛 `CanvasConflictError`，整个 `_execute_media_job` 异常退出，任务被标记 failed（`core/media_job_store.py:482-486`），provider 已扣费但节点停在 error/旧值。
- 根因：`server/app.py:1506/1518/1533/1542/1589/1643` 全部裸调 `registry.canvas_repository.save(canvas)`；而同文件的合成路径 `_save_composition_state` 有 4 次 CAS 重试（`server/app.py:1285-1299`）。同一系统两种写入策略。
- 修复方向：把「读-改-写 + CAS 重试 + 冲突合并规则」收敛到 repository 层一个写方法，worker 只调它；冲突时合并「只属于本次生成的字段」（status/outputs/takes/current_take_id），不整体覆盖。

**P0-2｜revision 可选 → 乐观锁形同虚设**
> 状态：已修复（`docs/p0-repro-verify-plan.md` §7）。内容写入路径改为强制 revision（缺失 400、过期 409）；`position / connect / nodes 创建更新 / run / compose` 等其余写接口仍走宽松版，留给 P1 收口。
- 现象：任何不带 `revision` 的写请求直接 200 覆盖；`select_canvas_take`（`1965`）和 `delete_canvas`（`1829`）干脆没有 revision 参数。
- 根因：所有请求模型 `revision: int | None = None`（`server/app.py:181/212/221/229/237/247/256/2025`），`_require_revision`（`277-279`）在 None 时 return。
- 影响：双端/人+Agent 同时编辑必丢数据，且无告警（前端 canvas2 目前是传的，但 Agent 工具、脚本、将来第三方客户端不一定传）。
- 修复方向：写契约改为 `If-Match` 必填（缺失即 409/400）；读接口返回 `revision` 与 `etag`；冲突响应携带「服务端当前版本摘要」供前端合并。

**P0-3｜拖拽不回写、松手回弹**
- 现象：`NodeCard` 拖动只用本地 `offset` 渲染（`canvas2/NodeCard.vue:41-63`），松手 emit `move`；`store.moveNode` 只更新 `canvasServerRevision`（`web-vue/src/store.js:236-248`），不写回节点坐标也不 reload；`CanvasBoard.onMove` 也不本地更新（`CanvasBoard.vue:560-578`）。下一次拖动以旧坐标为基准，位移累积丢失。
- 修复方向：pointerup 时先本地提交（乐观更新）再发请求；失败回滚并提示；组拖用一次批量接口，避免 N 次串行写 + N 次 revision 冲突。
- 附带：多选拖动时其余节点只在松手后一次性发请求，拖动过程不跟手。

**P0-4｜生成是同步阻塞调用，无取消/超时/单飞**
- 现象：`runCanvas` 内先 `POST /approvals` 再 `POST /run`，随后 800ms 轮询等 job 结束（`store.js:357-430`），整个按钮 await 到任务完成；无 AbortController、无超时、无取消端点；重复点击会产生新审批 + 新 job（`server/app.py:1064-1065` 每次新 `approval_id`）。
- 修复方向：生成即刻返回 `job_id`，前端只订阅任务事件；同 canvas 单飞（一次只允许一个「生成会话」）；补 `POST /api/media/jobs/{id}/cancel`；节点按 job 状态派生 UI。

**P0-5｜视频节点渲染成破图**
- 现象：`NodeCard.vue:105-112` 只渲染 `<img>`，视频 mp4 路径塞进 img；播放键是装饰（无 handler）；时长硬编码「00:04」；无 poster、无 `preload`、无懒加载。
- 影响：用户看不到生成结果，无法判断成败；旧 Vue Flow 版反而有 `<video preload=metadata>`（`components/ToonNode.vue:220-236`）。
- 修复方向：`<video preload="none" :poster>`，进入视口/悬停才 `src`；后端在生成完成时抽帧产 poster；播放用自定义浮层（不进全屏）。

**P0-6｜画布没有删除节点能力**
- 事实：后端无 `DELETE /api/canvas/nodes/{id}`（全路由表见 `server/app.py`：删除仅有画布级 `1829`）；`Canvas.remove_nodes`（`core/canvas.py:501`）只有代码没有出口；前端节点右键删除项被禁用并注明「服务端暂无删除接口」（`CanvasBoard.vue:609-628`）。
- 影响：画布只增不减，A/B 对比、废稿清理、无限画布的核心体验不成立。
- 修复方向：新增节点删除（级联删边 + `takes`/资产引用计数 + 软删标记 + revision）；前端支持 Delete 键、右键删除、多选删除、撤销恢复。

**P0-7｜「无连线时按自身提示词生成」不成立（用户问题三的核心分支）**
> 状态：已修复（`docs/p0-repro-verify-plan.md` §10）。新增 `Canvas.resolve_prompt`：来源优先级为「显式 prompt 节点 > 其他文本输入 > 卡片自身 `data.prompt`」，job 下发 `prompt_source` / `prompt_overridden` / `ready_reason`；`validate` 不再要求生成卡必须连 prompt 口，只在三种来源都为空时报问题。单卡文生视频已能端到端出片（`tests/p0/test_p0_07_generation_prompt.py`）。
- 现象：`Canvas.generation_jobs`（`core/canvas.py:1083-1110`）里 `prompt_text` 只从 `text_inputs`（上游 prompt/text 节点正文）取第一个非空；`ready = bool(prompt_text.strip())`；**完全不读节点自身 `data.prompt`**。而 `image_gen/video_gen` 节点自带 `data.prompt`（人可在卡下输入框写，`app.py:1928-1933` 也确实写进去了），但生成时被忽略。
- 影响：单卡「文生图/文生视频」要么因 `ready=false` 被跳过（任务 failed「没有输入提示词」），要么用上游第一段文本、丢掉人写的这一张的要求。
- 修复方向：定义生成节点的「提示词来源优先级」：显式入边 prompt 节点 > 节点自身 `data.prompt` > 报错；`ready` 的判定改为「按生成模式的最小必填集」，并把缺什么写进 `ready_reason`。

**P0-8｜审批先消耗后入队，非事务**
> 状态：已修复（`docs/p0-repro-verify-plan.md` §9）。`consume` 与 `enqueue` 包成"提交点"：入队失败 → 回滚节点激活标记 + 把审批退回 `pending` + 503「请重试」；审批哈希与 job_id 去掉 `canvas_revision`（绑定执行计划），刷新后可用同一份审批重试，且重放落到同一个 job_id。
- 现象：`run_canvas` 顺序为 `approvals.consume`（`1053-1062`）→ 重新 load + `_require_revision`（`1098-1104`）→ `media_jobs.enqueue`（`1105`）。中间冲突或异常 → 审批已作废、任务未入队，无补偿。
- 修复方向：事务内完成「校验 revision → 冻结 plan → 写 job → 消费 approval → 写 node 状态 → 写 outbox」；失败整体回滚；重复提交按幂等键返回同一个 job。

**P0-9｜画布读不到新式提示词正文，且体检规则与 S5 冲突 → 生成与导出都拿不到正文**
> 状态：已修复（`docs/p0-repro-verify-plan.md` §10）。`read_text` 改为单入口回退链「content.md 文件 → `outputs.prompt` → `outputs.text` → `outputs.shots` → `data.prompt`」，生成/导出/体检/API 全走它；`validate` 去掉 `content_path` 与 ≥500 字硬要求（长度降级为建议，不再算问题）；`GET /api/canvas` 的 `content` 字段改为无条件 `read_text`，卡片详情能看到新式正文。
> 审查补充（同一批）：① 两个写侧（画布 `/text` 与制片台 `/studio/segments/{id}/prompt`）改为**只写本类型自己的槽位并清掉另一个槽位**，否则 `outputs.prompt` 的旧值会永远盖住 `outputs.text` 的新值（新增回归用例 `test_p0_09_alternating_writers_keep_latest_body`，旧代码 RED 已复现）；② `server/studio.py:_node_text` 改为委托 `Canvas.read_text`，删掉它的第二套顺序（旧实现不认 `data.prompt`，导致同一片段在画布有正文、在制片台为空，回归用例 `test_p0_09_studio_segment_view_reads_same_body`）。
- 现象：`Canvas.read_text`（`core/canvas.py:639-649`）只从 `content_path` 文件或 `outputs["text"]` 取值；而 S4/S5 之后片段正文写在 `data.prompt`，人改写的写在 `outputs["prompt"]`（`server/studio.py:702-707`）。画布生成链路（`generation_jobs` → `text_inputs` → `read_text`）与画布导出（`render_all`）因此读到空串。
- 叠加冲突：`Canvas.validate`（`core/canvas.py:800-810`）仍要求 prompt 节点有 `content_path` 且正文 ≥500 字，而 prompting 阶段已改成「服务端按分镜渲染正文」——新画布跑体检必然报错，生成前的 lint 也可能被卡住。
- 修复方向：正文读取统一成「单函数、三态回退」（`outputs.prompt` → `content_path` → `data.prompt`）；`validate` 拆成「结构校验」与「内容质量建议」两级，内容长度降级为 warning；prompt 节点明确「正文来源」字段（rendered_by / body_source）。
- 影响：这是「视频节点读不到提示词」的直接根因之一，与 P0-7 一起构成生成分支不可用的双重原因。

**P0-10｜画布生成不读项目画幅与分镜时长 → 竖屏短剧出横屏**
> 状态：已修复（`docs/p0-repro-verify-plan.md` §10）。新增 `Canvas.generation_params`（节点 `duration/ratio/quality/size` → 分镜镜头时长 → `meta.project` 的 `aspect_ratio/quality` → 请求默认值），job 下发 `params.{duration,ratio,resolution,size}`；审批计划合并请求参数后**冻结进 job**，Worker 不再按 kind 硬编码 size；前端 `runCanvas` 从 `state.canvas.meta.project` 取画幅/画质。9:16 + 720P 出片已验证为 720x1280（`tests/p0/test_p0_10_media_params.py`）。
- 现象：`runCanvas` 固定 `duration:5 / resolution:"720p" / ratio:"16:9"`（`web-vue/src/store.js:365-368`）；`server/app.py:1564-1575` 把 size 硬编码为图片 `1024x576`、视频 `1280x720`。`generation_jobs` 不读取 storyboard 的 `shots[].duration_s`。
- 影响：9:16 的短剧项目从画布生成会得到 16:9 素材，合成时再裁切/黑边；分镜时长（3-8 秒校验，`core/canvas.py:126-175`）不进入生成，节奏完全靠默认 5 秒。
- 修复方向：参数解析优先级「节点 params → 分镜镜头时长 → 项目 defaults（ratio/resolution/duration）」，provider 侧只做能力 clamp；合成阶段的总时长校验使用同一来源。

### 1.3 P1 缺陷

**画布基础设施与交互（用户板块一）**
1. **小地图缺失**：canvas2 无 MiniMap；旧 Vue Flow 版有 `<MiniMap>`（`CanvasPanel.vue:488-498`），但 `@vue-flow/controls` 只引入依赖与死样式（`canvas.css:388-400`），全仓无 `<Controls>`。
2. **对齐/吸附缺失**：无网格吸附、无智能参考线、无等距提示；旧版 `snap-grid=16` 但新版未用。
3. **节点分组（Frame）缺失**：无父子坐标、无折叠、无批量运行，短剧「一场戏一组」的编辑模型无法表达。
4. **全局撤销/重做缺失**：全仓无 undo/redo 栈（仅 takes 的版本历史，语义不同）。
5. **快捷键缺失**：canvas2 只有 Space/Ctrl+K/Esc（`CanvasBoard.vue:671-689`），无 Delete、Ctrl+A、Ctrl+C/V、Shift 加选；且 Ctrl+K/Esc 在 textarea 内照样触发（无 `isEditing` 守卫），Esc 会连带销毁输入框草稿。
6. **边不可操作**：无法选中边、删除边、改接端口；`store.deleteEdge`（`store.js:326-339`）在 canvas2 无入口；`matchPorts`（`canvas2/CanvasBoard.vue:511-519`）只取「第一个同类型端口」，两个 IMAGE 输入口（ref / video_ref / first_frame / last_frame 类）无法区分。
7. **右键菜单越界**：`ContextMenu` 用 `clientX/Y` 直接定位，不做视口 clamp（`CanvasBoard.vue:613-643`；旧版有 clamp，`CanvasPanel.vue:123-129`）。
8. **首屏视口抖动**：`watch([canvasName, nodes.length])` 在每次 loadCanvas 后都触发 fit→focusFirst（`CanvasBoard.vue:209-229`），生成轮询 800ms 一次就是 800ms 跳一次画面。
9. **resize 不自适应**：可见区剔除只依赖 `view`，无 `ResizeObserver`（`CanvasBoard.vue:73-84`）；`fit` 只看最大坐标，负坐标场景出错。
10. **死按钮**：DockBar 5 个按钮无行为；Hero 新建胶囊丢弃 kind，全建 `image_gen`（`CanvasBoard.vue:772`）。
11. **上传无校验**：无类型/大小限制，取消选择后 `pendingUpload` 残留。

**连线与数据流（用户板块二）**
12. **多上游无「角色/槽位」语义**：`generation_refs`（`core/canvas.py:581-611`）按边插入顺序 + 上游 prompt 的媒体边合并去重后输出一个扁平列表，`{{ref N}}` 的 N 就是这个顺序；一旦增删连线，N 全部漂移，历史提示词里的 `{{ref 1}}` 会指向别的图。
13. **生成时提示词未展开**：画布链路直接把 prompt 节点正文（含 `{{ref N}}`）交给 provider；而制片台链路有 `_materialize_prompt`（`server/studio.py:1093-1123`）会展开成「参考素材：图片N = 名称 + 图片N名称」。两条链路对同一提示词的处理不一致（画布链路模型会看到字面量 `{{ref 1}}`）。
14. **参考文件存在性不校验**：`TFRouterProvider._media_url`（`core/media.py:278-297`）对本地不存在的文件返回 None，`:337` 过滤后**静默丢弃**参考图 → 表现为「有连线但降级成纯文生图」，不报错。
15. **音频未进入生成**：provider 无音频生成；画布 `generation_jobs` 也不收集 AUDIO 入边（`text_inputs` 只跳过 IMAGE，AUDIO 会被当文本读，`read_text` 返回空后被丢弃）。「音频驱动口型/配乐」不存在。
16. **video_ref（视频→视频生成）类型合法但语义没接**：`NODE_SPEC.video_gen.in` 有 `video_ref: VIDEO`（`core/canvas.py`），但 payload 只把 refs 当图片参考，provider 的 `reference_image`/`first_frame` 不区分来源是视频；「上一镜尾帧续写」断链。
17. **节点级参数被忽略**：`runCanvas` 硬编码 `duration:5 / resolution:"720p" / ratio:"16:9"`（`store.js:365-368`）；provider 侧 size 也硬编码 `1024x576`/`1280x720`（`server/app.py:1564-1575`）。分镜时长（storyboard 的 `duration`）不进入生成。
18. **前端预检弱**：非法连线只 toast，无落点高亮/原因说明；无 `isValidConnection`；拖线落点靠 `elementFromPoint('.node')`，落不到具体端口。

**短剧生成与节点内交互（用户板块三）**
19. **内联输入框「写了不生效」**：PromptBox 对生成卡写 `data.prompt`（`app.py:1928-1933`），但生成不读它（见 P0-7）；且只在 Enter/↑ 时保存，失焦不保存，切卡即丢草稿。
20. **生成状态被全局冒充**：`NodeStudio` 用全局 `canvasBusy` 表示「生成中」（`NodeStudio.vue:64`），任一任务时所有生成卡都显示生成中；`CanvasBoard` 根本不映射 node.status（`CanvasBoard.vue:109-137`）。
21. **无进度与可取消**：排队中 → 生成中 → 失败/重试的完整生命周期只有「任务表有状态」，节点 UI 没有 queued、没有百分比、没有错误原因、没有单卡重试入口。
22. **首尾帧仅制片台有**：`generate_keyframe` / 首帧模式在 `server/studio.py` 实现（`_default_video_model`、`first_frame` 分支、`ProductionView.vue:187-264`「生成首帧/重画首帧」「这镜以它驱动」），画布节点的 `video_gen` 没有 first_frame/last_frame 槽位，也没有 UI。
23. **角色卡/参考图无一致性机制**：只有 asset 节点的 `consistency_tags` + `ref` 端口 + `{{ref N}}` 文本约定；无「角色卡」实体（锁定脸/服装/风格词）、无 seed 锁定、无「本场所有镜头必须引用同一角色卡」的校验，跨镜头长相一致全靠提示词运气。
24. **画布/制片台双轨生成**：同一节点数据被两套链路写（见 P1-26），产物语义（takes/mode/model）不一致。

**补充（制片链路审计的额外结论，均验证于代码）**
- **首尾帧只有一半**：provider 侧 `last_frame` 已实现（`core/media.py:344/372`），但全仓**无调用方**；「上一镜尾帧 → 下一镜首帧」的接力（drama_craft §5 的意图）未实现。制片台只有首帧 keyframe（`ProductionView.vue:187-264`）。
- **一致性机制只有「参考图 + 文本替换」**：`consistency_tags` 只进资产图提示词（`server/studio.py:1599`）与 `asset_desc`（`core/canvas.py:651-668`），**不进视频/图片请求**；请求体无 seed/LoRA/persona（未确认有 seed 参数）；`subjects[].id` 不贯通正文（正文按名字/序号），`asset_problems` 不校验名字唯一，重名会静默错绑（`core/canvas.py:178-197`）。
- **分镜时长只用于校验**：`duration_s` 仅做 3-8 秒校验（`core/canvas.py:126-175`）；总时长阈值 ±0.01s（`canvas.py:840-848`），`target_duration` 不参与生成；成片时长靠 ffprobe 累加（`server/app.py:1302-1455`），没有全片总时长约束。制片台有 `_infer_duration`（`studio.py:225/360`），画布没有——两条链路对「这一镜多长」的答案不同。
- **制片台生成链路没有审批、幂等、崩溃恢复**：`_jobs/_tasks` 在进程内（`studio.py:47-48`），无 approval、无 invocation/args_hash、无 `operation_callback`（`core/media.py:398-400`），进程崩溃后远端任务无法 resume，只能重新计费。它与画布链路写同一份 `node.data.takes`，但规则不同：不截断 30 版、不登记 `media_assets`、`shot_no` 恒为 1（`studio.py:1163-1218`）。
- **制片台 assemble 的 timeline 无声无字幕**：`assemble` 建出的 timeline 没有音频输入、没有 subtitle 输入（`studio.py:775-840`）；只有画布 `compose` 路径会 TTS 合成并写 SRT（`server/app.py:1302-1455`）。
- **流水线阶段从未执行完**：`ProductionRunner` 只跑到 `waiting_approval`（`core/production.py:452-456`），generating/composing/final_review/completed 没有执行方；`meta.production.renders` 全仓无写入方、`export_done` 只读（`studio.py:1945-1979`）→ 流水线里的「导出」永远显示 pending。

**异步与状态（用户板块四）**
25. **无 Celery/Redis 前提下的默认最弱配置**：默认 `InMemoryMediaJobStore`（`server/app.py:64`），API 进程重启任务全丢、节点永久 generating；Redis/Postgres 需显式 env 才启用。单 worker 串行（`core/media_job_store.py:466-493`），一个 30 分钟视频阻塞所有画布所有节点。
26. **任务与节点状态双写、无对账**：job 状态在任务表，node 状态在 canvas JSON，无事务无补偿；`/api/media/jobs/{id}` 只在 failed/unknown 时顺手补 production stage（`server/app.py:1702-1717`），从不反向修画布。
27. **无事件推送**：任务/画布变更不发事件（`outbox_events` 只在 Postgres 会话仓库下启用，`server/registry.py:117-119`）；前端纯轮询，且每轮 `loadCanvas()` 全量刷新（`store.js:394-430`）。
28. **同一节点可并发两条生成**：节点级互斥只靠 `invocation` 的 args_hash（`app.py:1481-1499`），参数不同就互斥不了；`active_generation_id` 后写覆盖前写，先跑的会被 `stale_discarded`，但钱已经花了。
29. **切版无 revision**：`POST /nodes/{id}/take`（`app.py:1965-2000`）不校验 revision，与画布其他写接口不一致。
30. **版本语义不完整**：takes 只保留 30 条且存在 canvas JSON 里（每生成一次全量重写画布）；合成/上传覆盖 outputs 不写 takes（`app.py:1391-1404`、`2090`）；资产文件永不回收。

**持久化与安全（用户板块五 + 工程）**
31. **逐动作全量重写**：每次拖拽/连线/改字都是「load 全量 → 改一个字段 → 写全量」，无字段级 patch、无 debounce、无批量事务。
32. **`update_node` 可任意写 `data`**（`app.py:1886-1896`）：前端可绕过状态机改 `status`、`content_path`、`takes`。
33. **两处任意路径写（路径穿越）**：`/api/canvas/export` 的 `WORKSPACE / req.path`（`app.py:2125-2128`，可 `../../`）与 `/nodes/{id}/text` 的 `canvas.root() / data["content_path"]`（`app.py:1935-1939`，content_path 可被 update_node 写入）。项目已有 `_register_media_asset` 的 `relative_to(WORKSPACE)` 防护（`app.py:70-78`），这两处没用。
34. **全站无鉴权**：`/assets` 静态挂载（`app.py:2210`）暴露全部产物；`media_assets` 无索引、无分页、无读取接口。

### 1.4 P2 / P3 缺陷

**P2**
- `get_canvas` 每个节点同步读 content.md（N+1，`app.py:1853-1858`）；`list_canvases` 逐个全量 load（`1803-1811`）；upload 把 100MB body 读进内存再同步写（`2035-2086`）。
- 轮询先清 `runResult` 再写回，进度闪（`store.js:198`）；bootstrap `Promise.all` 一个失败全挂（`1529`）。
- `NodeCard` 在 window 注册 pointermove 无卸载（`NodeCard.vue:37-38`）；边拖 `pointerup {once}` 未清理。
- `@pointerleave="onPointerUp"`（`CanvasBoard.vue:718`）鼠标移出画布即结束框选/平移。
- 侧栏「所有评级」是假交互（`SidebarPanel.vue:42-47`）；`TopBar` 组件从未被引用；边 `:key="index"`。
- 无节点级费用预估、无审计日志（审批只有哈希）。

**P3**
- Frame 分组 / 折叠 / 场次视图。
- 多人协作（事件流 + 光标 + 锁）。
- 素材库（Asset Library）页面：现在的 `media_assets` 只写不读（`app.py:70-92`），左栏只有节点列表（`SidebarPanel.vue:19`）。
- 画布 JSON 导出/导入（目前导出只有 Markdown 提示词，`app.py:2117-2130`）；选中节点导出为图片/成片。
- 时间轴/成片：`timeline` 节点 + `compose` 已有（`app.py:1200-1299`），但画布内没有可视化剪辑。

---

## 二、核心数据结构设计建议

### 2.0 六条设计原则

1. **真假分层**：图结构的真相是「画布文档」，执行真相是「任务 + 事件」，资产真相是「内容寻址的 Asset」。不要用 `node.status` 兼任任务状态，不要用 `node.outputs` 兼任资产索引。
2. **端口契约单一来源**：`NODE_SPEC` 留在服务端（唯一真相），前端通过 `GET /api/canvas/spec` 获取并缓存；连边校验抽成**双端同源的纯函数规则**，前端用于落点高亮与预检，服务端仍是最终裁决者。
3. **节点数据分区**：`data`（业务参数，持久化、参与生成）、`runtime`（服务端投影，只读、事件驱动）、`ui`（选中/悬停/折叠/编辑态，不落库、不进 diff）。这样拖拽、悬停、编辑不会产生文档写入，也不会触发全量刷新。
4. **只追加、不覆盖**：`takes`、`JobEvent`、审批记录 append-only；`outputs` 永远只是「当前版的指针」，不是数据本体。任何「生成结果丢失」都可以从 take + asset 恢复。
5. **事件驱动投影**：节点状态由任务事件投影到画布（worker 写事件，投影器写画布），前端只消费事件、不参与状态机；前端任何写 `status` 的能力都应被移除（现状 `update_node` 可写任意 data，`app.py:1886-1896`）。
6. **乐观并发 + 字段级变更**：写接口必带 `If-Match: revision`；提交的是 **ops 列表**而不是整份文档；冲突返回服务端当前摘要与冲突字段，前端合并后重试，而不是静默丢弃（现状 `store.js:201-204`）。

### 2.1 端口类型系统与连边规则

**端口定义（PortSpec）**——这是修复「非法连线」问题的核心。现状端口只有 `name → type`（`core/canvas.py:37-93`），缺少语义角色与基数，导致「两个同类型输入口无法区分」「多上游顺序漂移」。建议每个端口显式声明：

| 字段 | 含义 | 例 |
|---|---|---|
| `key` | 端口名（存边用） | `first_frame` |
| `type` | 数据类型枚举 `STRING/IMAGE/VIDEO/AUDIO/TIMELINE` | `IMAGE` |
| `role` | 语义角色（决定后端映射到模型参数） | `first_frame / last_frame / reference / style / audio / music / prompt / context` |
| `cardinality` | `single`（最多一条边）/ `multi`（有序列表） | `single` |
| `required_for` | 哪些生成模式必填 | `["image2video"]` |
| `accepts` | 允许的 source 端口类型（默认同类） | `["IMAGE"]` |

**建议的端口矩阵（关键节点）**

| 节点 | 输入端口（type / role / 基数） | 输出 |
|---|---|---|
| `image_gen` | `prompt` STRING·prompt·single（可选，缺省用自身 data.prompt）；`ref` IMAGE·reference·multi；`style` IMAGE·style·single | `image` IMAGE |
| `video_gen` | `prompt` STRING·prompt·single（可选）；`first_frame` IMAGE·first_frame·single；`last_frame` IMAGE·last_frame·single；`reference` IMAGE·reference·multi；`video_ref` VIDEO·video_ref·single；`audio` AUDIO·music·single | `video` VIDEO，`last_frame` IMAGE（抽帧产物，供下一镜续写） |
| `tts_gen` | `text` STRING·prompt·single；`voice` AUDIO·reference·single | `audio` AUDIO |
| `timeline` | `clip` VIDEO·multi（有序）；`audio` AUDIO；`subtitle` STRING | `video` VIDEO（成片） |
| `asset`（角色卡/场景卡） | `desc` STRING；`image` IMAGE·reference | `ref` IMAGE（消费时注入）；`spec` STRING |
| `prompt` | `ref` IMAGE·reference·multi；`video_ref` VIDEO·multi；`ctx` STRING | `text` STRING |

**连边校验规则（服务端裁决，前端预检同源）**

- R1 两端节点存在且属于同一画布、同一 `story_id`（现状已做，`canvas.py:453-499`）。
- R2 端口存在、方向正确（输出→输入）。
- R3 `type` 兼容（现状 `_compatible` 只做相等判断；建议升级为兼容表，例如 `VIDEO` 可进 timeline.clip 与 video_gen.video_ref，但不可进 image 输入）。
- R4 `cardinality=single` 的目标端口已有边时，返回「替换/拒绝」二选一（前端弹确认），而不是静默追加。
- R5 成环检测（现状 `find_path`，复用）。
- R6 重复边幂等返回已有边（现状已做，复用）。
- R7 生成前 lint（`/api/canvas/generation-plan` 已有雏形）：必填端口、必填参数、参考文件可读、上游产物已成功、模型能力匹配（时长/比例/首尾帧支持），逐条输出 `ready_reason`。

### 2.2 前端节点数据结构（NodeView）

```jsonc
{
  "id": "img_7f3a",
  "type": "video_gen",
  "label": "第 3 镜 · 雨夜巷口",
  "position": { "x": 1240, "y": 480 },
  "parent_id": "frame_scene_02",        // Frame 分组（P3，预留）
  "data": {                              // 业务参数：持久化、参与生成、可 diff
    "story_id": "story_01",
    "shot_no": 3,
    "prompt": "{{ref 1}}站在雨里回头，霓虹反光……",  // 提示词原文，允许 {{ref N}}
    "params": {
      "model": "Seedance 2.0 mini",
      "ratio": "16:9",
      "resolution": "720p",
      "duration": 5,
      "seed": null,
      "generate_audio": false
    },
    "consistency": { "tags": ["灰蓝士兵", "银发"], "character_ids": ["char_01"] } // 角色卡引用
  },
  "runtime": {                           // 服务端投影：只读，事件更新，不参与 PATCH
    "status": "running",                 // draft|ready|queued|running|succeeded|failed|cancelled|stale
    "status_detail": { "phase": "provider", "percent": 40, "message": "模型生成中" },
    "ready": false,
    "ready_reason": ["缺少首帧：first_frame 端口未连接"],
    "outputs": {
      "video": { "asset_id": "ast_9c", "url": "/assets/generated/v_9c.mp4",
                 "thumb_url": "/assets/generated/v_9c.thumb.jpg",
                 "poster_url": "/assets/generated/v_9c.poster.jpg",
                 "mime": "video/mp4", "duration_s": 5, "mock": false }
    },
    "takes": ["take_1", "take_2"],
    "current_take_id": "take_2",
    "active_generation_id": "gen_ab12",
    "problems": []
  },
  "ui": { "selected": true, "hovered": false, "editing": false }  // 仅前端，不落库
}
```

要点：
- `position` 走独立 patch（`PATCH /nodes/{id}/position`）且**必带 revision**，仍然允许组拖批量一次请求。
- `runtime` 不能通过 `update_node` 写；前端一切「生成中/成功/失败」的展示都从 `runtime.status` 派生，删除全局 `canvasBusy` 冒充逻辑（`NodeStudio.vue:64`）。
- `data.prompt` 与上游 prompt 节点是两个来源，按 2.5 的优先级解析，不再出现「写了不生效」。
- `ui` 状态与视口、面板开合一起进 `localStorage`（按 canvas key），避免每次 reload 重置。

### 2.3 连线数据结构（Edge）

```jsonc
{
  "id": "edge_01",
  "source": { "node": "asset_char_01", "port": "ref" },
  "target": { "node": "video_gen_7f3a", "port": "first_frame" },
  "kind": "data",                  // data | order | group
  "label": null,
  "created_by": "human",
  "created_at": "2026-10-19T12:00:00+08:00"
}
```

- **必须引入 `kind`**：短剧需要「镜头顺序」这种不进入生成 payload 的边（第 1 镜 → 第 2 镜），以及 Frame 的归属边。现状所有边都会被 `generation_refs` 当成数据依赖（`core/canvas.py:581-611`），顺序边会污染 payload。
- 边的身份是 `(source.node, source.port) → (target.node, target.port)`，不是「卡到卡」；这样两个 IMAGE 输入口才可区分。
- 多上游的顺序不由数组下标决定，而由「稳定引用键」决定（见 2.5 的 `ref_map`），避免增删连线导致 `{{ref N}}` 漂移。

### 2.4 画布文档与写入协议（CanvasDocument v2）

```jsonc
{
  "schema_version": 2,
  "name": "main",
  "project_id": "default",
  "story_id": "story_01",
  "revision": 128,                     // 乐观锁：单调递增
  "updated_at": "...",
  "updated_by": "human|agent",
  "meta": {
    "defaults": { "ratio": "16:9", "resolution": "720p", "duration": 5 },
    "script_id": "script_01",
    "model_prefs": { "image": "seedream-5.0-lite", "video": "Seedance 2.0 mini" }
  },
  "nodes": [ /* NodeDoc（同 2.2，去掉 ui） */ ],
  "edges": [ /* Edge（同 2.3） */ ]
}
```

**写入协议（关键改动）**：写接口从「整文档 PUT」改为「ops patch」：

```jsonc
POST /api/canvas/main/ops
Headers: If-Match: "128", Idempotency-Key: "..."
{
  "base_revision": 128,
  "ops": [
    { "op": "move_node", "node": "video_gen_7f3a", "position": { "x": 1250, "y": 490 } },
    { "op": "add_edge", "edge": { /* Edge */ } },
    { "op": "update_node", "node": "x", "patch": { "data.params.duration": 8 } },
    { "op": "remove_node", "node": "y" }
  ]
}
```

- 服务端在 revision CAS 内**按序应用 ops**，全成功才 bump revision；返回新 revision + 实际生效的 ops（含被合并/拒绝的项）。
- 拖拽用 300ms debounce 合并成一条 `move_node`（组拖合并成多条 ops 一次提交）。
- 冲突（revision 不符）：返回 409 + 服务端当前 revision + 冲突节点的服务端摘要；前端对「位置」类冲突自动重放，对「内容」类冲突提示用户，禁止静默 reload 丢弃（现状 `store.js:201-204`）。

### 2.5 生成 Payload 的三层结构（用户问题二的核心）

现状是「一层 payload 直接打到 provider」：前端把 prompt + refs 交给 `/run`，后端冻结后原样传给适配器（`server/app.py:1564-1575`）。建议拆成三层，职责清晰、可审计、可回放：

**第一层：GenerationIntent（前端 → 服务端，用户意图）**

```jsonc
{
  "canvas": "main",
  "revision": 128,
  "targets": ["video_gen_7f3a"],
  "mode": "image2video",            // text2image|image2image|reference2image|text2video|image2video|video2video|first_last_frame
  "overrides": {                     // 用户在生成面板里改的参数（可选）
    "model": "Seedance 2.0 mini", "duration": 5, "ratio": "16:9", "resolution": "720p", "seed": null
  }
}
```

前端**不组装 prompt/refs**，只表达「给哪个节点、什么模式、覆盖哪些参数」。图解析、引用编号、提示词展开、角色卡注入都在服务端完成——这是杜绝「前端拼 payload 拼错」的根本办法。

**第二层：ExecutionPlan（服务端解析后冻结，返回前端预览 + 费用 + 阻断原因）**

```jsonc
{
  "plan_id": "plan_91c",
  "canvas": "main",
  "base_revision": 128,
  "created_at": "...", "expires_at": "...",
  "cost_estimate": { "currency": "CNY", "min": 0.8, "max": 1.2, "mock": false },
  "jobs": [{
    "job_id": "job_5d2",
    "node_id": "video_gen_7f3a",
    "mode": "image2video",
    "prompt": {
      "text": "参考素材：图片1 = 角色·灰蓝士兵\n\n图片1灰蓝士兵站在雨里回头，霓虹反光……",
      "source": "self",                       // edge|self|merged
      "prompt_hash": "sha256:..."
    },
    "refs": [
      { "role": "first_frame", "ordinal": 1, "name": "角色·灰蓝士兵",
        "asset_id": "ast_11", "sha256": "...", "url": "/assets/...", "mime": "image/png",
        "from_node": "image_2", "width": 1024, "height": 576 }
    ],
    "ref_map": { "图片1": "ast_11" },         // {{ref N}} 的稳定解析表，随 job 冻结
    "params": { "model": "Seedance 2.0 mini", "duration": 5, "ratio": "16:9",
                "resolution": "720p", "seed": 42, "generate_audio": false },
    "preconditions": [
      { "check": "asset_exists", "asset_id": "ast_11", "ok": true },
      { "check": "model_supports", "feature": "first_frame", "ok": true }
    ],
    "ready": true,
    "ready_reason": [],
    "warnings": ["未设置 seed，多镜一致性不保证"]
  }],
  "blockers": [
    { "level": "error", "node_id": "video_gen_7f3a",
      "code": "MISSING_REQUIRED_PORT", "port": "first_frame",
      "message": "image2video 模式需要 first_frame 端口，请连一张图片" }
  ]
}
```

关键设计点（对应现状 4 个漏洞）：
- `ref_map`：把 `{{ref N}}` 从「数组下标」变成「随 job 冻结的映射表」，增删连线不再让历史提示词的 N 漂移（修复缺陷 12）。
- `prompt.text`：服务端统一展开（复用 `_materialize_prompt` 的语义，`server/studio.py:1093-1123`），画布链路不再把字面量 `{{ref 1}}` 送进模型（修复缺陷 13）。
- `preconditions`：参考文件存在性、模型能力、上游状态在 plan 阶段就暴露；任何 `ok=false` 让 `blockers` 非空并**禁止审批**（修复缺陷 14、23 的「静默降级」）。
- `cost_estimate`：审批前给金额区间（现状审批只有哈希，用户不知道在批多少钱）。

**第三层：ProviderRequest（适配器内部，无统一字段，双向隔离）**

```jsonc
// 概念结构，不同 provider 自己映射
{
  "model": "Seedance 2.0 mini",
  "operation": "video.generate",
  "prompt": "...",
  "media": [
    { "role": "first_frame", "url": "data:image/png;base64,...", "mime": "image/png" }
  ],
  "params": { "duration": 5, "resolution": "720p", "ratio": "16:9", "generate_audio": false }
}
```

适配器契约（接口层面的设计，不写实现）：`capabilities()` 声明支持的模式/时长/参考数/是否支持首尾帧/是否支持音频；`build_request(plan_job)` 做映射；`submit()` 返回 opaque 的 `provider_op_id`；`poll()` 或 `on_callback()` 返回归一化结果；`cancel()` 尽力取消；`normalize()` 输出统一的 `MediaResult`。
**能力不匹配在 plan 阶段拦截**，而不是把首尾帧参数丢给不支持的模型再看它报错。

**audio2video / 多模态解析的后端设计**（用户问题二第 3 问）：把「复合 payload → 模型参数」的转换集中到 `ProviderAdapter.build_request` 的**角色映射层**：`role=reference` → provider 的 reference images 数组；`role=first_frame` → `role: first_frame`；`role=audio` → 音频输入（无能力的 provider 直接 blocker）。后端不再有第二套「猜字段名」的逻辑（现状 `TFRouterProvider` 里 `first_frame`/`reference_image` 的 if-else 就是这套映射的雏形，`core/media.py:366-392`）。

### 2.6 任务、事件、版本、资产结构

**MediaJob v2**

```jsonc
{
  "job_id": "job_5d2",
  "canvas": "main", "node_id": "video_gen_7f3a",
  "plan_id": "plan_91c", "generation_id": "gen_ab12",
  "mode": "image2video",
  "model": "Seedance 2.0 mini", "provider": "tfrouter",
  "request": { /* 冻结后的 GenerationPlan.job 快照，含 ref_map 与 sha256 */ },
  "status": "running",           // queued|leased|running|succeeded|failed|unknown|cancelled
  "attempt": 1, "max_attempts": 1,
  "lease_until": "...", "worker_id": "worker_2",
  "provider_op_id": "task_xxx",
  "progress": { "phase": "provider", "percent": 40, "eta_s": 60 },
  "error": null,                 // { code, message, retryable, provider_raw }
  "idempotency_key": "media:sha256(...)",
  "cost": { "currency": "CNY", "amount": 0.9 },
  "created_at": "...", "started_at": "...", "finished_at": null
}
```

**JobEvent（append-only，事件流的最小单元）**

```jsonc
{
  "seq": 1024,
  "event_id": "evt_...",
  "type": "job.progress",
  "canvas": "main", "node_id": "video_gen_7f3a", "job_id": "job_5d2",
  "revision": null,
  "payload": { "phase": "provider", "percent": 40 },
  "ts": "..."
}
```

事件类型建议：`job.queued / job.progress / job.succeeded / job.failed / job.unknown / job.cancelled`、`node.updated`、`canvas.updated`、`asset.created`。前端按 `seq` 排序、按 `revision` 丢弃过期快照；断线重连用 `Last-Event-ID` 续传（这是替代 800ms 全量轮询的关键）。

**Take（版本）**

```jsonc
{
  "take_id": "take_02",
  "node_id": "video_gen_7f3a", "canvas": "main",
  "kind": "video",
  "asset_id": "ast_9c", "sha256": "...",
  "path": "assets/generated/v_9c.mp4",
  "thumb_path": "...", "poster_path": "...",
  "mime": "video/mp4", "bytes": 1234567, "width": 1280, "height": 720, "duration_s": 5,
  "provider": "tfrouter", "model": "Seedance 2.0 mini", "mock": false,
  "seed": 42, "mode": "image2video", "prompt_hash": "...", "refs_hash": "...", "shot_no": 3,
  "created_by": "human", "created_at": "...",
  "rating": null, "note": null,
  "status": "active"    // active|superseded|archived
}
```

- takes 从 canvas JSON 拆到独立表（或至少独立文件），画布只存 `take_ids` 摘要，避免每次生成把整份画布重写（修复缺陷 30）。
- **所有产生媒体产物的路径都必须 append take**：生成、上传替换、合成、抽帧（现状合成/上传不写 takes，`app.py:1391-1404/2090`）。
- 资产回收：take 被删或画布删除后，按 `ref_count` 异步 GC（现状文件永不回收）。

**Asset（内容寻址）**

```jsonc
{
  "asset_id": "ast_9c",
  "sha256": "abc...",
  "kind": "video", "mime": "video/mp4",
  "path": "assets/sha256/ab/abc....mp4",
  "thumb_path": "...", "poster_path": "...",
  "bytes": 1234567, "width": 1280, "height": 720, "duration_s": 5,
  "source": "generated",              // upload|generated|tts|composition|frame_extract
  "canvas_id": "main", "node_id": "video_gen_7f3a",
  "created_at": "...", "ref_count": 2
}
```

内容寻址带来三个直接收益：同一张参考图被多个镜头引用只存一份；`sha256` 写进 job 后，重放任务时能校验「用的还是那张图」；导出/分享时可以直接按 hash 打包。

### 2.7 状态机：节点状态与任务状态的映射（谁写什么）

**任务状态机**：`queued → leased → running → succeeded | failed | unknown`；`unknown` 只能经 reconcile 转 `succeeded/failed` 或人工重放（现状 `core/media_job_store.py:337-363` 的租约超时语义保留）；新增 `cancelled`（用户主动取消）。

**节点状态机**：`draft → ready → queued → running → succeeded | failed | cancelled | stale`。

| 事件 | 节点状态变化 | 写入方 |
|---|---|---|
| plan 生成 | `ready` / 保持 `draft`（有 blocker） | 画布服务（读侧计算，不落库也可） |
| 提交成功 | `queued` | 事务内投影 |
| worker claim | `running`（带 phase） | 事件投影器 |
| job.succeeded | `succeeded` + 写 outputs + append take + current_take_id | 事务内投影 |
| job.failed | `failed` + `error.code/message`（可重试标记） | 事件投影器 |
| 新 generation 启动 | 旧 generation 的节点 → `stale` 并丢弃迟到结果 | 投影器按 `active_generation_id` 判 |
| 用户取消 | `cancelled` | 投影器 |

**硬规则**：节点状态只由服务端投影写入；前端只读；`update_node` 的 patch 白名单不含 `runtime/status/outputs/takes/active_generation_id`（修复缺陷 32）。前端「重试」= 新建一次 generation（只针对该节点），而不是改状态。

### 2.8 存储分层总览

| 层 | 数据 | 介质（现状→建议） | 并发控制 | 备注 |
|---|---|---|---|---|
| 图结构 | nodes/edges/meta | canvas JSONB / 文件 → 保持，加 schema_version | revision CAS + ops 幂等键 | 仍是唯一真相 |
| 执行 | MediaJob、JobEvent | media_jobs 表（已有）→ 加 outbox/seq | 行锁 + 租约 + 幂等键 | 事件必须持久化 |
| 版本 | Take | canvas JSON 内嵌 → 独立表 | append-only | 迁移后画布变轻 |
| 资产 | Asset | media_assets（已有，无索引）→ 加 sha256 唯一 + 索引 + 引用计数 | 唯一约束 | 支撑素材库 |
| 审批 | Approval | 已有（内存/仓储）→ 持久化 | 消费幂等 | 需带金额与 plan_hash |
| 幂等 | invocation | 已有（`invocation_store`） | 唯一键 | 与 job 关联 |
| 投影 | 节点 runtime | 物化在 canvas JSON | 投影器 CAS 重试 | 可由事件重建 |

### 2.9 现状 → 目标的迁移策略（不打断存量画布）

1. **加版本号**：`CanvasDocument.schema_version`，读时按版本迁移，写时只写新版本；旧画布首次打开自动升级。
2. **双写过渡期**：`node.data.takes` 保留为只读镜像，新写入同时落 takes 表；前端优先读新表，回退读 JSON。
3. **ops 接口灰度**：先让前端「位置更新」和「连线」走 ops，其余保持整文档写；验证后再切全量。
4. **provider 适配器不改动对外模型清单**，只在适配器内部加 `capabilities()`，逐步把硬编码的 `first_frame`/`reference_image` 分支替换成角色映射。
5. **前端清理**：删掉 `?legacy=1` 的 Vue Flow 版（或改为回归用例），避免两套交互逻辑长期漂移；store 拆成 `canvasDoc`（服务端镜像）与 `jobs`（任务态）两个命名空间。

---

## 三、视频生成流程的前后端交互时序图（文字详细描述）

### 3.0 参与组件

| 角色 | 职责 |
|---|---|
| `CanvasBoard`（前端） | 只发「意图」（intent）+ 订阅事件；不组装 prompt/refs，不写状态 |
| `Store.canvasDoc` | 服务端镜像：nodes/edges/revision；写操作全部走 ops + If-Match |
| `Store.jobs` | 任务态镜像：按 canvas/node 索引的 job 快照 + 事件游标 |
| `CanvasService`（后端） | 读图、解析模式、收集 refs、展开提示词、生成 ExecutionPlan |
| `ApprovalService` | plan_hash + 金额 + 人 + 过期时间的审批凭证，消费幂等 |
| `GenerationService` | 事务内：校验 revision/plan/单飞 → 写 job + 投影 node=queued + outbox |
| `MediaJobStore` | 持久化队列 + 租约 + 幂等键 + unknown 语义 |
| `JobWorker`（独立进程） | claim → provider 提交/恢复 → 下载 → 事务回写 take/outputs/runtime/event |
| `ProviderAdapter` | capabilities 校验 + 请求映射 + submit/poll/resume/cancel/normalize |
| `Projector` | 消费 outbox/事件，把 job 状态投影到画布文档（CAS 重试） |
| `EventStream` | SSE/WS 推送 `job.*` / `node.updated` / `canvas.updated`，支持 Last-Event-ID 续传 |
| `AssetService` | sha256 内容寻址、缩略图/海报生成、引用计数 GC |

### 3.1 主链路（从点击生成到节点更新）分步描述

**Step 0 · 打开画布（订阅先于操作）**
1. 前端 `GET /api/canvas/{name}` 拿文档 + revision；`GET /api/media/jobs?canvas=main&active=1` 拿未完成任务快照；随后建立 `GET /api/canvas/{name}/events`（SSE，带 `Last-Event-ID`）。
2. 前端把「进行中/失败」任务合并进 `node.runtime`，因此刷新页面后节点状态立刻正确（现状靠 800ms 轮询才恢复，`store.js:394-430`）。

**Step 1 · 点击生成（单卡或批量）**
1. 前端从图里读目标节点的**入边摘要**做本地预检（端口是否必填、上游是否 succeeded、模型是否支持该模式），只用于即时反馈；不是裁决。
2. 前端只提交**意图**：`POST /api/canvas/{name}/plans`，body = `GenerationIntent { targets, mode, overrides }` + `If-Match: revision`。
3. 前端立即把目标节点置为「计划中」（纯 UI 态，不写文档），按钮进入 loading，防重复点击；同 canvas 的 generation 通道保持单飞。

**Step 2 · 服务端解析图，生成 ExecutionPlan（核心解析发生在后端）**
服务端在一个只读快照（按 revision）上依次做：
1. **定位目标与模式**：`mode` 由端口/连线/用户选择推导（例如 `first_frame` 有边 → `image2video`；有 `first_frame`+`last_frame` → `first_last_frame`；只有 prompt → `text2video`）。
2. **提示词来源解析**（修复 P0-7 与「read_text 读不到新式正文」）：
   - 优先级：显式 `prompt` 入边 → 节点自身 `data.prompt` → 上游 prompt 节点的渲染正文（`content.md` / `outputs.prompt` / `data.prompt` 三态统一读取）→ 报错 `MISSING_PROMPT`；
   - 展开 `{{ref N}}`（用本 job 的 `ref_map`）+ `@名字`（用资产名映射）+ 追加「参考素材」头；输出 `prompt.text` 与 `prompt_hash`。
3. **收集参考资产**：遍历入边，按 `(target_port, role)` 分组；每个资产登记 `asset_id/sha256/mime/尺寸/from_node`；`ordinal` 用**稳定键**（角色卡 id / 边创建序 / 节点创建序的组合）生成 `ref_map`，避免增删连线导致编号漂移。
4. **首尾帧/角色卡注入**：`first_frame`/`last_frame` 单值校验；角色卡节点追加 `consistency_tags` 到提示词头并绑定参考图（如果模型支持多参考图）；上一镜 `last_frame` 若由「自动抽帧」提供，标记 `source=frame_extract`。
5. **参数解析优先级**：节点 `data.params` → 分镜镜头 `duration_s`（storyboard shots）→ 项目 `meta.defaults`（比例/分辨率**必须读项目**，修复画布链路硬编码 16:9 的问题）→ 模型能力 clamp（如 4-30s）。
6. **能力与前置校验**：`ProviderAdapter.capabilities()` 校验模式/时长/参考图数量/首尾帧/音频；`asset_exists` 校验文件可读；上游节点状态校验；任何 `ok=false` 进入 `blockers`（error 禁止审批，warning 允许但提示）。
7. 返回 `ExecutionPlan { jobs[], blockers[], cost_estimate }`；**此步不写库、不扣费**。

**Step 3 · 用户确认与审批（人必须看见「将要发生什么」）**
1. 前端打开「生成确认面板」：逐 job 展示结构化提示词（已展开）、参考图缩略图（标注「首帧/尾帧/参考」）、模型、时长、比例、预计费用、blockers/warnings。
2. 用户点「确认生成」→ `POST /api/canvas/{name}/approvals { plan_id, plan_hash }` → 服务端校验 plan 未过期、blockers 为空、金额未超限（可配阈值）→ 写 Approval（人、plan_hash、金额、过期时间）→ 返回 `approval_id`。
3. 这一步替代现状的「前端 `approved: true` 自批」（`store.js:365-368`），审批不再是橡皮图章，也不会出现「改完参数审批哈希对不上」。

**Step 4 · 提交生成（事务 + 幂等）**
1. `POST /api/generations`，headers 带 `Idempotency-Key`（建议 = `plan_hash + node_ids`），body `{ canvas, approval_id, plan_id }` + `If-Match`。
2. 服务端一个事务内完成：
   - 校验 revision（冲突 → 409，前端重新拉 plan，不扣费）；
   - 校验 approval 未消费/未过期，plan_hash 一致；
   - **单飞检查**：同节点已有 `active_generation_id` 且状态 queued/running 时，返回 409 `ALREADY_GENERATING`（或按用户确认「替换」，把旧 job 标记 stale）；
   - 写 `MediaJob(status=queued)`（幂等键唯一约束，重复提交返回同一批 job）；
   - 投影：`node.runtime.status=queued`、`active_generation_id=gen_x`；
   - 写 outbox 事件 `job.queued`；
   - 消费 Approval（同一 approval 只能成功一次）。
3. 返回 202 `{ job_ids, revision }`；前端立即按 job_id 显示「排队中」，**不等待生成完成**。

**Step 5 · Worker 执行（提交 / 恢复 / 下载）**
1. `JobWorker` 从队列 claim（`FOR UPDATE SKIP LOCKED` 或 Redis 原子操作），置 `leased` + `lease_until`，周期 heartbeat。
2. 幂等：按 `job.idempotency_key`/`generation_id` 检查 invocation 表，确保「provider 侧只提交一次」；若已是 `executing` 状态，走 `resume(provider_op_id)` 而不是重新提交。
3. `adapter.build_request(plan_job)` → `submit()` 得到 `provider_op_id` 并**持久化**（崩溃恢复的关键），投影 `node.runtime.status=running`，发 `job.progress`（若 provider 支持百分比/阶段）。
4. 轮询或等待回调；结果下载到临时文件 → 校验大小/类型 → 计算 sha256 → 生成缩略图/海报（视频抽帧）→ 登记 Asset（内容寻址、引用计数）。
5. **回写事务**（CAS 重试，修复 P0-1）：
   - append `Take`（含 prompt_hash/refs_hash/seed/mode/model/cost）+ 更新 `current_take_id` + `outputs` 指针；
   - 投影 `node.runtime.status=succeeded`，清 `active_generation_id`（或保留用于版本对比）；
   - 写 `job.status=succeeded` + result；写 outbox `job.succeeded`（带 canvas revision）；
   - 事务失败/冲突 → 合并重试 N 次；仍失败则**保留结果文件**，job 置 `failed` 且 `error.code=WRITEBACK_CONFLICT`，由投影器异步补写（绝不让付费产物消失）。

**Step 6 · 前端更新节点**
1. SSE 收到 `job.progress` → 只更新该节点 `runtime.status_detail` 的进度环/文案，不刷新文档。
2. 收到 `job.succeeded` → 更新 `node.runtime.outputs` + `takes`；节点卡换成 `<video preload="none" poster=...>`；历史抽屉新增一版并高亮「当前版」。
3. 收到 `job.failed/unknown` → 节点角标变红，显示错误码与「原因/可重试」；失败原因来自 `error.code/message`，不再只有「生成失败」。
4. 收到 `canvas.updated`（其他端/Agent 改了画布）→ 若本地无未提交编辑，按 revision 更新文档；若有，提示「画布已在别处更新」，提供合并/丢弃（现状是静默 reload，`store.js:201-204`）。
5. 前端**永不**自己把节点写成 done/error；所有状态来自事件投影（修掉 `update_node` 可写 status 的后门）。

**Step 7 · 取消 / 重试 / 切版本**
- 取消：`POST /api/media/jobs/{id}/cancel` → worker 尽力 `adapter.cancel(provider_op_id)` → job/node 置 `cancelled`；不影响已产生版本。
- 重试：前端只对该节点重新走 Step 1-4（新 plan / 新 generation_id），旧 job 若仍在跑会被 `stale` 丢弃（沿用现状 `active_generation_id` 语义）；重试前明确提示「上一次可能已计费」。
- 切版本：`POST /nodes/{id}/take`（带 revision）只改 `current_take_id` 与 outputs 指针，不触发重新生成、不产生费用。

**Step 8 · 合成与导出（复用同一任务设施）**
`timeline` 节点 → composition plan（clip_order/trims/转场/音频/字幕）→ 审批 → composition job（同 worker 体系，单独 job_type）→ 合成产物 append 到 timeline 的 Take → 事件更新。导出（JSON/成片/选中节点）走同一套资产与事件，不再另起一套。

### 3.2 时序图（文字版）

```
前端            画布服务         审批服务       任务存储         Worker           Provider         事件流
 │ 点击生成        │                │               │                │                 │               │
 │─POST /plans───>│ 读图(revision)  │               │                │                 │               │
 │                │ 解析prompt/refs │               │                │                 │               │
 │                │ 能力/文件/上游校验│              │                │                 │               │
 │<──ExecutionPlan(blockers,cost)──│               │                │                 │               │
 │ 展示确认面板    │                │               │                │                 │               │
 │─POST /approvals───────────────>│ 写Approval     │                │                 │               │
 │<──approval_id──│                │ (plan_hash,金额)│               │                 │               │
 │─POST /generations(+IdemKey)───>│ 事务:校验rev/单飞│               │                 │               │
 │                │───────────────写job(queued)────>│                │                 │               │
 │                │───────────────投影node=queued─>│                │                 │               │
 │                │───────────────outbox job.queued───────────────>│                 │               │
 │<──202 {job_ids}│                │               │                │                 │               │
 │<═══════════════ job.queued/progress 事件 ═══════════════════════════════════════════════════════│
 │                │                │               │─claim(lease)──>│                 │               │
 │                │                │               │                │─build_request──>│               │
 │                │                │               │                │<──op_id─────────│               │
 │                │                │               │                │ 持久化op_id      │               │
 │                │                │               │<──running──────│                 │               │
 │<═══════════════ job.progress 事件 ══════════════════════════════════════════════════════════════│
 │                │                │               │                │─poll/resume────>│               │
 │                │                │               │                │<──result url────│               │
 │                │                │               │                │ 下载/校验/sha256 │               │
 │                │                │               │                │ 事务: take+outputs+node=succeeded+job=succeeded
 │                │                │               │                │────────────outbox job.succeeded─────────────>│
 │<═══════════════ job.succeeded 事件（含 take/outputs）═════════════════════════════════════════════│
 │ 局部更新节点     │                │               │                │                 │               │
 │ (视频懒加载poster)│               │               │                │                 │               │
```

### 3.3 异常分支（必须全部定义，否则线上必踩）

| 场景 | 期望行为 |
|---|---|
| provider 提交超时 | job 继续按 `provider_op_id` 恢复；超过全局 deadline（现有 1800s，`core/media.py:411-449`）→ `failed`，`error.retryable=true`，节点保留旧 take |
| worker 崩溃 / 租约过期 | 置 `unknown`（现状语义），不自动重试；reconcile 用 `provider_op_id` 查询真实结果：成功则补写 take，失败则 `failed` |
| 画布 revision 冲突（回写） | 合并重试；只合并本次 job 拥有的字段；`WRITEBACK_CONFLICT` 时后台补偿，不回滚产物 |
| 节点在生成中被删除 | job 取消/忽略；产物登记为孤儿资产（可被素材库回收或在历史里找回），事件注明 `node_deleted` |
| 重复点击 / 重复提交 | 幂等键命中返回同一 job；同节点单飞 409，前端提示「已有一个生成在跑」 |
| 参考图文件丢失 / 不可读 | plan 阶段 `blockers`（修复现状静默降级为文生图），审批按钮禁用并给出「重新上传」入口 |
| 模型不支持所选模式（首尾帧/时长/比例） | plan 阶段 `blockers`，并建议可用模型（capabilities 表驱动） |
| 审批过期 / 已被消费 | 410 `APPROVAL_EXPIRED/CONSUMED`，前端重新拉 plan 重新审批，不扣费 |
| 资产下载失败 | 下载重试 + 保留 provider 临时 URL；仍失败 → `failed` 并保留 op_id 供人工恢复 |
| 前端断线 | SSE 重连 + `Last-Event-ID` 续传；重连后拉任务快照对齐 |
| 服务重启（默认 memory store） | 生产必须 Redis/Postgres 任务存储；启动扫描 `queued/running` 任务：local 侧未提交的置回 queued，已提交的置 unknown 等 reconcile |
| 用户切版本 / 编辑中 | 切版只改指针；编辑不打断生成；若编辑改了 prompt，plan 的 prompt_hash 标记 stale 但不影响已在跑的任务 |
| 合成任务失败 | 同生成链路：失败可重试，保留已完成片段，支持从断点续合 |

### 3.4 与现状的对照（每一步差在哪）

| 步骤 | 现状 | 目标 |
|---|---|---|
| 解析 | 前端组装 prompt/refs（`store.js:357-385`），后端只透传 | 前端只发 intent，服务端解析图 |
| 提示词 | 画布链路发原文（含 `{{ref N}}`），且读不到新式正文 | 服务端展开 + 三态统一读取 |
| 参考 | 扁平列表，N 随数组下标漂移 | `(port, role, ordinal)` + 冻结 `ref_map` |
| 校验 | 无文件/能力校验，静默降级 | plan 阶段 blockers |
| 审批 | 前端自批 `approved: true`，只有哈希 | 人确认 + 金额 + 过期 + 幂等消费 |
| 提交 | 先 consume 后 enqueue，非事务 | 单事务 + 幂等键 + 单飞 |
| 回写 | 裸 save，冲突即失败 | CAS 合并重试 + 保底补偿 |
| 通知 | 800ms 轮询 + 全量 reload | SSE 事件 + 局部更新 |
| 状态 | 前端可写、全局 canvasBusy 冒充 | 服务端投影，前端只读 |
| 版本 | takes 内嵌 JSON，合成/上传不写 | 独立表 append-only，所有产出入版本 |

---

## 四、针对短剧业务的画布交互优化与性能建议

### 4.1 画布基础设施（对应板块一）

**视图与操作**
- 保持自研 viewport（滚轮缩放、光标锚定、空格/中键平移、fit 已可用），但补：`ResizeObserver` 驱动可见区重算；fit 同时考虑负坐标；缩放档位限制在 0.1–2.5 并对齐设备像素。
- **小地图**：右下角 160×120 缩略图，只画「节点矩形 + 视口框」，按 200ms 节流；点击/拖动跳转。不要复用 Vue Flow 的 MiniMap（已废弃那套）。
- **框选与多选**：保留框选；补 Shift 加选/减选、Ctrl+A 全选、点空白清空；框选结果实时高亮；多选拖动用一条批量 ops。
- **对齐**：拖动时对同层节点做 6px 吸附 + 显示参考线（左右居中/顶底对齐/等距）；提供「一键对齐/等距分布」命令（多选后）。
- **分组 Frame**：新增 `frame` 节点类型（或 `node.parent_id` + 组框渲染）：父子坐标（移动 Frame 带动子节点）、折叠、命名、批量运行「这一场所有生成卡」、组级状态汇总。短剧建议「一场戏一个 Frame」。
- **历史**：本地 command stack（move/prompt/connect/delete/创建 的逆操作）；拖拽合并为一条、文本 500ms debounce；Ctrl+Z/Y；切画布清栈；服务端只存最终状态（不做快照式 undo）。
- **快捷键层**：统一 hotkey 模块 + `isEditing` 守卫（输入框内不触发画布快捷键）。建议：Space 平移、滚轮缩放、Ctrl+Z/Y、Delete/Backspace 删除选中、Ctrl+C/V 复制粘贴（节点+内部边）、Ctrl+A 全选、Ctrl+G 分组、Ctrl+Enter 生成选中、Esc 逐层关闭（菜单→编辑→选中）。

**右键菜单**
- 空白：新建（图片/视频/提示词/分镜/文本/角色卡）、粘贴、全选、适应视图、排列（自动布局已有 `arrange`）。
- 节点：生成、只生成这一张、复制、删除、查看历史、打开详情、上传替换、加入分组。
- 边：删除、改接端口（reconnect）、插入节点（中间加一个生成节点）。
- 菜单定位做视口 clamp（修 `CanvasBoard.vue:613-643` 越界）。

**边操作（缺失项）**
- 边支持 hover 高亮 + 点击选中 + Delete 删除 + 拖动端点改接；`matchPorts` 升级为「端口候选列表」：拖到目标卡上时高亮所有兼容端口，由用户/最近距离决定，而不是只取第一个同类型口。

### 4.2 节点内交互：内联提示词 vs 下方展开（用户问题三的评估）

**现状**：卡片下方 PromptBox（`canvas2/PromptBox.vue`，3 行 textarea，Enter 发送，失焦不保存）+ NodeStudio 右侧抽屉。问题：a) 生成卡写了不参与生成（P0-7）；b) 失焦丢草稿；c) 卡片下方常驻输入框让画布密度显著下降；d) chips 是装饰。

**三种方案的取舍**

| 方案 | 画布密度 | 编辑效率 | 长文本可读性 | 生成入口 | 结论 |
|---|---|---|---|---|---|
| A. 完全内联在卡片中（卡片底部常驻 textarea） | 最差（卡高 +60~90px，几十个节点时拥挤） | 高 | 差（3 行截断） | 直接 | 不适合短剧（提示词长、节点多） |
| B. 卡片下方常驻展开（现状的加高版） | 差 | 中 | 中 | 直接 | 现状已偏拥挤 |
| C. 卡片内一行摘要 + 抽屉/浮层编辑 | 好 | 中（多一次打开） | 好（宽、可长文、可富交互） | 卡片角标 + 抽屉内按钮 | **推荐** |

**推荐混合方案（避免拥挤的同时保留「随手写」）**：
1. 卡片底部固定 **1 行**：`prompt 摘要（截断）` + 模式徽标（文生/图生/首尾帧）+ 生成按钮；高度恒定 28px，不随内容膨胀。
2. **双击卡片正文 / ⌘Enter / 点摘要** → 打开右侧抽屉（复用 NodeStudio）：宽文本编辑、参考图槽位、首尾帧槽位、模型/时长/比例/seed、生成确认。编辑实时保存（debounce 500ms + 失焦立即保存）。
3. 悬停时在卡片顶部出现「生成 / 历史 / 上传」三个小按钮，不常驻（现状 NodeToolbar 选中即显示）。
4. 文本类节点（prompt/storyboard）保留卡片内多行编辑（它们以文字为主、数量少）。
5. 批量生成时抽屉不打断：选中多卡 → Ctrl+Enter 直接走「计划确认面板」。

### 4.3 短剧一致性（用户问题三：首尾帧 + 角色卡）

**首尾帧**
- 在 `video_gen` 增加 `first_frame`/`last_frame` 单值端口（provider 侧 `last_frame` 已有实现但**全仓无调用方**，`core/media.py:344/372`），画布 UI 提供「从图片节点连入 / 从资产库选 / 从上一镜尾帧取」三种来源。
- **首帧接力**：生成成功后自动抽尾帧为 take（`poster`/`last_frame`），下一镜可一键「接上一镜尾帧」；这是短剧镜头连贯的最低成本手段，也是现在完全缺失的。
- 首帧+尾帧双约束生成（`first_last_frame` 模式）在 plan 里作为独立 mode，能力表驱动。

**角色卡（Character Card）**
- 现状：`asset` 节点 + `consistency_tags` + `ref` 边 + `{{ref N}}`；但 `consistency_tags` 只进资产图提示词，不进视频/图片请求（drama 审计确认），且没有实体、没有校验、没有 seed。
- 建议：新增「角色卡」资产类型（或 `asset.kind=character`）：`{ character_id, name, tags[], anchor_images[]（多角度）, default_prompt_fragment, style_refs[], voice_id }`；生成时由服务端**强制注入**「角色声明块 + 锚定参考图」，而不是靠作者手写 `@名字`。
- **一致性校验**：分镜表每镜声明 `character_ids`；生成前 lint「本镜未引用本场角色卡」「同一角色在不同镜引用了不同锚定图」→ warning；跨镜一致性还可用固定 seed + 同一模型 + 同一参考图组合。
- 不做「人脸 embedding 相似度校验」这类重方案（成本/误报），先把「引用同一张锚定图 + 同一 seed + tags」做扎实。

**镜头表驱动**
- storyboard 节点 `data.shots` 作为「这一场的镜头表」：每镜 `{shot_no, duration_s, character_ids[], prompt_seed, first_frame_from_shot}`；生成面板按镜头表批量出任务（顺序生成，前一镜尾帧可作后一镜首帧），进度在镜头表行内展示。
- 现状分镜时长不进入画布生成（`generation_jobs` 不读 shots），而制片台有 `_infer_duration`；应当统一到 plan 参数解析的优先级链。

### 4.4 异步状态与体验（板块四）

- **任务中心**：右上角全局队列（按 canvas 过滤）：排队/生成中/失败；每行显示节点名、进度、模型、预计费用、重试/取消。
- **节点徽标**：`queued` 灰点 + 位置数字、`running` 进度环、`failed` 红角标可展开错误、`stale` 旧任务半透明；全部来自 `runtime`，删除 `canvasBusy` 全局冒充。
- **失败可操作**：错误分级（provider 限流/内容审核/超时/参数不支持/文件缺失），每种给一个动作（重试/换模型/重新上传/改时长）。
- **限流与并发**：按 provider 配并发上限；批量生成默认串行或小并发（短剧镜头间有依赖）；队列可见、可暂停。
- **进度**：provider 无进度时用「阶段 + 已耗时/预计时长」而不是假百分比；`queued` 时显示队列位置。

### 4.5 性能（板块五）

**渲染**
- 已具备：可见区剔除、LOD（缩略/简化）、只画两端可见的边（`CanvasBoard.vue:73-91/143-150`）——保留。
- 补：`ResizeObserver`；选中/拖拽/编辑中的节点永不被剔除；节点/边列表用浅比较 memo；大画布（>500 节点）时边降级为直线并减少阴影/模糊；Frame 折叠后子节点不渲染。
- **视频懒加载（当前完全没有）**：`<video preload="none" poster>` + IntersectionObserver 进视口才 `src`；网格/大图 `loading="lazy"`；缩略图用后端的 `thumb/poster`，不要用原片。
- **首屏**：`GET /api/canvas` 支持 `include_text=false`（正文单独按需拉，修 N+1 文件读）；任务快照单独接口。

**数据与保存**
- 拖拽 300ms debounce + 一条 `move_node`；组拖一条批量 ops；文本 500ms debounce + 失焦保存；切卡/关闭页面 flush。
- 冲突：位置类自动重放，内容类提示合并；禁止静默 reload。
- revision 冲突风暴：前端 mutation queue 串行化，同一画布的写请求不并发。

**后端**
- worker 池化（每进程 N 并发 + provider 限流），视频生成不再阻塞其他画布；任务存储生产环境强制 Redis/Postgres（现默认内存，重启丢任务）。
- 列表接口只查 meta（jsonb 投影）；`list_canvases` 不再逐个全量 load；资产按 sha256 去重、缩略图异步生成。
- 事件流替代轮询：SSE 单连接 + 心跳 + `Last-Event-ID`；轮询仅作兜底（30s 一次快照对齐）。

### 4.6 素材库与导出（板块五）

**Asset Library**
- 页面级素材库：按类型/来源/画布/角色筛选，分页；缩略图网格；详情显示 sha256、引用它的节点/镜头（反查）；支持「拖到画布即建 asset 节点」。
- 数据基础已有（`media_assets` 表只写不读），补查询接口 + 索引 + 引用计数。
- 画布侧：上传入口统一走资产库（上传即入库，节点只是引用），删除画布不删资产。

**导出**
- 画布 JSON 导出/导入（含 `schema_version`、资产清单 `{sha256, path}`；导入时按 hash 校验/补齐资产）。
- 选中节点导出：图片/视频原片、或带提示词的镜头卡（markdown/PDF）。
- 成片导出：复用 composition（时间轴），支持单镜预览、整片下载、字幕 SRT 单独导出；导出任务同样进任务中心。
- 只读分享：快照 + 资产打包（zip）或只读链接（服务端渲染缩略图 + JSON）。

### 4.7 成本与可观测性（短剧业务必需）

- 审批前展示单任务/批量金额区间（provider 价目表），审批记录带人、金额、plan_hash；
- 任务表记录 `cost`、`provider_op_id`、耗时、失败码，做用量面板（按项目/画布/模型）；
- 失败率/超时率监控；`unknown` 任务告警（钱可能花了但没结果）；
- 审计：谁在什么时候改了哪个镜头、批了多少钱（审批 + 事件流天然可回溯）。

---

## 附录 A：修复路线图（建议 4 个 Sprint）

### A.0 已交付（14 项，2026-10）

| 项 | 内容 | 证据 |
|---|---|---|
| P0-2 | 内容写入强制 `revision`：`/text`、`/take`、`DELETE canvas`；缺 revision→400，过期→409 | `docs/p0-repro-verify-plan.md` §7 |
| P0-1 | Worker 写回 CAS 合并重试：`_mutate_canvas_with_retry`，6 处写回改闭包重放 | §8 |
| P0-8 | 审批/入队原子化：哈希去 `canvas_revision`、`ApprovalStore.release`、单一提交点 + 回滚 | §9 |
| P0-9 | 正文单一入口：`read_text` 回退链、`validate` 分级、写侧只写本槽位 | 审查修复（§10 之后的追记） |
| P0-7 | 提示词来源优先级：`resolve_prompt`（prompt 端口 > 文本入边 > 卡片自身） | §10 |
| P0-10 | 参数解析链：节点 > 分镜时长 > 项目画幅/质量 > 请求默认，`size` 由 ratio×quality 推出 | §10 |
| 合成原子化 | `compose_canvas` 与生成路径同构：`job_key` 去 revision、`activate/revert` + 单一提交点、失败退回审批 | §11 |
| Agent 双轨统一 | `build_media_request` 供 Worker 与 Agent 工具共用；Agent 生成走 `record_generated_media`（outputs+takes）与 CAS 重试，不再硬编码画幅 | §12 |
| 任务终态语义 | `MediaJobWorker` 读执行结果：产物被取代记 `discarded`、有节点失败记 `failed`，不再一概 `succeeded`；`/api/media/jobs` 与前端 `waitForMediaJob` 同步 | §13 |
| 异常边界收窄 | 提交点区分"临时故障"（超时/连接/存储 RuntimeError/驱动模块异常）与编程错误：前者 503 + `warning` 日志、后者 500 + `logger.exception`；两者都退回审批并回滚激活；CAS 重试改为 jitter 退避 | §14 |
| P0-6 删除节点 | 新增 `DELETE /api/canvas/nodes/{id}`（级联删边 + 严格 revision）；Worker 对"节点已被删除"按 `stale_discarded` 处理；前端右键菜单/删除快捷键接入 | §15 |
| P0-3 拖拽回弹 | `store.moveNode` 先把坐标写回本地 `state.canvas.nodes[].position`（乐观更新 + 失败回滚），`moveNodes` 整组落位后串行提交；`positions.js` 抽出坐标纯函数；多选拖动时同组卡片即时跟手（`preview` 偏移），松手一次提交 | §16 |
| P0-5 视频卡 | 视频卡从 `<img>` 改为 `<VideoPlayer>`：三层懒加载（视口 + 缩放 ≥45% + 只取首帧）、悬停静音预览、时长以 `take.duration_s` 为准、播放键打开只读灯箱（Esc/点背景关闭、body 锁滚动、下载）；图片卡与旧 Vue Flow 版不动 | §17 |
| M1/M2 生成可见性 | 卡片生成遮罩（提交即显示，不等轮询）+ 完成后 1.5s 高亮 + 失败遮罩与「重试」；生成期间禁用按钮/批量过滤 busy；顺带修「轮询触发 fit 拽走视口」与「先悬停后到位不预览」 | §18（面试前最小修复） |

> 全量门禁：`python -m pytest tests/p0 -q` → 58 passed；`cd web-vue && npm run test:unit` → 9 passed；`compileall` OK；`npm run build` OK；真 MockProvider 端到端 smoke 通过；`demo/composition_invariants.py` PASS。

### A.1 剩余问题与优先级（本轮审查后更新）

> 完整、带证据行号的后端待完善清单见 [`docs/后端待完善清单.md`](后端待完善清单.md)（2026-10-19 记录，含安全 S1–S4、契约 C1–C5、异步 A1–A7、业务 B1–B6、数据运维 D1–D6、工程 E1–E4）。本节只保留与 Sprint 排期直接相关的条目。

排序判据：① 是否造成不可逆损失（钱/产物/数据）② 是否阻塞其它修复（前置依赖）③ 改动是否收敛且可验证 ④ 用户是否直接可感知。

**P0｜止血收尾（约 1.5–2 天）**

1. ~~**合成路径原子化**~~ ✅ **已交付（§11）**：`compose_canvas` 改为与生成路径同构的单一提交点；`job_key` 排除 `canvas_revision`；失败退回审批并 503。RED 用例 `tests/p0/test_p0_11_compose_atomicity.py`（5 条）。
2. ~~**Agent 工具与画布双轨统一**~~ ✅ **已交付（§12）**：抽出 `build_media_request(job)` 供 Worker 与 Agent 工具共用；Agent 写回复用 `record_generated_media`（outputs + takes）与 `mutate_canvas_with_retry`；`media_generate` 指定节点时参数取节点口径。RED 用例 `tests/p0/test_p0_12_agent_tool_parity.py`（6 条）。
3. ~~**stale 任务状态语义**~~ ✅ **已交付（§13）**：`_classify_result` 把执行结果翻译成 `failed/discarded`，`complete()` 三实现加 `discarded` 参数；部分失败记 `failed` 并把逐节点原因汇总进 `job.error`；前端 `waitForMediaJob` 不再把 `discarded` 当失败。RED 用例 `tests/p0/test_p0_13_stale_status.py`（3 条）。
4. ~~**异常边界收窄**~~ ✅ **已交付（§14）**：提交点把异常分成"临时故障"（超时/连接/`RuntimeError`/`redis`/`asyncpg` 等驱动异常 → 503 + `logger.warning`，用户可重试）和"编程错误"（`TypeError`/`AttributeError`/`KeyError` 等 → 500 + `logger.exception`），两条路都退回审批、回滚激活；`mutate_canvas_with_retry` 改为 jitter 退避。RED 用例 `tests/p0/test_p0_14_error_boundary.py`（3 条）。

**P1｜体验正确性（约 2–3 天）**

5. ~~**P0-3 拖拽回弹**~~ ✅ **已交付（§16）**：根因是 `store.moveNode` 只回写 revision、没回写本地坐标。现在 `moveNode` 先乐观写 `state.canvas.nodes[].position`（其它失败回滚，409 冲突则重拉并以服务端为准），`moveNodes` 先整组落位再逐张串行提交；坐标计算抽成 `canvas2/positions.js` 纯函数；多选拖动时同组卡片用 `preview` 偏移即时跟手、松手一次提交；顺带修掉 `NodeCard` 拖动中卸载不摘 `window` 监听。前端单测 `web-vue/tests/positions.test.mjs`（5 条，`npm run test:unit`）。
6. ~~**P0-5 视频卡**~~ ✅ **已交付（§17）**：新增 `canvas2/video.js`、`VideoPlayer.vue`、`VideoLightbox.vue`；视频卡不再是 `<img>` 破图，三层懒加载（视口 + 缩放 ≥45% + `#t` 首帧）、悬停静音预览、时长取 `take.duration_s`、播放键打开只读灯箱；`canvas2/NodeCard.vue` 删除伪 `play-btn`/`00:04`；`CanvasBoard.boardNodes` 补 `src`/`durationSeconds`。前端单测 `web-vue/tests/video.test.mjs`（4 条，`npm run test:unit` 共 9 条）。
7. ~~**P0-6 删除节点**~~ ✅ **已交付（§15）**：新增 `DELETE /api/canvas/nodes/{id}`（级联删边 + 严格 revision），Worker 对"节点已删"按 `stale_discarded` 处理；前端右键菜单与 `Delete`/`Backspace` 快捷键接入（批量删除逐个 CAS，失败即停）。RED 用例 `tests/p0/test_p0_15_node_delete.py`（6 条）。
8. **revision 全局收口**：`position/connect/edges/nodes/upload/rename/run/compose` 仍允许 `None` → 严格化，并同步 `web/canvas.js`、`demo/*.py`（或提供 `ALLOW_LEGACY_WRITE=1` 兼容开关）。

**P1｜异步化（约 2 天，依赖 3）**

9. **P0-4 事件流**：前端 800ms 轮询 + 每轮 `loadCanvas()` 全量重拉 → SSE/WS 事件流 + 节点状态投影；job 状态机补 `queued/cancelled`；超时与取消。

**P2｜清理（约 0.5 天）**

10. `media_size` 与 `server/studio.py:_default_size` 合并（口径已一致，防漂移）；旧画布 `outputs` 与 `data.prompt` 分叉的一次性迁移脚本；`_update_production_stage` 的 4 次 CAS 加退避。

**P2｜产品增强（另排）**：Frame 分组/小地图/对齐吸附/undo、素材库、首尾帧接力与角色卡、导出 JSON/成片。

### A.2 依赖与建议顺序

- 依赖：3 ✅ → 9（状态机先于事件流）；8 依赖前端调用点全部带 revision（store 已带，剩 `web/canvas.js` 的 position 与 demo 脚本）；1 ✅/2 ✅/3 ✅/4 ✅ 相互独立；7 ✅ 不依赖其它项，但它的"批量删除"与 5 ✅（拖拽乐观更新）共用前端选择态（已共用 `selection`/`selectedIds`）。
- 建议顺序：**Sprint 0.5：1 ✅ → 2 ✅ → 3 ✅ → 4 ✅**；**Sprint 1：7 ✅ → 5 ✅ → 6 ✅**；**Sprint 2：8 → 9**；P2 穿插。
- 每批门禁：先写 RED → 修 → 全量 `pytest tests/p0` → `compileall`（前端改动加 `npm run build` + `npm run test:unit`）→ 涉及媒体/合成时跑真 Provider smoke → 同步三份文档。

### A.3 后续大项（原 4-Sprint 规划保留）

**Sprint 1（约 2 周）｜生成架构：三层 payload + 事件流**
1. `GenerationIntent → ExecutionPlan → ProviderRequest` 三层；plan 阶段 blockers/费用/能力校验。
2. 审批面板（人确认 + 金额 + plan_hash + 过期 + 幂等消费）。
3. `POST /api/generations` 单事务 + 幂等键；job 状态机补 `cancelled`。
4. 任务持久化默认 Redis/Postgres；worker 池化 + provider 限流；启动 reconcile。
5. SSE 事件流 + 前端任务中心/节点投影；删除 800ms 全量轮询。
6. takes 拆表 + 所有产出路径入版本；资产 sha256 去重、索引、引用计数。
> 验收：两个画布并发各 3 个视频任务互不阻塞；重启进程后任务可恢复/可解释；前端每秒请求数下降一个量级。

**Sprint 2（约 2 周）｜画布体验：从「能用」到「顺手」**
1. 边操作（选中/删除/改接/候选端口高亮）、删除快捷键、复制粘贴、全选、批量 ops。
2. 对齐吸附 + 参考线 + 一键对齐；Frame 分组（父子坐标、折叠、组级运行）。
3. undo/redo command stack + 快捷键层 + 输入框守卫。
4. 内联改「一行摘要 + 右侧抽屉」；生成参数 popover；草稿保存。
5. 小地图、ResizeObserver、右键菜单 clamp、处理死按钮。
6. 素材库页面（筛选/分页/反查引用/拖入画布）。
> 验收：40 节点画布 60fps；拖拽零回弹；误删可撤销；生成参数每节点可配。

**Sprint 3｜短剧业务增强**
1. 角色卡实体 + 一致性校验 + seed 锁定；首帧接力（尾帧自动抽帧 → 下一镜首帧）。
2. 镜头表驱动批量生成（顺序生成 + 依赖）；分镜/时间轴联动。
3. 导出：JSON 导入导出、选中节点导出、成片/字幕导出、只读分享。
4. 合成画布内可视化（时间轴视图）、费用与用量面板。
5. （可选）多人协作：事件流 + 光标 + 字段级锁。

---

## 附录 B：接口契约变更清单（建议）

| 变更 | 接口 | 说明 |
|---|---|---|
| 新增 | `POST /api/canvas/{name}/plans` | 提交 GenerationIntent，返回 ExecutionPlan（blockers/费用/参数） |
| 新增 | `POST /api/canvas/{name}/ops` | 字段级 ops 写入，`If-Match` + 幂等键，替代整文档写 |
| 新增 | `POST /api/canvas/{name}/approvals` | 加强版：绑定 plan_hash、金额、过期；现接口只哈希 payload（`app.py:1016`） |
| 新增 | `POST /api/generations` | 单事务提交（job+节点投影+outbox+消费审批），替代 `/api/canvas/run` 与 `/api/canvas/compositions` 两条入口 |
| 新增 | `POST /api/media/jobs/{id}/cancel` | 尽力取消 provider 任务 |
| 新增 | `GET /api/canvas/{name}/events` | SSE 事件流（`Last-Event-ID` 续传） |
| 新增 | `DELETE /api/canvas/nodes/{id}` | 级联删边、软删、revision 校验 |
| 新增 | `GET /api/assets` / `GET /api/assets/{id}/refs` | 素材库查询/反查（`media_assets` 现在只写不读） |
| 新增 | `GET /api/canvas/{name}/export.json` / `POST /import.json` | JSON 分享/导入（含 schema_version 与资产清单） |
| 修改 | `GET /api/canvas/{name}` | 支持 `include_text=false`、返回 `schema_version`/etag |
| 修改 | `/api/canvas/nodes/{id}/text` | 生成卡写入也参与生成（提示词来源优先级），保持 `rendered_by=human` |
| 修改 | `/api/canvas/nodes/{id}/take` | 必带 revision；切版只改指针 |
| 废弃 | `/api/canvas/run` 的 `approved: true` 语义 | 审批必须走 approval_id，禁止前端自批 |
| 废弃 | `?legacy=1` Vue Flow 画布 | 统一到 canvas2（或反向），避免双交互漂移 |
| 收敛 | `update_node` 的 `data` merge | 白名单字段 + 禁止写 `runtime/status/outputs/takes/active_generation_id/content_path` |

---

## 附录 C：审计范围、方法与未确认项

**已审计（只读）**
- 后端：`server/app.py`（路由/审批/run/worker/合成/导出）、`core/canvas.py`（NODE_SPEC/连线/引用/任务计划/渲染/校验）、`core/canvas_repository.py`（revision CAS）、`core/media.py`（Mock/TF-router 适配）、`core/media_job_store.py`（内存/Redis/Postgres 任务存储与租约）、`server/studio.py`（制片台双轨链路）、`core/production.py`、`core/composition.py` 的调用点、`tools/media/tool.py`、`server/registry.py` 的装配、`migrations/0001-0002`。
- 前端：`web-vue/src/canvas2/`（CanvasBoard/NodeCard/PromptBox/ContextMenu/SidebarPanel/GenerationHistoryDrawer/NodeStudio）、`store.js`、`CanvasWorkspace.vue`、`components/CanvasPanel.vue`（Vue Flow 旧版）、`ToonNode.vue`、`utils.js`、`styles/canvas2.css` 的关键类。
- 前端审计来自子代理的静态阅读 + 本 Lead 的关键路径复核（moveNode/NodeCard/PromptBox/CanvasBoard 连线与右键、`save text`/`take` 接口）。

**明确未覆盖**
- 未启动应用、未运行任何测试与端到端流程；所有「现象」均来自代码阅读，运行时行为需在修复前用最小复现验证（尤其拖拽回弹、生成中拖动导致回写失败）。
- 部署形态未确认：默认 `InMemoryMediaJobStore`（`server/app.py:64`）还是生产已启用 Redis/Postgres，直接决定 P1-25 的严重度。
- `VideoProvider.resume`、ComfyUI 类 provider 是否存在（当前仓库只有 Mock 与 TF-router）；`media_assets` 是否有其它读取方（可能在未审的 studio 视图中查询）。
- 前端 Vue Flow 旧版与新版的取舍建议基于「新版是默认入口、且交互更接近目标」；若产品决定回到 Vue Flow，则第 4 节交互方案需重新映射到 Vue Flow API。
- 文中行号以审计时工作区为准（部分文件在未提交改动中，行号可能与 `fb2fa9d` 基线有 ±几行偏差）。

**审计过程中的两个方法论提醒（给后续接手的人）**
- 看到 `node.status`、`node.data.takes`、`active_generation_id` 时，先问「谁写、谁读、冲突怎么办」——这个项目的大多数 bug 都出在「同一份数据两条链路写」。
- 看到 `save(canvas)` 时，先找它的 revision 来源；不带 revision 或裸 save 的地方就是下一次「产物丢失」的现场。

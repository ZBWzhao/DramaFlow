# tests/p0 —— P0 缺陷复现与修复验证

配套文档：`docs/architecture-fix-plan.md`（诊断与架构方案）、`docs/p0-repro-verify-plan.md`（复现矩阵与工期）。
修复记录集中在后者 §7–§10。

## 怎么跑

```powershell
# 仓库根目录；pytest/httpx 已在本机环境里，未写入生产 requirements.txt
$env:PYTHONUTF8="1"   # Windows 控制台中文输出
python -m pytest tests/p0 -q
```

当前预期：**60 passed / 0 failed** —— 已修复并转绿的有：
P0-2（写接口 revision 契约）、P0-1（Worker 写回 CAS 重试）、P0-8（审批/入队事务化）、
P0-7（无连线按自身提示词生成）、P0-9（新式正文读取与体检分级）、P0-10（项目画幅/分镜时长进入生成参数）、
合成路径原子化（compose 与生成同构的提交点，§11）、
P0-12（Agent 工具与画布生成共用参数构造器与写回，产物进历史版本，§12）、
P0-13（任务终态四态语义：succeeded/failed/discarded/unknown，§13）、
P0-14（异常边界收窄：编程错误 500 + traceback、临时故障 503 可重试，§14）、
P0-6（删除节点：级联删边 + 严格 revision，生成中删除按过期丢弃，§15）、
P0-3（拖拽回弹：坐标先写回本地再提交，多选整组落点一次提交，§16——这条的回归网在前端，见下）、
P0-5（视频卡：三层懒加载 + 悬停预览 + 只读灯箱，§17——回归网同样在前端）、
生成进度（Worker 把粗粒度阶段写到 `node.data.generation_progress`，前端画真进度环；总/清空两态见 `test_p0_16_progress.py`）。

前端这批的回归网是另一套（`tests/p0` 是 Python；拖拽落点、时长格式化、首帧地址都是纯前端逻辑）：

```powershell
cd web-vue
npm run test:unit    # node --test "tests/**/*.test.mjs" → 13 passed（positions 5 + video 4 + progress 4）
npm run build
```

## 设计原则

- **零外部依赖**：Provider 用 `FakeProvider` 替换 `server.app.default_registry`，不联网、不花钱；
  任务存储用内存实现（`EVENT_STORE_BACKEND=memory`），不需要 Redis/Postgres。
- **零污染**：每个用例独立 `tmp_path/workspace`，并 `monkeypatch` `server.app.WORKSPACE`、
  `registry`、`media_jobs`、`composition_service`、`tts_registry`；仓库自带 `workspace/` 不会被写。
  制片台路由在 import 阶段就用真实 registry 装配过一次（`server/app.py:2394`），
  夹具会再调 `studio.configure(registry=临时 registry, workspace=临时目录)`，否则 `/api/studio/*`
  会读写仓库里的真实 workspace。
- **走真实链路**：`TestClient` → 真实路由 → 真实审批/入队/嵌入式 `MediaJobWorker` →
  `_execute_media_job` 全流程，只替换 Provider 与存储。
- **对照组**：`test_p0_01_control_single_writer_succeeds` 使用同一夹具、同一 Provider，
  只去掉并发编辑就必须成功 —— 证明失败来自"并发写"本身，而不是夹具或替身的问题。

## 用例清单

| 文件 | 用例 | 现状 | 说明 |
|---|---|---|---|
| `test_p0_00_smoke.py` | `test_app_boots_with_minimal_canvas` | ✅ | 夹具可用、`node_spec` 由服务端下发 |
| | `test_fake_provider_is_active` | ✅ | `/api/media/providers` 读到 FakeProvider |
| | `test_canvas_write_requires_no_missing_fields` | ✅ | P0-2 修复后：缺 revision 必须 4xx，带 revision 才 200 |
| `test_p0_01_generation_writeback.py` | `test_p0_01_control_single_writer_succeeds` | ✅ | 单写者：job succeeded / status=done / takes=1 |
| | `test_p0_01_artifact_survives_concurrent_edit` | ✅ | P0-1 修复后：并发编辑不再丢产物，且并发写入的坐标保留 |
| | `test_p0_01_stale_task_does_not_overwrite_newer_generation` | ✅ | 防串代回归：旧任务写回时整份丢弃，不覆盖 `active_generation_id` 更新的一代 |
| `test_p0_02_optimistic_lock.py` | `test_p0_02_text_write_without_revision_is_rejected` | ✅ | `/nodes/{id}/text` 不带 revision → 400 |
| | `test_p0_02_stale_revision_is_rejected` | ✅ | 带过期 revision → 409（`canvas_revision_conflict`） |
| | `test_p0_02_take_switch_without_revision_is_rejected` | ✅ | `/nodes/{id}/take` 不带 revision → 400 |
| | `test_p0_02_delete_canvas_requires_revision` | ✅ | `DELETE /api/canvases/{name}` 缺 revision → 400、过期 → 409、正确 → 200 且文件消失 |
| `test_p0_07_generation_prompt.py` | `test_p0_07_single_card_uses_own_prompt` | ✅ | 单卡无连线：`ready=True`、`prompt_source="node"` |
| | `test_p0_07_prompt_node_wins_over_card_prompt` | ✅ | 显式 prompt 节点优先，卡片文字标记 `prompt_overridden` |
| | `test_p0_07_text_input_is_fallback` | ✅ | 没有 prompt 节点时退回文本输入 |
| | `test_p0_07_missing_prompt_is_not_ready_with_reason` | ✅ | 三种来源都空 → `ready=False` 且有可读 `ready_reason` |
| | `test_p0_07_validate_allows_single_card_with_own_prompt` | ✅ | 体检不再要求生成卡必须连 prompt 口 |
| | `test_p0_07_end_to_end_single_card_generation` | ✅ | 端到端：新建单卡视频节点 → 审批 → 出片 → takes=1，Provider 收到的提示词就是卡片文字 |
| `test_p0_08_run_atomicity.py` | `test_p0_08_failed_enqueue_does_not_leave_ghost_activation` | ✅ | 入队失败 → 503 可读错误，节点不残留指向不存在任务的 `active_generation_id` |
| | `test_p0_08_failed_enqueue_leaves_approval_reusable` | ✅ | 失败后审批退回 pending，刷新 revision 可用同一份审批重试成功（takes=1） |
| | `test_p0_08_replayed_run_does_not_create_second_job` | ✅ | 重放已成功的请求 → 同一个 job_id、`created=False`、provider 只调一次 |
| `test_p0_09_text_sources.py` | `test_p0_09_read_text_falls_back_to_data_prompt` | ✅ | `read_text` 回退链覆盖 `data.prompt` |
| | `test_p0_09_read_text_reads_outputs_prompt` | ✅ | `outputs.prompt`（人改写正文）优先于卡片文字 |
| | `test_p0_09_read_text_still_reads_content_file` | ✅ | 旧格式 `content.md` 仍可读 |
| | `test_p0_09_validate_accepts_new_style_prompt` | ✅ | 新式正文画布体检不再报"还没有正文" |
| | `test_p0_09_validate_short_body_is_a_suggestion_not_a_problem` | ✅ | 正文长度不再算体检问题 |
| | `test_p0_09_validate_still_reports_missing_body` | ✅ | 真没正文仍然报告 |
| | `test_p0_09_export_includes_new_style_body` | ✅ | 导出 Markdown 包含新式正文 |
| | `test_p0_09_api_exposes_content_for_data_prompt_node` | ✅ | `GET /api/canvas` 的 `content` 返回新式正文 |
| | `test_p0_09_alternating_writers_keep_latest_body` | ✅ | 画布 `/text` 与制片台 `/studio/.../prompt` 交替写同一节点：读侧永远是最新正文（写侧只保留一个 STRING 槽位） |
| | `test_p0_09_studio_segment_view_reads_same_body` | ✅ | 制片台 `_node_text` 委托 `Canvas.read_text`：`data.prompt` 正文在片段详情里也可见 |
| `test_p0_10_media_params.py` | `test_p0_10_params_come_from_project_and_storyboard` | ✅ | 9:16 + 720P + 分镜 6 秒 → `duration=6 / size=720x1280` |
| | `test_p0_10_node_params_override_project` | ✅ | 节点级 `duration/ratio/quality` 覆盖项目设置 |
| | `test_p0_10_params_are_none_without_sources` | ✅ | 没有来源时返回 None，交给请求参数（不猜默认值） |
| | `test_p0_10_video_request_uses_shot_duration_and_project_ratio` | ✅ | 端到端：Provider 收到 `6s / 9:16 / 720p` |
| | `test_p0_10_image_request_uses_portrait_size` | ✅ | 图片节点按竖屏出 `720x1280` |
| | `test_p0_10_request_defaults_still_apply_without_canvas_settings` | ✅ | 旧画布没有项目设置时退回 `5s / 16:9 / 720p`，行为不变 |
| `test_p0_11_compose_atomicity.py` | `test_compose_control_happy_path` | ✅ | 对照组：无并发/无故障时合成正常受理 |
| | `test_compose_survives_unrelated_edit_after_approval` | ✅ | 审批后无关编辑（revision 前进）不让合成作废 |
| | `test_compose_activation_survives_competing_edit` | ✅ | 激活落盘瞬间被并发写抢先：CAS 重试成功，不 409 吃掉审批 |
| | `test_compose_enqueue_failure_releases_approval` | ✅ | 入队失败 → 503、无幽灵激活、审批退回 pending，同一审批可重试成功 |
| | `test_compose_retry_after_edit_reuses_same_job_id` | ✅ | 同一审批重试复用同一 `job_id`（不重复计费） |
| `test_p0_12_agent_tool_parity.py` | `test_tool_uses_node_params_when_node_given` | ✅ | Agent `media_generate` 指定节点时用节点的画幅/时长/画质，不再硬编码 1280x720 |
| | `test_tool_writes_take_history_for_node` | ✅ | Agent 生成写回 `outputs` + `takes` + `current_take_id`，可在历史版本里切换 |
| | `test_tool_marks_node_error_on_provider_failure` | ✅ | Provider 失败：节点 `status=error`，不写入 take |
| | `test_run_canvas_uses_shared_request_builder` | ✅ | `media_run_canvas` 的请求走共享构造器：分镜 6 秒 + 9:16 进入视频请求 |
| | `test_run_canvas_unready_job_skipped_with_reason` | ✅ | 没提示词的节点跳过并说明原因，不调用 Provider |
| | `test_run_canvas_writes_takes` | ✅ | 批量生成同样落 `takes`（Agent 路径与画布路径一致） |
| `test_p0_13_stale_status.py` | `test_stale_task_is_marked_discarded` | ✅ | 产物被新一代任务取代：job 记 `discarded`，节点已有 `gen_newer`、takes/outputs 为空 |
| | `test_partial_provider_failure_is_not_success` | ✅ | 批次里有节点真的失败：job 记 `failed` 且带失败原因，节点停在 `error` |
| | `test_success_still_reports_succeeded` | ✅ | 对照组：正常出片仍是 `succeeded` |
| `test_p0_14_error_boundary.py` | `test_programming_error_returns_500_not_503` | ✅ | 提交点里的 `TypeError` → 500（不再伪装成 503"请重试"），审批退回、无幽灵激活 |
| | `test_transient_error_returns_503_and_retry_succeeds` | ✅ | `ConnectionError` → 503 且可读；同一审批重试成功（takes=1） |
| | `test_canvas_conflict_retries_with_backoff` | ✅ | 连续 2 次 `CanvasConflictError` 后第 3 次写成功：CAS 退避重试不再一次冲突就失败 |
| `test_p0_15_node_delete.py` | `test_delete_node_removes_node_and_incident_edges` | ✅ | 删除节点：节点消失、入边/出边级联删除、无关节点保留、revision 前进 |
| | `test_delete_node_without_revision_is_rejected` | ✅ | 不带 revision → 4xx，节点仍在 |
| | `test_delete_node_with_stale_revision_is_rejected` | ✅ | 过期 revision → 409 + `canvas_revision_conflict`，节点仍在 |
| | `test_delete_missing_node_returns_404` | ✅ | 删不存在的节点 → 404 |
| | `test_deleted_prompt_node_makes_generation_not_ready` | ✅ | 删提示词节点后，生成卡回到 `ready=False` 且有 `ready_reason` |
| | `test_deleting_node_during_generation_discards_result` | ✅ | 生成中删节点：任务 `discarded`、不复活节点、不落 take、不判失败 |
| `test_p0_16_progress.py` | `test_progress_is_written_while_generating_and_cleared_after` | ✅ | 生成中 `data.generation_progress` 有 step/total（total=3），成功后清空、产物仍写入 |
| | `test_progress_is_cleared_when_generation_fails` | ✅ | Provider 失败后进度清空、节点 `error`，不残留"生成中 2/3" |

## 已核实的现场（2026-02，HEAD 工作区）

- `tests/` 此前不存在；本目录是仓库第一个 Python 测试目录。
- 最小画布用领域 API 构造（`core.canvas.Canvas.add_node/connect/save`）而非 HTTP 建节点，
  避免把"建节点接口未来变化"耦合进夹具。
- 注册表类名是 `server.registry.SessionRegistry`（`server/registry.py:83`）。
- 任务状态机：`queued → running → succeeded/failed/discarded/unknown`；`discarded` 表示"出片了但被新一代任务取代、没写回画布"（钱已花、不可重试），`unknown` 表示远端结果不确定、禁止自动重试（`core/media_job_store.py:69-78`）。

## 已知边界

- `FakeProvider` 只验证编排，不验证真实 Provider 的限流/审核/超时语义；上线前仍需一次真实 key 的 smoke。
- P0-1 的用例是**放大竞态**：真实时序里 Worker 在写回前会重新 `load`，冲突窗口只有毫秒级；夹具在"写回 done+take"的瞬间注入一次并发写（`supersede_generation_before_writeback` 则注入"又点了一次生成"），走的是同一条 CAS 路径，语义等价。
- ~~任务表把"正常返回但产物被丢弃（stale_discarded）"仍记成 `succeeded`~~ —— P0-13 已修（§13）：任务终态区分 `discarded`，`/api/media/jobs` 原样返回，前端 `waitForMediaJob` 不再把它当失败抛错。
- 文件画布仓储在 Windows 上依赖 `os.replace` 重试，极少数情况下会拖慢用例；目前未观察到 flaky。
- P0-8 的"入队失败"是 monkeypatch `media_jobs.enqueue` 抛出异常模拟的，不覆盖"调用超时但服务端实际已写入"的不确定场景（撤销/对账属于 P1）。
- 合成（compose）用例同样用 `enqueue` 替身：只验证"审批 → 激活 → 入队"这段提交语义，不跑真实 ffmpeg 渲染（渲染链路由 `demo/composition_invariants.py` 覆盖）。`composition_service.inspect` 也被替换成假元数据，避免 ffprobe 引入平台差异。
- P0-10 只打通"数据链"：前端还没有"每张卡单独选时长/画幅/模型"的 UI；分镜时长之和被 clamp 到 provider 的 1–30 秒，超限时不会提示用户。
- P0-12 统一的是"参数口径 + 写回"：Agent 工具仍以 `media_approved` 布尔门控，没有画布链路那套"审批哈希冻结参数 + 幂等 invocation"；真实 Agent 侧要等 P0-4（事件流/任务模型）一起收口。
- P0-14 的"临时故障"判定基于异常类型/模块白名单（超时、连接、`OSError`、`RuntimeError`、redis/asyncpg/psycopg 等），不解析错误文案；未列入白名单的第三方异常会按 500 处理——这是有意的："无法确认可重试"就不承诺可重试。
- P0-6 是**硬删除**（没有回收站/撤销）：删除后节点与连线从画布 JSON 中消失，产物文件仍保留在 `assets/`（可能被历史/合成引用）；"生成中删除"只保证结果被丢弃、不复活节点，不保证立刻停止远端调用（Provider 已发出的请求照常跑完并计费，取消属于 P0-4）。
- P0-6 只接入自研画布 `canvas2`；旧 Vue Flow 画布（`?legacy=1`）没有删除入口，正在逐步下线。
- P0-3 的坐标逻辑在 `web-vue/src/canvas2/positions.js` 有单测，但"松手后卡片有没有停在落点"仍需人在浏览器里看一遍（`npm run build` 只能证明能打包，证明不了交互）。拖动中不持续写库：拖到一半刷新会回到拖动前的位置，这是有意的取舍（避免一拖几十个 revision）。
- P0-5 的视频卡没有自动化测试（组件依赖 `<video>` 解码，headless 环境不稳定）：回归网是 `video.js` 的 4 条纯函数单测 + 人工浏览器验收。已知边界：悬停一律静音（不做声音预览）；far 缩放（<45%）只显示骨架不显示缩略图；首帧依赖 `/assets` 的 Range 支持；灯箱只读，版本切换仍在历史抽屉。
- 前端单测用 Node 自带 runner（`node --test`，Node 22+），没有引入 vitest/jest 之类的依赖。
- P0-14 的 500 分支同样会退回审批并回滚激活标记，但**不会重试**；前端目前对 500 也只显示"失败"，区分"可重试/需人工处理"留到 P0-4 的状态机。

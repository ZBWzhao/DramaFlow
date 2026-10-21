# P0 复现与修复验证方案（一页）

> 配套文档：`docs/architecture-fix-plan.md`（完整诊断与架构方案）。
> 本文只解决一件事：**把 10 条 P0 变成「可复现的 RED → 可断言的 GREEN」**，让修复有证据、回归有门禁。

## 0. 先修哪个方向

**结论：第一批修「写入一致性」——P0-2 → P0-1 → P0-8（revision 契约 → CAS 收敛 → 审批/入队事务化），同时并行修「生成正确性」P0-9/P0-7/P0-10。前端 P0-3/P0-5/P0-6 放在同一周的后半段。P0-4（异步化）最后做。**

排序判据（同时满足越多越先修）：① 后果不可逆（丢钱/丢数据）；② 是其它修复的前置（否则二次返工）；③ 改动收敛、不依赖产品决策；④ 能立刻建起回归网。

| 顺序 | 方向 | 覆盖 P0 | 理由 |
|---|---|---|---|
| 1 | 写入一致性 | P0-2、P0-1、P0-8 | 唯一「钱花了产物没了」且不可逆；P0-1 依赖 P0-2 的 revision 契约，P0-4 的异步改造也要先有稳定写回，否则一起返工；改动集中在 repository + run 路径 |
| 2 | 生成正确性 | P0-9、P0-7、P0-10 | 决定「能不能生成出对的东西」；基本是只读路径 + 小改，1 天可完成，可与方向 1 并行 |
| 3 | 画布可用性 | P0-6、P0-3、P0-5 | 不丢命但阻断编辑与验收（看不到成片、删不掉废稿、拖不动） |
| 4 | 异步体验 | P0-4 | 面最大（前后端 + 任务设施），务必等回归网建好后再动 |

## 1. 复现环境（先搭 0.5 天，一次投入）

现状：仓库**没有 Python 测试目录/pytest 配置**，前端只有 `web-vue/scripts/stream-selftest.mjs`；`server/app.py` 的 `WORKSPACE` 是模块级常量（`server/app.py:36`），默认启用嵌入式 worker（`MEDIA_WORKER_EMBEDDED=true`，`:100`）。

| 夹具 | 内容 | 用途 |
|---|---|---|
| `tests/p0/conftest.py` | tmp workspace（含 `assets/<id>/content.md` 与最小画布 JSON）；`monkeypatch.setattr(server.app, "WORKSPACE", tmp)`；`ApplicationRegistry(workspace=tmp)`（file 后端，避免 Postgres） | 所有用例的隔离环境，禁止污染仓库 `workspace/` |
| `FakeProvider` | 实现 `list_models/generate/resume/describe/activate`；可配延迟（默认 1s）、可注入失败/挂起；产出真实字节的小 PNG / 假 mp4（mime 与 kind 匹配） | 控制生成时长，稳定复现「生成期间用户操作」的并发窗口 |
| `FakeRegistry` | 替换 `server.app.default_registry`（模块级导入，`:44`） | 不触网、不花钱、可重复 |
| 最小画布 | prompt 节点 + `video_gen` 节点 + 一条 `prompt→video_gen` 边 + 一张已登记图片资产；用现有 `POST /api/canvas/nodes` 与 `/connect` 生成，而非手写 JSON | 贴近真实文档结构 |
| 测试客户端 | `fastapi.testclient.TestClient`（需 dev 依赖 `pytest`、`httpx`，不写入生产 `requirements.txt`） | 接口级 RED/GREEN |

**RED 优先**：先写 10 个失败测试并提交日志，证明「现在确实坏」，再开始改代码。每个 P0 一个文件：`tests/p0/test_p0_01_writeback_cas.py` … `test_p0_10_ratio_duration.py`。

## 2. 十条 P0 的复现与验证矩阵

| P0 | 复现（最短步骤） | 现状 RED 观测点 | 修复后 GREEN 断言 | 层级 |
|---|---|---|---|---|
| P0-1 | 生成 1 个视频（FakeProvider 延迟 2s）；期间 `POST nodes/{id}/position` 改坐标（revision 5→6）；等 job 结束 | job=`failed`（`CanvasConflictError`），节点无 outputs/takes | job=`succeeded`；outputs 有产物、takes=1 条、`status=done`；坐标改动保留、revision 单调 | 接口+仓储 |
| P0-2 | 不带 revision 调 `/nodes/{id}/text`、`/nodes/{id}/take`、`DELETE /canvases/{name}` | 全部 200 并覆盖；take 无 revision 参数 | 缺失/过期 → 409（带服务端当前 revision）；带正确 revision → 200 | 接口 |
| P0-3 | 前端 store 单测：drag 节点 100px → `moveNode` resolve → 读 `nodes[id].position`；模拟 5 次连续拖动 | 坐标未写回、松手回弹，位移累积丢失 | 立即为新坐标；失败时回滚原坐标并提示；5 次拖动位移累计正确 | 前端单测（需引入 vitest）或 Playwright 手工 |
| P0-4 | 点击生成，测点击到函数返回耗时；连点 10 次；再调 cancel | 等待 30 分钟级；10 次点击产生 10 组 approval/job；无 cancel | ≤300ms 返回 `job_ids`；10 次点击只 1 个 job；cancel 后节点=`cancelled` | 接口+前端 |
| P0-5 | 生成成功后检查节点 DOM | 视频渲染为 `<img>` 破图、无 poster/preload | 存在 `<video preload="none" poster>`；未进视口时不请求视频字节 | 前端单测/手工 |
| P0-6 | `DELETE /api/canvas/nodes/{id}` | 404/405（无路由） | 200；关联边级联删除；revision+1；takes 仍可回看（软删） | 接口 |
| P0-7 | 建独立 `video_gen`（`data.prompt` 有值、无入边），调 generation-plan | `ready=false`、prompt 为空 | `ready=true`、prompt==`data.prompt`；有入边时按优先级取入边（用例覆盖 3 级优先级） | 接口/领域 |
| P0-8 | 消费 approval 后、入队前 monkeypatch `enqueue` 抛异常 → 再重试 | approval 已作废，重试 428；任务未入队 | 事务回滚：approval 可用、重试成功；或补偿后返回明确错误且可重试 | 接口 |
| P0-9 | prompt 节点写 `data.prompt` → 调 `render_all`/generation-plan；跑 `validate` | 读到空串；validate 报「缺 content_path/<500 字」 | 输出含正文；validate 内容长度只出 warning、结构问题才 error | 领域 |
| P0-10 | 项目 meta 设 9:16、1080x1920；分镜 `duration_s=7`；调 generation-plan | ratio=16:9、size=1280x720、duration=5 | ratio=9:16、size=1080x1920、duration=7（能力不支持时给 blocker） | 接口/领域 |

## 3. 执行顺序与工期（1 人约 6～7 个工作日）

| 天 | 交付 | 依赖 |
|---|---|---|
| D0.5 | 夹具 + FakeProvider + P0-1/P0-2 的 RED 测试 | — |
| D1–D2 | P0-2 revision 契约（全部写接口 + 前端 mutation 带 revision）→ P0-1 repository 统一写方法（CAS 重试 + 字段合并）+ worker 改调用 | D0.5 |
| D2–D3 | P0-8 审批/入队事务化（失败整体回滚） | D1 |
| D2–D3 并行 | P0-9 → P0-7 → P0-10（纯读路径，互不冲突，可与写路径并行） | D0.5 |
| D4 | P0-6 删除节点（路由/级联/软删/前端 Delete 键） | P0-1、P0-2 |
| D4–D5 | P0-3 拖拽乐观更新 + 409 合并提示；P0-5 video 卡 lazy/poster | 前端独立 |
| D5–D7 | P0-4 异步化（提交即返回 + 单飞 + cancel + 前端订阅；SSE 可延后，先用 1s 轮询兜底）+ 全量回归 | D1、D2、D4 |

## 4. 验证门禁

1. **每条 P0 必须先 RED 再 GREEN**：测试失败日志随 PR 提交（证明复现成立），修复后同测试通过。
2. **自动化门禁**：`pytest -q tests/p0` 全绿；`python -m compileall server core`；前端 `npm run build`；修复前后各跑一次同一脚本，输出 JSON 快照做 diff（如 `job/public()` 与节点快照）。
3. **手工回归清单（真实浏览器，6 条）**：① 生成中拖动节点，产物不丢；② 连续拖 5 次不回弹；③ 单卡文生视频可出片；④ 视频卡可播放/有海报；⑤ 删除节点后边消失且可撤销；⑥ 9:16 项目出片比例正确。
4. **观测指标**：模拟 100 次「生成中拖动」→ 产物丢失 0、静默覆盖 0；重复点击 10 次 → job=1；生成按钮响应 ≤300ms；视频卡首屏视频字节=0；`unknown` 任务全部有可解释状态与人工入口。

## 5. 风险与边界

- FakeProvider 只验证编排，不验证真实 provider 契约（超时/限流/内容审核）；上线前需 1 次真实 provider smoke（有 key 时）。
- Windows 文件锁可能 flaky：用例统一用短 `lock_timeout`，或主用例用内存仓储 + 单独 1 条文件仓储并发用例。
- `WORKSPACE` 是模块级常量：夹具必须同时 monkeypatch `server.app.WORKSPACE` 与 `ApplicationRegistry(workspace=tmp)`，否则会写进仓库目录。
- P0-4 跨前后端，建议加 `generation_async` 开关保留旧路径一周，灰度后再删。
- 修复期间**冻结 UI 新功能**，只做 P0 与回归；否则验证矩阵无法闭环。

## 6. 落地状态（首批：夹具 + P0-1/P0-2 的 RED 已成立）

已交付 `tests/p0/`（仓库首个 Python 测试目录，未改任何业务代码）：

| 文件 | 内容 |
|---|---|
| `tests/p0/conftest.py` | `FakeProvider`（`list_models/generate/resume/describe/activate`，可配 `delay`/`fail_with`）+ `SessionRegistry` 隔离（file 画布仓储、内存任务存储、tmp 会话目录）+ `server.app` 模块级 `WORKSPACE/registry/media_jobs/default_registry/composition_service/tts_registry` 全部 monkeypatch + `TestClient`（真实 lifespan，含嵌入式 Worker） |
| `tests/p0/test_p0_00_smoke.py` | 3 条，验证夹具与最小画布可用 |
| `tests/p0/test_p0_01_generation_writeback.py` | 1 条对照组（单写者必成功）+ 1 条 RED（并发编辑） |
| `tests/p0/test_p0_02_optimistic_lock.py` | 4 条：2 条缺失 revision（text / take）+ 1 条过期 revision 409 + 1 条删除画布缺 revision |
| `tests/p0/README.md` | 运行方式、用例清单、已核实现场、已知边界 |

运行：`python -m pytest tests/p0 -q` → 首次 **5 passed / 3 failed**（3 条失败 = 待修缺陷）；修完 P0-2 后 **8 passed / 1 failed**（仅剩 P0-1 并发条）；修完 P0-1 后 **10 passed / 0 failed**（含 1 条防串代回归用例）。

### RED 证据（首次运行，原样摘录）

| 用例 | 观测 |
|---|---|
| `test_p0_01_artifact_survives_concurrent_edit` | `job=failed`，`error="CanvasConflictError: 画布 main 已被其他用户或 Agent 更新（期望 revision=3，实际=4），本次修改未覆盖新版本，请刷新后重试"`；节点 `status=generating`；`outputs={}`；Provider 已调用 1 次（费用已发生）；并发写入的坐标 `(999,999)` 保留 —— 即"钱花了、产物丢了、节点卡死、且不会自动重试" |
| `test_p0_02_text_write_without_revision_is_rejected` | `POST /nodes/{id}/text` 无 revision → `200 {"ok":true,...,"revision":2}` |
| `test_p0_02_take_switch_without_revision_is_rejected` | `POST /nodes/{id}/take` 无 revision → `200 {"ok":true,...,"current_take_id":"take_p0","revision":3}` |

GREEN 判据：上述 3 条转绿，且 2 条对照组 + 3 条 smoke 保持绿；全量 `pytest -q tests/p0` 无 flaky。

### 与本文原方案的差异（已核实）

1. 注册表类名是 `server.registry.SessionRegistry`（§1 中 `ApplicationRegistry` 为笔误）。
2. 最小画布用领域 API `core.canvas.Canvas.add_node/connect/save` 直接构造并落盘，未走 `POST /api/canvas/nodes`：少 4 个 HTTP 往返，也不把"建节点接口自身可能变"耦合进夹具。
3. **P0-1 的复现是放大竞态**：真实时序中 Worker 写回前会重新 `load`（`server/app.py:1601`），自然冲突窗口只有毫秒级；夹具在"写回 `done + take`"的瞬间注入一次并发写，走的是同一条 CAS 路径，语义等价、可稳定复现。因此 P0-1 的严重性不在"概率高"，而在"一旦发生不可恢复（不重试、无补偿）"。
4. P0-2 两个接口的现状不同：`/text` 有 `revision` 字段但 `_require_revision(canvas, None)` 放行（`server/app.py:1902`、`:277-279`）；`/take` 的请求模型里根本没有 revision 字段（`server/app.py:1960-1962`）。修复时两处都要补，且前端调用要带上 revision。

### 下一步（按 §3 的 D1–D2）

1. ~~**P0-2 先行**~~ → 已完成（见 §7）。
2. ~~**P0-1 随后**~~ → 已完成（见 §8）：新增"读-改-写重试"统一入口 `_mutate_canvas_with_retry`（冲突时重载 → 只重放本 Worker 的增量），Worker 6 处写回全部改调用该入口；`approval/claim/active_generation_id` 语义不变。
3. ~~两条都绿后，再按 §3 推进 P0-8（审批/入队事务化）~~ → **P0-8 已完成（见 §9）**。下一批按 §3 的 D2–D3 进 P0-9 / P0-7 / P0-10（生成正确性：正文读不到、无连线不能生成、规格写死）。

## 7. 修复记录（P0-2：写接口 revision 契约）

改动面（4 处后端 + 4 处前端，未动仓储与生成链路）：

| 位置 | 改动 |
|---|---|
| `server/app.py:282-296` | 新增 `_require_write_revision(canvas, expected)`：`None` → 400（"缺少画布版本号 revision…"），过期 → 抛 `CanvasConflictError`（409 + `code=canvas_revision_conflict`）。与宽松的 `_require_revision` 并存，后者只留给尚未改契约的旧接口（P1 收口） |
| `server/app.py:1935` | `POST /api/canvas/nodes/{id}/text` 改用 `_require_write_revision` |
| `server/app.py:1981-1995` | `SelectTakeRequest` 增加 `revision`；`POST /api/canvas/nodes/{id}/take` 改用 `_require_write_revision` |
| `server/app.py:1846-1857` | `DELETE /api/canvases/{name}` 增加 `?revision=` 查询参数并强制校验（删除前不写盘，校验在 `load` 之后） |
| `web-vue/src/store.js` | `savePrompt` / `selectTake` 请求体补 `revision: state.canvasServerRevision`；`renameCanvas` 修正"当前画布取 `state.canvas.revision`（恒为 undefined）"→ 改取 `state.canvasServerRevision`；`deleteCanvas` 补 `?revision=` |
| `tests/p0/test_p0_00_smoke.py` | 原"接受无 revision"的现状断言改为：缺 revision 必须 4xx、带 revision 才 200 |

验证：`python -m pytest tests/p0 -q` → **8 passed / 1 failed**（剩余失败 = P0-1 并发写回，符合预期）；`cd web-vue && npm run build` → 成功（1642 modules，无语法错误）。

遗留（P1 收口，不在本批）：`position / connect / edges/delete / nodes（创建、更新）/ upload-media / rename / run / compose` 仍走宽松版 `_require_revision`。前端这些调用本来都带 revision，所以把 `_require_revision` 统一改严格是"零产品变更"的后续小步，但会打断 `web/canvas.js`（旧版静态画布）与 `demo/*.py` 手工脚本 —— 需要连同这些调用点一起改，单独一批做。

## 8. 修复记录（P0-1：Worker 写回 CAS 重试）

改动面（1 个新入口 + 6 处写回，未动生成参数、审批、幂等语义）：

| 位置 | 改动 |
|---|---|
| `server/app.py:301-334` | 新增 `_mutate_canvas_with_retry(canvas_name, mutate, attempts=5)`：循环「load 最新画布 → `mutate` 里**只重放本任务的增量** → save」，捕获 `CanvasConflictError` 后立即重读重试；`mutate` 返回 `False` 表示前提已不成立（节点已有更新一代任务），调用方按"过期丢弃"处理。冲突重试用尽才抛错 |
| `server/app.py:1553-1565` | 幂等重放写回：改为闭包 `apply_replay`，冲突不再让整个 job 失败（结果已在 invocation 里） |
| `server/app.py:1573-1585` | `unknown` 无 operation_id → 节点标记 error：改闭包 |
| `server/app.py:1591-1603` | 进入执行前置 `generating`：`mutate` 内重做 `active_generation_id` 校验，返回 `False` 即按"开始前已过期"丢弃 |
| `server/app.py:1607-1617` | `mark_executing` 冲突（RuntimeError）→ 标记 error：改闭包；写回冲突时保留原始原因 |
| `server/app.py:1660-1673` | Provider 失败 → 标记 error：改闭包，`CanvasConflictError` 吞掉（原始失败原因优先） |
| `server/app.py:1683-1732` | **成功写回**（outputs / takes / current_take_id / status=done）：核心修复。`take_id` 在重试外只生成一次，重试是"同一笔增量再贴一遍"，不会产生重复 take；`mutate` 每次都在最新画布上重做 `active_generation_id` 校验，过期则走原有 stale 路径 |

验证：`python -m pytest tests/p0 -q` → **10 passed / 0 failed**（含 `test_p0_01_control_single_writer_succeeds`、`test_p0_01_artifact_survives_concurrent_edit`，以及新增的防串代回归 `test_p0_01_stale_task_does_not_overwrite_newer_generation`）；`python -m compileall -q server core tests` → OK。

设计取舍：

1. **重试放在写回侧而不是任务侧**：任务重试会重新调用 Provider（重复计费）；写回重试只重复"读 + 合并 + 保存"，不产生额外费用。这也对应 `docs/architecture-fix-plan.md` 里"CAS 合并重试"的结论 —— 但当前实现放在 `server/app.py`（Worker 与任务语义都在这里），而不是 repository 通用方法：`mutate` 闭包天然表达"这次写回属于哪个任务、前提是什么"，比在仓储层拼通用 merge 规则更不易误合并。
2. **增量重放而非整份覆盖**：闭包只改本节点的 `status/outputs/takes/current_take_id`，用户并发写入的坐标、正文、其他节点不受影响 —— 测试里断言 `prompt.position == {999,999}` 就是防"修成覆盖"。
3. **冲突不是错误**：只有 5 次都失败才抛错并让 job 失败；单次冲突走 `asyncio.sleep(0)` 让出事件循环后重读（并发写通常已落地，第二次即成功）。
4. **未覆盖**：合成（composition）路径已有自己的 4 次 CAS 重试（`_save_composition_state`），本批不动；`approval consume → enqueue` 的非事务窗口属于 P0-8，下一批处理。

## 9. 修复记录（P0-8：审批作废与任务入队事务化）

RED（新增 `tests/p0/test_p0_08_run_atomicity.py`，修复前原样）：

- 入队抛异常时**未捕获异常直接冲出 ASGI**（TestClient 只能 `pytest.raises`），没有任何可读错误；
- 失败后画布上已留下幽灵激活标记：`node.data.active_generation_id` 指向一个不存在的任务，节点永久等待；
- 恢复存储后**同一份审批重试 → 409 `{"detail":"审批已经使用或失效"}`**：consume 已把审批置为 consumed，而失败时画布 revision 已前进、执行计划的 `canvas_revision` 变了，哈希对不上 → `RuntimeError("审批已经使用或失效")`，用户既没生成也无法重试。

改动面（1 个补偿 API + 1 个哈希口径 + run 提交点重构）：

| 位置 | 改动 |
|---|---|
| `core/invocation_store.py:127-131` | `ApprovalStore` 协议新增 `release(approval_id) -> bool`：把"已消费但下游提交失败"的审批退回 `pending`；只回退 consumed，pending/expired/不存在都是幂等空操作 |
| `core/invocation_store.py:167-181` | `InMemoryApprovalStore.release`：`status="consumed"` 才置回 pending 并清 `used_at` |
| `core/invocation_store.py:299-305,347-352` | `RedisApprovalStore._RELEASE` Lua（EXISTS→-1 / 非 consumed→0 / 其余回退）+ 调用；三实现同步加，避免切换后端时补偿失效 |
| `core/invocation_store.py:510-520` | `PostgresApprovalStore.release`：`UPDATE ... WHERE status='consumed' RETURNING`，并发安全 |
| `server/app.py:333-341` | 新增 `_approval_hash(payload)`：审批哈希**排除 `canvas_revision`**，只绑定执行计划（提示词/参考素材/模型/时长/比例/provider）。revision 会因无关写入前进；把它算进哈希会把"一次可重试的失败"变成"审批永久作废" |
| `server/app.py:344-350` | `_release_approval_quietly`：补偿失败不得覆盖原始异常 |
| `server/app.py:1099,1296` | 生成/合成两处审批创建改用 `_approval_hash(payload)` 作为 `args_hash` |
| `server/app.py:1129,1324` | 两处 `consume` 改用同一哈希口径 |
| `server/app.py:1138` | `job_key` 改为 `canonical_hash({approval_id, plan_hash})`（同样去 revision）：失败重试、HTTP 重放都落到**同一个 job_id**，不会新开一单重复计费 |
| `server/app.py:1172-1224` | run 的"提交点"重构：try 内先做幂等查询 `media_jobs.get(job_id)` → `activate`（CAS 合并写 `active_generation_id`）→ `enqueue`；任一步失败 → `revert_activation` 还原激活标记 + `release` 退回审批 + 503「审批已退回，请重试」；`CanvasConflictError` 走原 409 处理器（激活未落盘，无需回滚）。幂等查询也放进 try：任务存储不可用时不能先消费审批 | 

修复后行为（三条新用例全绿）：

1. 入队失败 → **503 可读错误**，节点 `active_generation_id` 回到原值（无幽灵标记）；
2. 恢复存储后**刷新 revision、用同一份审批重试 → 200 且成功出片**（`takes=1`，没有多扣一次模型费用）；
3. **成功之后重放同一请求 → 同一个 job_id、`created=False`、provider 只被调用一次、takes 仍为 1**（幂等重放不重复下单）。

设计取舍：

1. **激活先于入队，失败再回滚**：反过来（先入队后激活）会打开"Worker 已认领任务、但节点的 `active_generation_id` 还没写入"的窗口，Worker 会把任务当过期丢弃 —— 不花钱但产物静默丢失。先激活再入队，代价是失败时要回滚一次（CAS 合并写，且回滚本身失败也不影响正确性：幽灵标记指向的任务不存在，下次生成会覆盖）。
2. **审批哈希去 revision，而不是把 revision 也存成"审批快照"**：后者要引入预留/两阶段提交；而计划哈希已经覆盖所有花钱字段，"计划没变"就允许执行是安全的。请求级 `_require_revision` 仍然要求调用方拿最新画布（`server/app.py:1118`）。
3. **job_id 去 revision**：让"同一份审批"永远映射到同一个任务，幂等重放与失败重试共用同一单；代价是 job_id 不能再当"快照指纹"用（它本来也不是）。
4. **未覆盖**：(a) 入队调用抛出"超时但服务端实际写成功"的不确定场景 —— 重试会命中已存在任务并跳过激活，语义安全但需要一次刷新才能发现；(b) 合成（compose）路径只统一了哈希口径，`consume → enqueue` 的事务化与回滚未做（与 §8 同口径，单独立项）；(c) `release` 不校验 scope/args_hash（调用方刚刚 consume 成功，此处不存在伪造路径）。

验证：`python -m pytest tests/p0 -q` → **13 passed / 0 failed**（12 条既有 + 新增 `test_p0_08_replayed_run_does_not_create_second_job`）；`python -m compileall -q server tests` → OK。

## 10. 修复记录（P0-7 / P0-9 / P0-10：生成分支、正文来源、媒体参数）

三条缺陷共用同一条生成计划（`Canvas.generation_jobs` → 审批计划 → Worker），所以一起修，避免"改三次同一段代码"。

RED（新增三个测试文件，修复前原样）：

- `tests/p0/test_p0_09_text_sources.py`（10 条）：`read_text` 对 `data.prompt` / `outputs.prompt` 返回空串；`validate` 对新式 prompt 节点报"还没有正文"；`GET /api/canvas` 的 `content` 为空；`render_all` 导出缺正文；正文 <500 字被当成体检问题；两个写侧交替写同一节点时旧槽位盖住新正文；制片台详情读不到 `data.prompt`。
- `tests/p0/test_p0_07_generation_prompt.py`（6 条）：单卡 `ready=False`、`prompt` 为空；连了 prompt 节点时卡片自身提示词被静默丢弃；`validate` 要求生成卡必须连 prompt 口；端到端单卡生成拿不到 Provider 调用。
- `tests/p0/test_p0_10_media_params.py`（6 条）：项目 9:16 + 分镜 6 秒的镜头，请求仍是 `5s / 16:9 / 1280x720`；图片节点忽略竖屏比例；节点级 `duration/ratio/quality` 无效。
- 合计修复前：**17 failed / 16 passed**（16 绿 = 既有 13 条 + 3 条本就不该坏的基线）。

改动面：

| 位置 | 改动 |
|---|---|
| `core/canvas.py:108-146` | 新增 `normalize_resolution()` / `media_size()`：画幅 + 画质 → provider 认识的 `720p` 与偶数边长 `WxH`（`9:16` + `720P` → `720x1280`），与 `server/studio.py` 的 `_default_size` 同口径 |
| `core/canvas.py:670-698` | `read_text` 改为单入口回退链：`content.md` 文件（非空）→ `outputs.prompt` → `outputs.text` → `outputs.shots` → `data.prompt`。生成、体检、导出、API、制片台全走它（`server/studio.py:_node_text` 改为委托，删掉自己的第二套顺序） |
| `core/canvas.py:712-800` | 新增 `project_meta()` / `_segment_seconds()` / `generation_params()` / `resolve_prompt()`：画布层只做**解析**（节点 > 分镜时长 > `meta.project` > 空），不猜默认值 |
| `core/canvas.py:847-860` | `validate`：正文判定改用 `read_text`；去掉 `content_path` 与 ≥500 字硬要求；生成卡只在三种提示词来源都为空时报问题 |
| `core/canvas.py:1223-1250` | `generation_jobs` 不再自己拼提示词：改用 `resolve_prompt` / `generation_params`，job 增加 `prompt_source`、`prompt_overridden`、`ready_reason`、`params` |
| `server/app.py:1045-1100` | `_resolved_media_params()` + 计划里每个 job 带上合并后的 `duration/ratio/resolution/size`（画布为准、缺失退回请求参数） |
| `server/app.py:1150-1180` | `frozen_jobs` 冻结上述参数：审批时看到的画幅/时长就是 Worker 实际执行的参数 |
| `server/app.py:1700-1712` | Worker 的 `media_request` 改用 job 冻结值，不再按 kind 硬编码 `1024x576` / `1280x720` |
| `server/app.py:1768` | take 的 `duration_s` 记录本单实际时长（不再是全局请求值） |
| `server/app.py:2036-2041` | `GET /api/canvas` 的 `content` 无条件走 `read_text`（卡片详情能看到新式正文） |
| `web-vue/src/store.js:361-373` | `runCanvas` 的画幅/画质从 `state.canvas.meta.project` 取，不再写死 `16:9` / `720p` |

设计取舍：

1. **正文读取"文件优先"**：`content.md` 是多人/Agent 共同维护的长文，文件存在且非空时它最权威；`outputs.prompt` 次之（人改写的片段正文），`data.prompt` 最后（卡片内联短提示）。代价是：若文件残留旧版长文，会盖住卡片里改过的短提示 —— 但 `/text` 写入时本就会同步文件，正常操作不会分叉。
2. **解析在画布层，冻结在审批层，Worker 只读冻结值**：这样"审批界面显示什么、实际执行什么"一致；用户执行期间改节点设置不会污染在途任务（另一笔审批自然用新参数）。
3. **`size` 只在能算出比例时才算**：没有 `ratio` 就返回 `None`，由 Worker 退回 `1024x576/1280x720` 的历史默认值，而不是猜一个"像竖屏"的尺寸。
4. **`ready_reason` 进 job / API**：前端可以把"为什么不能生成"直接显示在卡上，不用再让用户猜。
5. **未覆盖**：(a) 前端还没有"每卡片选择时长/画幅/模型"的 UI（本批只打通数据链，默认值来自项目设置与分镜）；(b) 分镜 `duration_s` 之和超出 provider 上限时由 `generation_params` clamp 到 1-30 秒，但不提示用户；(c) 合成（compose）阶段的总时长校验仍读分镜原值，尚未与 clamp 后的生成时长对齐。

验证（全量回归，含之前所有批次）：

- `python -m pytest tests/p0 -q` → **35 passed / 0 failed**（含审查后补的 2 条正文一致性回归）；
- `python -m compileall -q server core tests` → OK；
- `cd web-vue && npm run build` → 成功（1642 modules，无语法错误）。
- 手工链路验证：最小画布（prompt→video_gen 连线、单卡视频、9:16 项目 + 分镜 6 秒）分别走 `approvals → run → /api/media/jobs/{id}`，产物路径、`takes`、节点 `status=done` 均正常。

## 11. 修复记录（合成路径原子化：与 P0-8 同构）

生成路径在 P0-8 已经改成「单一提交点」，但这轮审查发现合成路径（`server/app.py:1342` 的 `compose_canvas`）还是旧写法：

```
consume(审批) → media_jobs.get(job_id) → _require_revision(冻结的 canvas_revision)
→ save(激活标记) → enqueue
```

四个症状：① 激活写入用审批时冻结的 `canvas_revision`，期间有人拖节点就 409，而审批已经作废；② `enqueue` 失败没有任何补偿（审批永久作废、画布留下幽灵 `active_generation_id`，异常直接冲出 ASGI）；③ 幂等查询 `media_jobs.get` 在 try 之外，任务存储不可用时审批照样先被消费；④ `job_key` 包含 `canvas_revision`，重试会变成新 `job_id`，同一份审批可能被重复计费。

RED（`tests/p0/test_p0_11_compose_atomicity.py`，5 条）：

- 控制用例：无并发/无故障时合成受理成功（修复前也绿）。
- 并发激活：激活落盘瞬间另一位写者提交 → 旧路径 `save` 无重试 → 409 且审批已被吃掉。
- 入队失败：`RuntimeError` 未捕获，直接冲进 ASGI（`TestClient` 原样抛出），审批作废、幽灵激活残留。
- 同一审批重试：无关编辑后 `job_id` 变成 `media_a8dafe…`（首次是 `media_87dc65…`）——重复计费的直接证据。
- 无关编辑后首次提交（审批之后、compose 之前）：旧路径其实已能通过（执行计划是 compose 时现算的），保留为语义防回归。

修复前原样结果：**2 failed / 2 passed**（控制 + 无关编辑两条绿；入队失败报未捕获 `RuntimeError`；重试用例 `assert 'media_a8dafe…' == 'media_87dc65…'` 失败）。并发激活用例与修复同批加入，仅作防回归锁。

改动面：

| 位置 | 改动 |
|---|---|
| `server/app.py:1370-1378` | `job_key` 改为 `canonical_hash({"approval_id": …, "plan": _approval_hash(execution_payload)})`：与生成路径同口径，排除 `canvas_revision` |
| `server/app.py:1388-1440` | 新增 `activate` / `revert_activation` 闭包 + 单一提交点：`try { get → activate(_mutate_canvas_with_retry) → enqueue } catch CanvasConflictError { release } catch Exception { revert + release + 503 }` |
| `server/app.py`（compose 激活段） | 删掉 `_require_revision(canvas, execution_payload["canvas_revision"])` 与裸 `save`：激活改走 CAS 重试，不再要求"画布还停在第几版" |

设计取舍：

1. **与生成路径共用同一套提交点，而不是各写一份**：两段代码结构一致后，后续加"取消/超时/审计"时不会再次分叉（Agent 工具那条双轨是下一个要收的，见附录 A.1 第 2 项）。
2. **`job_id` 排除 revision**：`revision` 是"画布被保存过几次"，不是执行计划的一部分；把它算进幂等键会让"一次可重试的失败"变成"第二笔费用"。
3. **失败仍然返回 503**：这是"可重试"信号，与 409（参数/审批不匹配）、400（计划不合法）区分开；前端可以据此提示"重试"而不是"重新审批"。
4. **激活仍在 enqueue 之前**：先入队后激活会打开"Worker 认领早于激活"的窗口，任务会被当成过期静默丢弃（P0-1 的 `stale_discarded`）。

验证：

- `python -m pytest tests/p0 -q` → **46 passed / 0 failed**（§11 交付时 40，§12 追加 6 条）；
- `python -m compileall -q server core tests` → OK；
- `python demo/composition_invariants.py` → PASS（mock MP4 → 时间线 → TTS/字幕 → 成片，真 ffmpeg 链路未被破坏）。
- 手工核对：合成受理后时间线节点 `data.active_generation_id` 指向 `job_id:node_id`，失败回滚后该字段还原。

---

# §12 P0-12：Agent 工具与画布生成双轨统一（2026-10）

## 问题
同一个"生成一张图"在仓库里有两条实现：画布链路（`/api/canvas/run` → `MediaJob` → `_execute_media_job` → `build_media_request` + `record_generated_media`）与 Agent 工具链路（`tools/media/tool.py` 的 `media_generate` / `media_run_canvas`）。后者自带一套参数口径：`size` 硬编码 `1024x576`/`1280x720`、模型兜底 `mock-{kind}-v1`、不传 `duration/ratio/resolution`、写回只置 `outputs` 不追加 `takes`、每个节点一次裸 `save`。结果是：**同一张卡，Agent 触发的生成与按钮触发的生成出片尺寸不同、Agent 产物没有历史版本、并发写会整份覆盖**。

## RED（修复前）
新增 `tests/p0/test_p0_12_agent_tool_parity.py`（6 条，真实 ToolContext + FakeProvider + 临时画布仓库）：

- 修复前：`5 failed / 1 passed`。关键失败原文：
  - `test_tool_uses_node_params_when_node_given` → `KeyError: 'size'`（工具请求里根本没有 size 键，硬编码在调用点）；
  - `test_run_canvas_uses_shared_request_builder` → `AttributeError: module 'core.canvas' has no attribute 'build_media_request'`；
  - `test_tool_writes_take_history_for_node` / `test_run_canvas_writes_takes` → `node["data"]` 无 `takes`；
  - 唯一通过的是"未 ready 节点跳过"，说明跳过逻辑本来就在。

## 改动

| 位置 | 改动 |
|---|---|
| `core/canvas.py`（`media_size` 之后） | 新增模块函数 `build_media_request(job, *, provider="", model="", generate_audio=False) -> dict[str, Any]`：以 `job["params"]` 为准生成 Provider 请求，缺省回退画布默认（图片 `1024x576`、视频 `1280x720` + 请求 duration），`references` 只透传有 `path` 的项。Worker 与 Agent 共用这一处 |
| `core/canvas.py`（`Canvas.save` 之前） | 新增 `record_generated_media(node_id, *, kind, path, mime, provider="", mock, model, duration_s, take_id, shot_no, mode)`：写 `outputs[port]` + 追加 take（截断 30）+ `current_take_id` + `status=done`，返回 take 字典；成功写回的成功路径统一走它 |
| `core/canvas.py` | 补 `import time`（此前 `save()` 的 `os.replace` 重试分支引用 `time.sleep` 却没有导入，Windows 文件锁触发时才炸的潜在 NameError） |
| `core/canvas_repository.py` | 新增公共帮助函数 `mutate_canvas_with_retry(repository, canvas_name, mutate, *, attempts=5) -> bool`：`load → mutate → save`，`CanvasConflictError` 换成 `await asyncio.sleep(0)` 重试；`mutate` 返回 False 表示前提失效、放弃写入 |
| `server/app.py`（`_mutate_canvas_with_retry`） | 改为委托上面的公共函数，Worker 6 处写回行为不变 |
| `server/app.py`（Worker `media_request` / `apply_success`） | 请求改由 `build_media_request(job, provider=..., model=...)` 构造（再补 `operation_callback`）；成功写回调 `board.record_generated_media(...)` |
| `tools/media/tool.py` | `media_generate`：指定 `node_id` 时用 `canvas.generation_params(node)` 覆盖画幅/时长/画质；状态写入改 CAS 重试；写回改 `record_generated_media`（takes）；`media_run_canvas`：按 `job["ready_reason"]` 跳过说明、请求走 `build_media_request`、每节点写回 CAS 重试 + takes；`_pick_model` 优先当前激活 Provider 的模型（读 `media.registry.active`） |
| `tests/p0/conftest.py` | `P0Env` 增加 `repository`（`registry.canvas_repository`），工具用例直接复用同一临时画布 |

## 设计取舍

1. **参数构造器放在 `core/canvas.py` 而不是 `server/`**：它只依赖领域对象（job dict），Worker 与 agent 工具都能 import；如果再往上放（`server/`），`tools/` 就会反向依赖服务层。
2. **Agent 工具也写 `takes`，但不做"审批哈希冻结"**：画布链路的冻结是为了"用户确认的参数 = 实际扣费的参数"；Agent 侧的门控是宿主给的一次性 `media_approved`，没有审批单据可挂。强行引入台账会让工具层依赖 `MediaJobStore`，属于 P0-4 事件流批次的活。这条边界已写进 `tests/p0/README.md`。
3. **`record_generated_media` 收在 Canvas 域对象里**：P0-1 修复时"成功写回"的字段组合（outputs + take 30 截断 + current_take_id + done）是隐式契约，散在两处早晚漂移；现在有一条 Python 层面的单一事实源，后续加 `prompt/references/seed` 快照只需改一处。
4. **`mutate_canvas_with_retry` 上移到仓储层**：Worker 的 6 处写回和 Agent 的 2 处写回都是"load-mutate-save 重试"，把它留在 `server/app.py` 会让 `tools/` 反向依赖 app 模块。

## 验证

- `python -m pytest tests/p0/test_p0_12_agent_tool_parity.py -q` → **6 passed**；
- `python -m pytest tests/p0 -q` → **46 passed / 0 failed**（40 + 新增 6）；
- `python -m compileall -q server core tools tests` → OK；
- 手工：`demo/regression_media_approval.py` 打印 PASS 后进程不退出是既有现象（`registry.ainit()` 起的长驻线程），与本批无关。

## 已知边界
- Agent 工具没有"审批参数冻结 + invocation 幂等"，同一轮 Agent 重试仍可能重复调用 Provider（P0-4 收口）。
- `media_run_canvas` 仍是"同步逐个生成"，没有任务队列/进度事件；长视频会阻塞工具调用（P0-4）。
- 前端仍没有 per-node 的时长/画幅/模型 UI，参数只能来自项目设置、分镜或 API 调用方。

---

# §13 P0-13：任务终态语义（succeeded/failed/discarded/unknown）

## 问题
`MediaJobWorker._run`（`core/media_job_store.py:474-486`）原来只按"execute 有没有抛异常"决定终态：

```python
result = await self.execute(current)
await self.store.complete(current.job_id, self.worker_id, result, failed=False)
```

而 `_execute_media_job` 的返回值里本来就有 `ok` 与逐节点 `stale_discarded`。于是两种"没成功"都记成 `succeeded`：

1. **stale_discarded**：Provider 已出片、费用已发生，但写回前节点被新一代任务取代 → 产物整份丢弃；任务却报成功。前端/运维看到成功会以为画布拿到了产物。
2. **部分失败**：批次里某个节点 Provider 报错、其余成功 → `ok=False`，但仍记 `succeeded`；前端不会提示"有节点失败"。

## RED（修复前）
新增 `tests/p0/test_p0_13_stale_status.py`（3 条）：

- `test_stale_task_is_marked_discarded` → 失败：`assert 'succeeded' == 'discarded'`；
- `test_partial_provider_failure_is_not_success` → 失败：`assert 'succeeded' == 'failed'`；
- `test_success_still_reports_succeeded` → 通过（对照组）。
- 汇总：**2 failed / 1 passed**。

## 改动

| 位置 | 改动 |
|---|---|
| `core/media_job_store.py` 模块头 | 写清四态语义（`succeeded/failed/discarded/unknown`），说明 `discarded` 是"钱已花但产物被取代、不可重试" |
| `core/media_job_store.py` `MediaJobStore.complete` | 签名加 `discarded: bool = False`（Protocol + InMemory/Redis/Postgres 三实现同步）：`discarded → "discarded"`，否则 `failed → "failed"`，否则 `"succeeded"` |
| `core/media_job_store.py` `_classify_result(result)` | 新模块函数：`ok=True`→成功；所有非成功条目都带 `stale_discarded`→`discarded`；其余→`failed` |
| `core/media_job_store.py` `_run` | 用 `_classify_result` 决定 `complete(failed=, discarded=)`；`failed` 且 result 没有顶层 `error` 时，把逐节点失败原因汇总进 `result["error"]`（`/api/media/jobs` 才能显示原因） |
| `web-vue/src/store.js` `waitForMediaJob` | 新增 `discarded` 分支：不再当失败抛错，`runResult.status="discarded"` + 提示"结果已过期，未写入画布（节点已有更新一代任务）" |
| `tests/p0/conftest.py` | `TERMINAL_STATUSES` 加 `discarded`，`wait_job` 能等到该终态 |

## 设计取舍

1. **不加 "cancelled"**：取消是 P0-4 的活（需要 job 状态机 + 前端取消按钮 + 远端取消能力）；现在只补齐"语义诚实"这一层。
2. **`discarded` 不算 `failed`**：重试同一个审批不会改变"节点已被新一代接管"的事实，反而会再扣一次费；前端应展示为"未采用"，并引导用户看历史版本。
3. **部分失败整批记 `failed`（而不是 succeeded + 前端自己数）**：任务状态是给用户/运维的契约，不能要求每个消费方都去解析 `results`。成功节点仍已写回画布，`results` 里保留逐节点明细。
4. **默认仍是保守成功**：`_classify_result` 对"没有 results 的旧格式结果"不擅自判失败（`ok` 缺失且无条目 → 按成功），避免历史任务回放被误判。

## 验证

- `python -m pytest tests/p0/test_p0_13_stale_status.py -q` → **3 passed**；
- `python -m pytest tests/p0 -q` → **49 passed / 0 failed**（46 + 新增 3）；
- `python -m compileall -q server core tools tests` → OK；
- `cd web-vue && npm run build` → 成功（1642 modules，`store.js` 的 `discarded` 分支已进产物）。

## 已知边界
- 前端目前只有 `runResult.status` 一处消费 `discarded`；任务列表/节点徽标的状态映射随 P0-4 事件流一起做。
- Redis/Postgres 实现没有状态枚举约束（`status` 是文本列），故新增状态不需要迁移；但外部报表若写死三态需要同步。
- Worker 的"部分失败记 failed"只覆盖 media 任务；合成任务复用同一 Worker，行为一致。

## 14. Sprint 0.5 第 4 项：异常边界收窄 + CAS 退避（2026-10）

### 症状

生成/合成的提交点用 `except Exception` 一网打尽，把所有异常都包装成
`503 本次生成没有提交成功（任务存储或画布暂时不可用），审批已退回，请重试`。后果有两层：

1. **真 bug 被伪造成"临时故障"**：`TypeError`、`AttributeError`、`KeyError` 这类编程错误也返回 503，
   前端提示"重试就好"，用户反复点击、费用可能反复发生，而服务端日志里没有 traceback。
2. **重试是空转、还会互相撞车**：`mutate_canvas_with_retry` 的退避是 `asyncio.sleep(0)`，
   多个写者在同一毫秒抢同一把画布写锁时只会连续撞车再失败。

### RED（修复前证据）

`python -m pytest tests/p0/test_p0_14_error_boundary.py -q` → `1 failed, 2 passed`：

```
AssertionError: {"detail":"本次生成没有提交成功（任务存储或画布暂时不可用），审批已退回，请重试"}
E   assert 503 == 500
```

### 改动面

| 位置 | 改动 |
|---|---|
| `server/app.py` | `import logging` + `logger = logging.getLogger("toonflow.api")`；新增 `_TRANSIENT_ERROR_MODULES`、`_is_transient_storage_error(exc)`、`_revert_activation_quietly(canvas_name, revert)` |
| `server/app.py`（run 提交点） | `except HTTPException: raise`；`except Exception` → 回滚激活 + 退回审批 + 临时故障 503（`logger.warning`）/ 编程错误 500（`logger.exception`，响应提示"内部错误，请联系维护者查看日志"） |
| `server/app.py`（compose 提交点） | 同构修改，文案改为"本次合成…" |
| `core/canvas_repository.py` | `mutate_canvas_with_retry` 冲突退避改为 `asyncio.sleep(min(0.3, 0.02 * attempt) * (0.5 + random.random()))`（加抖动避免同步重撞）；`import random` |

### 设计取舍

1. **白名单判"可重试"，而不是黑名单**：只有明确可重试的（`TimeoutError`/`ConnectionError`/`OSError`/`RuntimeError`/redis/asyncpg/psycopg 等驱动异常）返回 503；其余一律 500 + traceback。宁可暴露一次内部错误，也不误导用户反复重试。
2. **`RuntimeError` 归入可重试**：内存/Redis/Postgres 存储封装在租约、队列、连接池不可用时统一用 `RuntimeError` 表达"现在做不了"；编程错误更常见的是 `TypeError`/`AttributeError`/`KeyError`，分界清晰。
3. **500 也要退回审批、回滚激活**：与 503 相同，否则"服务端 bug"会变成"审批被吃、任务没下、节点幽灵激活"的三重残留。
4. **退避加抖动**：冲突通常意味着有人正在写；固定 20ms/50ms 退避会让两个写者形成节拍一致的"撞车舞"，抖动能把重试错开。

### 验证

- `python -m pytest tests/p0/test_p0_14_error_boundary.py -q` → **3 passed**；
- `python -m pytest tests/p0 -q` → **52 passed / 0 failed**（49 + 新增 3）；
- `python -m compileall -q server core tools tests` → OK；
- `cd web-vue && npm run build` → 成功（1642 modules）。

### 已知边界

- 判定基于异常类型/模块，不解析错误文案；存储层若抛出未列入白名单的第三方异常会被判成 500——这是有意的（无法确认可重试就不承诺可重试）。
- `HTTPException` 原样透传，不会被二次包装。
- 前端目前仍统一按"失败可重试"提示 503；P0-4 事件流做状态机时再区分"可重试/需人工处理"。

## 15. Sprint 1 第 1 项：删除节点（P0-6，2026-10）

### 症状

画布只能"增"、不能"删"：后端没有 `DELETE /api/canvas/nodes/{id}`，前端右键菜单的"删除节点"是灰的
（hint 写着"服务端暂无删除接口"）。废稿、试错的卡片只能一直留在画布上，节点一多就拖慢渲染、
也让体检/导出产出噪声。

### 删除看起来简单，真正的坑在三条

1. **级联删边**：只删节点不删边，会留下"悬空边"——体检报错、导出引用了不存在的上游、
   前端画布画出一条指向空气的线。
2. **严格 revision**：删除是写操作。基于旧视图删节点，可能把别人刚连上的线一起删掉；
   和正文/切版一样必须带 revision，缺 → 400、过期 → 409。
3. **生成中删除**：Worker 完成时节点已经没了。旧代码用 `canvas.require(node_id)`，
   会抛 `CanvasError` 把整单判失败，甚至尝试把节点"复活"；正确语义是**结果无归属 → 过期丢弃**。

### 改动面

| 位置 | 改动 |
|---|---|
| `server/app.py` | 新增 `DELETE /api/canvas/nodes/{node_id}?canvas=&revision=`：`_require_write_revision` 严格校验 → 节点不存在 404 → `canvas.remove_nodes([node_id])` 级联删边 → save；返回 `removed / removed_edges / revision` |
| `server/app.py`（Worker） | 新增 `owned_node(board)`（节点不存在或 `active_generation_id` 已换 → `None`）；6 处状态回写改用它；执行前 `canvas.nodes.get()` 为空 → 记 `stale_discarded` 并跳过（不建 invocation、不调 Provider） |
| `server/app.py`（合成 Worker） | `_save_composition_state` 遇到节点被删返回 `False`（按过期处理，不复活） |
| `web-vue/src/store.js` | 新增 `deleteNode(nodeId)`：`DELETE ...?canvas=&revision=`（query 传参）→ 更新 revision → `loadCanvas()`；导出到 actions |
| `web-vue/src/canvas2/CanvasBoard.vue` | 右键菜单"删除节点"从 disabled 改为可用；新增 `deleteNodes(ids)`：确认框（正在生成的节点会额外提示"结果会被丢弃"）、逐个删除、失败即停；`Delete`/`Backspace` 快捷键删除选区（输入框内不触发） |

### 设计取舍

1. **硬删除，不做软删/回收站**：软删要引入 `deleted_at` 过滤，画布图算法/体检/导出全都要跟着改；当前阶段确认框 + 立即删除足够，回收站属于 P2 产品增强。
2. **删除立即释放，但正在生成的结果仍会落盘到资产目录**：产物文件不删（可能被历史/合成引用），只从画布移除节点；任务记 `discarded`，钱已花但不会污染画布。
3. **批量删除按顺序逐个调用**：每个节点一次 CAS 写，简单、冲突时能准确停在哪一个；不做"一个请求删 N 个节点"的新接口（会把 revision 语义和错误定位复杂化）。
4. **删除后重新 `loadCanvas()` 而不是本地删**：上游 readies、下游 `ready_reason`、拓扑顺序都会变，必须让服务端重算。

### 验证

- `python -m pytest tests/p0/test_p0_15_node_delete.py -q` → **6 passed**；
- `python -m pytest tests/p0 -q` → **58 passed / 0 failed**（52 + 新增 6）；
- `python -m compileall -q server core tools tests` → OK；
- `cd web-vue && npm run build` → 成功（1638 kB 主包，右键菜单/快捷键已进产物）。

### 已知边界

- 删除**不可撤销**（没有 undo/redo，那是画布基础设施 P2 项）；确认框里的文案已明确"不可撤销"。
- 生成中删除只保证"结果被丢弃、不复活节点"，不保证立刻停止远端调用（Provider 一旦发出请求就会跑完、照常计费）；真正的"取消"依赖 P0-4 的任务取消/事件流。
- 旧 Vue Flow 画布（`?legacy=1`）没有接入删除入口；它已经在逐步下线。


---

## 16. P0-3 拖拽回弹（前端批次）

### 症状

拖动卡片松手后，卡片先跳到新位置、随即弹回原位；只有刷新页面（或触发一次全量 `loadCanvas`）才看到"其实已经存上了"。

### 根因

`web-vue/src/store.js:moveNode` 只把服务端返回的 `revision` 写回 `state.canvasServerRevision`，**没有把新坐标写回 `state.canvas.nodes[].position`**。卡片渲染读的是后者，所以松手后 computed 按旧坐标重算，视觉上就弹回去了。同时 `canvas2/NodeCard.vue` 在 `pointerup` 立刻把本地 `offset` 归零，于是"跳一下再回原位"。

### 改动面

| 位置 | 改动 |
|---|---|
| `web-vue/src/canvas2/positions.js`（新增） | 坐标纯函数：`normalizePosition`（取整/拒绝非法值）、`nodePosition`（兼容画布节点 `position` 与画板卡 `x/y`）、`dragMoves`（整组按同一位移平移） |
| `web-vue/src/store.js` | 新增 `applyNodePosition` / `moveNodes`；`moveNode` 改为"先乐观写本地坐标 → 提交 → 失败回滚；409 冲突重拉并以服务端为准"；`moveNode`、`moveNodes` 加入 `actions` |
| `web-vue/src/canvas2/NodeCard.vue` | 拖动中 `emit("drag")` 通知画板；新增 `preview` 偏移用于多选跟手；`onBeforeUnmount` 摘掉 `window` 监听 |
| `web-vue/src/canvas2/CanvasBoard.vue` | `onMove` 改用 `dragMoves` + `actions.moveNodes`（整组先落位、再逐张串行提交）；新增 `dragPreview`/`previewFor`，同组卡片实时跟手；409 提示"位置已刷新"、其它失败提示保存失败 |
| `web-vue/package.json` | 新增 `test:unit` 脚本（`node --test "tests/**/*.test.mjs"`） |
| `web-vue/tests/positions.test.mjs`（新增） | 5 条单测：取整/非法值、两种节点形状、位移真的落点（回归）、多选整组平移且不误伤、非法拖动量降级 |

### 设计取舍

1. **乐观更新而不是"等保存成功再动"**：坐标是纯前端可推导的状态，等服务端往返会明显拖手；失败再回滚，最坏情况与旧行为一致。
2. **串行提交而不是 `Promise.all`**：每次 position 保存都会把服务端 revision +1，并发提交必然互相 409；多选 5 张卡的串行往返在本地是毫秒级，不值得为并行引入冲突重试。
3. **多选松手只提交一次**：拖动过程中只做视觉预览（`preview` 偏移），不逐帧写库，避免"拖一下产生几十个 revision"把并发窗口撑大。
4. **冲突时以服务端为准**：409 说明别人先改了画布，本地坐标不再是权威值；重拉后提示"位置已刷新"，让用户确认后再拖，而不是静默覆盖或静默丢弃。
5. **坐标计算抽成纯函数**：不引 Vue，`node --test` 直接覆盖；后续 undo/redo、对齐吸附都会复用同一套落点逻辑。

### 验证

- `cd web-vue && npm run test:unit` → **5 passed**；
- `npm run build` → 成功（`built in 1.56s`）；
- `python -m pytest tests/p0 -q` → **58 passed**（后端无改动，确认未被前端改动带崩）；
- 手工现象（本地 `npm run dev` + 后端起服务）：单卡拖到哪停哪、不再弹回；多选拖动时整组一起跟手、松手一次落位；拖动中刷新页面，位置以最后一次提交为准。

### 已知边界

- 没有 undo/redo：拖错只能再拖回去（undo 属 P2 画布基础设施）。
- 拖动过程中不持续保存：拖到一半强制刷新，会回到拖动前的位置。
- 旧 Vue Flow 画布（`?legacy=1`）仍用 `@vue-flow` 自己的拖拽实现，不在本批范围。

## 17. P0-5：视频卡破图、假时长与懒加载（已完成）

### 症状

- 视频节点在画布上是一张破图：`<img src="....mp4">` 当图片解码必然失败，只留一个空框。
- 播放键点不动（没有 handler），时长永远显示 `00:04`。
- 画布上几十个视频节点时没有懒加载：既没挂 `<video>`，又把整段 MP4 当图片请求一遍。

### 根因

- `web-vue/src/canvas2/NodeCard.vue` 只有图片分支：`<img v-if="node.img">`；`node.img` 对视频卡也取 `assetUrl(media.path)`（`.mp4`）。
- 时长 `node.duration` 是 `boardNodes` 里写死的 `"00:04"`，与 `take.duration_s` 无关。
- 画板已有视口剔除（`visibleNodes`，pad 360）与语义缩放（`lod-far/mid/near`），但没有"视频该不该加载"的策略层。

### 改动

| 文件 | 改动 |
|---|---|
| `web-vue/src/canvas2/video.js`（新增） | 纯函数：`formatDuration(seconds)`（非法值不显示）、`videoFrameUrl(src)`（加 `#t=0.1` 让浏览器只取首帧附近数据） |
| `web-vue/src/canvas2/VideoPlayer.vue`（新增） | 三层懒加载 + 悬停静音预览 + 播放键（hover 淡入）+ 时长徽标 + 首帧失败重试 + `prefers-reduced-motion` 不自动播 |
| `web-vue/src/canvas2/VideoLightbox.vue`（新增） | Teleport 到 `body` 的大图：Esc/点背景关闭、`body` 锁滚动、原生 controls、下载按钮 |
| `web-vue/src/canvas2/NodeCard.vue` | 视频卡改挂 `<VideoPlayer>`，删除伪 `play-btn`/`00:04`；新增 `open-video` 事件；图片卡不动 |
| `web-vue/src/canvas2/CanvasBoard.vue` | `boardNodes` 补 `src`/`durationSeconds`（取当前 take 的 `duration_s`，缺就不显示）；`videoOpen` 状态与 `onOpenVideo`；灯箱开着时键盘让给灯箱 |
| `web-vue/src/styles/canvas2.css` | 播放器骨架/进度/徽标/失败态/灯箱样式；`@media (hover:none)` 播放键常显；`prefers-reduced-motion` 关动画 |
| `web-vue/tests/video.test.mjs`（新增） | 4 条纯函数单测 |

### 设计取舍

1. **懒加载分三层**：不在视口不挂、缩得太远（<45%）不挂、挂了也只取首帧。不是"全部挂上再优化"，而是"能不挂就不挂"。
2. **悬停预览而不是点击播放**：画布上点一下要选中/拖线，"点开就播"会跟选中打架；悬停静音循环预览像素材卡，点播放键才进大图。
3. **时长以 take 为准**：`duration_s` 是生成时冻结的真实值；缺了就不显示，宁可不写也不编。
4. **骨架不做呼吸动画**：几十张卡同时出现时满屏闪会晕，静态渐变 + 迟到的首帧足够。
5. **灯箱是只读查看器**：不在灯箱里做版本切换/重生，避免两处状态源；那些在历史抽屉/工作台里。

### 验证

- `cd web-vue && npm run test:unit` → **9 passed**（positions 5 + video 4）；
- `npm run build` → 成功（1646 modules，`built in 1.24s`）；
- `python -m pytest tests/p0 -q` → **58 passed**（后端未改，确认未带崩）；
- 真浏览器验收（Chrome 155 headless + CDP，main 画布用 MockProvider 生成真 MP4 后）：视频卡 `videoWidth=1280`、`videoHeight=720`、时长徽标 `0:05`；悬停 2 秒后 `paused=false`、`currentTime=2.47`；点播放键打开灯箱（标题 / `mock` / `0:05` / 1280×720 播放 / `body` 滚动锁）；Esc 关闭后灯箱消失、选中保留、节点数不变；缩到 15% 时 `.video-el` 数量 1 → 0（只留骨架），60 个视频节点的画布不再解码任何视频。
- 截图证据：`docs/canvas-v2/p05-video-hover.png`（悬停预览）、`p05-video-selected.png`（选中 + 卡下提示词框）、`p05-video-lightbox.png`（大图）、`p05-video-far-skeleton.png`（15% 缩放只剩骨架）。
- 图片卡渲染未变；旧 Vue Flow 版未动（`?legacy=1` 仍走 `CanvasPanel`）。

### 已知边界

- 画布内不做声音预览：悬停一律静音（避免几十张卡同时出声），声音在大图里听。
- `<video>` 首帧依赖服务端 `/assets` 的 Range 支持（Starlette StaticFiles 已支持）；不支持 Range 的部署会退化成黑帧 + 播放键可用。
- far 缩放（<45%）只显示骨架、不显示静态缩略图：这是拿"带宽/解码"换来的，几十个视频节点时不值得为远处的卡解码。
- 灯箱只读；版本切换仍在历史抽屉/单卡工作台。

---

## 18. M1/M2：生成状态可见 + 失败重试（2026-10-19）

> 面试演示前的最小修复（见 `docs/面试演示方案.md` 第三节）。不是技术债清单里的条目，但用户可见性优先。

### 症状

- 点「生成这一张」后，卡片在 **800ms 之后**才有任何变化（第一次轮询回来之前，界面完全没反应，像没点中）。
- 生成失败时卡片只停在 `error` 状态，界面上**没有任何「重试」入口**，用户只能再点一次菜单。
- 生成期间可以重复点「生成」，会生成多份任务（服务端幂等能兜住同一次审批，但 UI 不设防）。
- 顺带发现：生成期间视口会被拽回全图（`watch(nodes.length)` 每次重拉都触发 `fit()`），演示时镜头直接离开被操作的卡。

### 改动

| 位置 | 改动 |
|---|---|
| `web-vue/src/canvas2/CanvasBoard.vue` | 新增 `pendingGen`/`doneFlash` + `markPending`/`flashDone`；`boardNodes` 补 `status/busy/busyLabel/failed/justDone`；`generateOne`/`batchGenerate` 提交即置 busy、结束后 `flashDone`；`onRetry` 复用 `generateOne`；`generateOne` 对 busy 卡直接返回 |
| `web-vue/src/canvas2/NodeCard.vue` | 新增 `retry` 事件与 `gen-busy`/`gen-failed`/`gen-done` 类；busy 遮罩（转圈 + 文案）、failed 遮罩（! + 生成失败 + 重试按钮） |
| `web-vue/src/canvas2/NodeToolbar.vue` | 生成按钮在 `node.busy` 时 `disabled` + title「正在生成，等它完成」 |
| `web-vue/src/styles/canvas2.css` | 遮罩/转圈/失败图标/重试/hover 浮出、`cv2-gen-done` 高亮、`prefers-reduced-motion` 关闭动画 |
| `web-vue/src/canvas2/VideoPlayer.vue` | 修「先悬停、metadata 后到」不自动预览：`watch(ready)` 里若仍悬停则补开 |
| `web-vue/src/canvas2/CanvasBoard.vue` | 修视口跳动：`watch` 从「nodes.length 变化」改为 `lastFocusKey`（`canvasName:ready/empty`），只在换画布/首次拿到节点时 `scheduleFocus()` |

### 设计取舍

1. **服务端状态为准，本地 pending 只补“点下瞬间”**：`pendingGen` 只用于提交后到首次轮询之间；轮询一回来就以 `node.status` 为准，避免本地猜测与服务端状态打架。
2. **失败不靠本地猜测**：只有 Worker 写入 `status=error` 才显示「重试」；轮询超时的 toast 另走 `waitForMediaJob` 的 deadline 分支。
3. **重试复用 `generateOne`**：重试不绕过审批、参数解析与 revision 契约，与首次生成同一条链路（否则会造出第二条写回路径）。
4. **`gen-done` 只亮 1.5s**：提示“这张变了”但不长期占色，避免几十张卡同时高亮。
5. **视口只在“换画布/首屏就绪”聚焦**：生成、拖拽、轮询刷新都不动相机——这是演示体验的硬要求。

### 验证

- `cd web-vue && npm run test:unit` → 9 passed；`npm run build` 成功。
- 真浏览器（Chrome headless + CDP）在 `main` 画布实测：点生成瞬间出现「已提交…」遮罩、按钮禁用（title 正确）；1–2s 后 `gen-done`、take +1；注入 `status=error` 后出现「生成失败 / 重试」，点重试恢复 `done`；视口坐标在生成前后稳定（(908, 500)）。
- 视频卡真实片段：`demo-clip.mp4`（12.04s、496×864）播放、悬停预览、点播放键打开大图。
- 历史版本：生成后抽屉显示 2 版，可「设为当前版」切回真实片段（`take_demo_01`）。
- 后端未改：`python -m pytest tests/p0 -q` → 58 passed。

### 已知边界

- 遮罩文案在 <50% 缩放下只显示转圈（尺寸不够放字），这是 LOD 的取舍。
- 重试按钮是卡片内最小实现，没有指数退避/自动重试；批量重试未做。
- 视口「首次就绪聚焦」在空画布首次添加节点时仍会触发一次（这是想要的行为）。
- 历史抽屉的「设为当前版」会改变 `outputs.video`，但 PromptBox 的模型 chip 仍显示当前生成模型，不随 take 切换（次要）。

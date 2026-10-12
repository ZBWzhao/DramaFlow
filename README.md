# toonflow-py-lab

以开源项目 **Toonflow**（TypeScript + Bun）的 Agent 架构为参考，**用 Python 独立实现**的两个核心模块，
外加一个能跑的前后端（FastAPI + NDJSON + 网页）。

> 说明：Toonflow 的 TS 代码无法直接复用（其底层 SDK 也只有 TS 版），
> 本项目带走的是**架构与设计取舍**，实现全部重写。

---

## 快速开始

```bash
pip install -r requirements.txt      # openai / pydantic / fastapi / uvicorn
python run.py                        # 起服务 → http://127.0.0.1:8000
```

API Key：环境变量 `DEEPSEEK_API_KEY` 优先，否则读 `~/.pi/agent/auth.json`（和 `pi-lab/mini_pi.py` 一致）。
换模型：`LLM_BASE_URL` / `LLM_MODEL` 环境变量。

### 启用 Redis + PostgreSQL 完整后端（推荐演示方式）

```powershell
docker compose -f compose.postgres.yml up -d
python run_full.py
```

`run_full.py` 默认把 Redis 用于事件流、运行状态、Follow-up 和结构化问答，把 PostgreSQL
用于工具调用账本、审批、媒体任务和画布；已有环境变量可覆盖默认连接地址。服务启动时会自动
执行 `migrations/*.sql`。独立媒体 Worker 模式还需要让 API 与 Worker
使用相同的 `DATABASE_URL` 和 `INVOCATION_STORE_BACKEND`，避免远端 operation_id 只存在某个进程内。

查看基础服务状态或停止服务：

```powershell
docker compose -f compose.postgres.yml ps
docker compose -f compose.postgres.yml down
```

`down` 不会删除命名卷；除非明确需要清空本地数据库，不要添加 `-v`。

## 命令行验收脚本

| 脚本 | 验什么 |
|---|---|
| `python demo/smoke.py` | 阶段 0：流式 + tool calling + usage |
| `python demo/run_tools.py` | **模块一**：沙箱自测 + 模型真的调用自研工具 |
| `python demo/run_delegation.py` | **模块二**：2 工具 + 1 次子 Agent 委派 + report 上报 + 用量 |
| `python demo/usage_experiment.py` | 增量 vs 全量 token 对照实验 |
| `python demo/cancel_cascade.py` | 父中止 → 子中止 |
| `python demo/compact_demo.py` | 上下文压缩（小阈值强制触发）+ 会话落盘 JSONL |
| `python demo/branch_demo.py` | 会话树分支（id/parent，文件里两条分支） |
| `python demo/run_team.py` | 声明式多 Agent 团队（director / writer / reviewer） |
| `python demo/skills_demo.py` | 技能渐进式披露：清单常驻 + 模型先读正文再动手 |
| `python demo/acceptance.py` | **一键验收**：10 个 CLI 场景 + 8 个 HTTP 接口，产出 `acceptance_report.md` |
| `python demo/events_overflow.py` | 事件总线不变量：队列满时**只丢流式增量**，生命周期事件必须送达 |
| `python demo/runtime_store_invariants.py` | 运行状态接口、递增事件 ID、断点补发与持久化后广播顺序 |
| `python demo/redis_stream_invariants.py` | 真实 Redis Streams：原子递增 ID、游标补发与测试数据清理 |
| `python demo/postgres_contract_invariants.py` | PostgreSQL 持久层契约、幂等约束、画布版本与 Outbox 表结构 |
| `python demo/postgres_live_invariants.py` | 真实 PostgreSQL：并发幂等、审批行锁、媒体任务租约与画布乐观锁 |
| `python demo/canvas_repository_invariants.py` | 画布 revision 乐观锁：旧版本写入被拒绝且不覆盖新内容 |
| `python demo/session_invariants.py` | 会话不变量：取消不清、并发互串、开关粘住、压缩/分支恢复不还原（纯本地） |
| `python demo/canvas_cycle.py` | 画布成环防护：写入前拒绝 + 精确报出环路（纯本地，不花 API 费用） |
| `python demo/canvas_doctor.py` | 画布医生：体检、把"环"定位到具体哪条边、`--drop` / `--break-cycles` 破环 |
| `python demo/drama_demo.py` | **产品化**：一句创意 → Agent 自主建出短剧生产图（画布） |
| `python demo/drama_demo.py --repair` | 提示词审校修正：校验发现编号问题 → 模型自纠 → 导出（实测未展开编号清零） |
| `python demo/run_team.py --team dramaTeam "做一个 30 秒短剧…"` | **短剧生产团队**：导演→编剧→美术→分镜→提示词→审校（5 成员流水线） |
| `python demo/canvas_inspect.py --all` | 画布体检报告（不花钱）：拓扑序、时长、资产 tags、编号可展开性 |
| `python demo/generate_demo.py` | **P2**：给画布补生成节点 → 按拓扑序执行 → 产物落 `assets/generated/`（mock provider，真 PNG） |

### 媒体 Provider 与人工审批

- 默认使用 `mock`，不会调用外部服务，也不会产生费用。
- 设置服务端环境变量 `TF_ROUTER_API_KEY` 后，前端会出现 TF-router 的 Seedance、Wan、Seedream 等模型；可参考 `.env.example`。
- Agent 只负责完成剧本、分镜、资产、正式提示词和生成节点，不能直接调用真实媒体生成。
- 每次执行生成都必须经过前端审批卡或确认框；服务端把画布、Provider、模型、生成节点和提示词哈希绑定到有时效的 `approval_id`，参数变化后旧审批立即失效。
- 文件、画布和媒体等副作用工具使用 `ToolInvocationStore` 记录 `prepared → executing → succeeded/failed/unknown`；相同调用可重放已保存结果，`executing/unknown` 不会盲目自动重试。
- `INVOCATION_STORE_BACKEND=redis` 时，调用与审批状态可跨实例共享；默认仍可使用零依赖的内存实现。外部 Provider 暂未统一支持幂等键，因此这里只保证应用侧防重，不宣称端到端 exactly-once。
- `INVOCATION_STORE_BACKEND=postgres` 与 `MEDIA_JOB_STORE_BACKEND=postgres` 时，不可丢的工具调用、审批和媒体任务写入 PostgreSQL。启动时按 `migrations/*.sql` 幂等建表；调用和任务使用唯一幂等键，审批消费使用行锁，媒体 Worker 使用 `FOR UPDATE SKIP LOCKED` 抢占任务。
- `CANVAS_STORE_BACKEND=postgres` 时，API、Agent 工具和媒体 Worker 统一通过 `CanvasRepository` 访问画布；`UPDATE ... WHERE revision=?` 防止旧快照覆盖新版本，首次启用会导入工作区已有画布。
- 推荐生产拆分：PostgreSQL 保存业务事实，Redis Streams/Redis 保存事件与运行态，媒体文件保存到对象存储。当前 PostgreSQL 已接管副作用账本、媒体队列和画布；JSONL 会话适配器与 Outbox Publisher 尚未切换，不能宣称已完成全量迁移。
- 对话框提供“逐步询问后审批 / 自动准备后审批 / 仅策划不生成”三种协作方式。
| `python demo/compact_bench.py` | 上下文压缩基准：实测 24 轮会话压缩后降 **90%**（13388 → 1364 字符） |

> 验收脚本支持 `--cli-range 1,3` 分批、`--api-only` 只验后端。最近一次验收：**16/16 通过**。

## 产品化路线

把 Agent 运行时改造成**短剧生成工具**（剧本 → 分镜 → 资产 → 片段提示词 → 画布连线 → 导出），
可执行清单见 `ROADMAP.md`；方案与 Toonflow 实证对照见笔记 `T9-01 短剧生成工具改造方案`。

## 前端

**Vue 3 + Vite + Element Plus + Vue Flow**（`web-vue/`），Toonflow 的同款选型。旧版零构建前端保留在 `/legacy` 作为退路。

```bash
cd web-vue && npm install        # 首次
cd web-vue && npm run dev        # 开发：http://127.0.0.1:5173（/api 代理到 8000）
cd web-vue && npm run build      # 构建：web-vue/dist → 由后端挂在根路径
node web-vue/scripts/stream-selftest.mjs   # 流式渲染自测（不花 API 费用）
```

| 位置 | 职责 |
|---|---|
| `web-vue/src/store.js` | 全局状态 + 动作；画布数据保持服务端原样，Vue Flow 的 nodes/edges 派生 |
| `web-vue/src/components/CanvasPanel.vue` | Vue Flow + 工具条（一键生成/导出/整理/适应/刷新）+ 结果条 + 体检条 + 节点详情 |
| `web-vue/src/components/ToonNode.vue` | 自定义节点卡（220 宽 / 标题在卡外 / 端口 hover 才显 / 产物缩略图） |
| `web-vue/src/components/ChatPanel.vue` | 按事件流顺序渲染对话（消息 / 工具卡 / 子 Agent 块 / 团队步骤） |
| `web-vue/src/styles/theme.css` | 设计 token + Element Plus 变量映射（组件库与自写样式共用色板） |
| `web/src/*` | 旧版零构建前端（退路，挂 `/legacy`） |

改动前端后请**构建一次**（`npm run build`），后端自动服务新产物；只跑 `npm run dev` 时后端仍是旧产物。

### Redis Streams 与断线续传

默认 `EVENT_STORE_BACKEND=memory`，保持个人开发零外部服务。部署 Redis 后设置
`EVENT_STORE_BACKEND=redis` 与 `REDIS_URL`，每个会话会写入独立 Redis Stream，事件使用 Redis
生成的递增 Stream ID。浏览器保存最近的 `event_id`；连接异常时调用
`GET /api/events/stream?session_id=...&run_id=...&last_event_id=...` 补发遗漏事件并继续实时订阅。
网络断开只会移除浏览器订阅，只有显式调用 `/api/agent/cancel` 才会取消 Agent 任务。

运行状态也实现了同一套可切换接口。`RUN_STATE_STORE_BACKEND` 未设置时跟随
`EVENT_STORE_BACKEND`：Redis 模式用 Hash 保存运行状态和取消位、用原子占用键阻止两个实例同时执行
同一会话，并用 List 保存运行中 Follow-up。取消请求即使落到另一实例，执行实例也会轮询共享取消位；
Follow-up 只会在模型请求与工具调用之间的安全点进入上下文。

结构化问答同样通过 `QuestionStateStore` 抽象：Redis Hash 保存问题、选项、所属 `run_id`
和终态，Session Pending Set 支持跨实例列出待回答问题。答案使用原子状态迁移，重复提交、非法选项、
已结束或心跳过期运行的答案都会被拒绝；执行实例检测到共享答案后再恢复原来的工具 Future。

### 后台媒体任务

审批后的媒体生成不会再占住原 HTTP 请求：`POST /api/canvas/run` 固化提示词与参数后返回
`job_id`，后台 `MediaJobWorker` 通过租约领取任务、定期心跳，并把结果写回画布。浏览器轮询
`GET /api/media/jobs/{job_id}`，刷新页面后会从本地保存的任务 ID 恢复跟踪。

默认使用内存队列和内嵌 Worker，适合本地开发。多进程部署时配置
`MEDIA_JOB_STORE_BACKEND=redis`、`MEDIA_WORKER_EMBEDDED=false`，然后单独运行：

```bash
python -m server.media_worker
```

Worker 租约过期的任务会进入 `unknown`，不会自动重新生成，以避免 Provider 已成功但本地未知时重复扣费。
TF-router 创建远端任务后会立刻把 `operation_id` 写入 invocation；用户点击“核对并恢复”时只查询该远端任务，
不会再次调用生成接口。每个节点还保存当前 `generation_id`，旧 Worker 晚到的结果只能进入审计结果，不能覆盖
新一代节点输出。当前仍未接入 Provider Webhook 与事务 Outbox，因此不宣称完整 exactly-once。

相关接口：`GET /api/canvases`、`GET /api/canvas`（含 `topo_order`/`problems`/`node_spec`）、
`POST /api/canvas/connect`（**带类型校验的连线**，成环会被拒）、`POST /api/canvas/arrange`、
`POST /api/canvas/approvals`（固化本次生成参数）、`POST /api/canvas/run`（校验审批并入队）、
`GET /api/media/jobs/{job_id}`（查询后台任务）、`POST /api/media/jobs/{job_id}/reconcile`（人工安全恢复）、
`POST /api/canvas/export`。

## MCP 插件与服务

项目同时支持 MCP 的两个方向：

- **MCP Client**：启动时读取 `.agent/mcp.json`，发现外部 MCP 工具，经过白名单过滤后以
  `mcp_<server>_<tool>` 名称加入 Agent。每个服务独立连接、超时和错误隔离，结果会截断并保留来源信息。
- **MCP Server**：把画布查询、故事工程创建、提示词导出、画布资源和短剧工作流 Prompt
  暴露给 Codex、Claude Desktop 等 MCP Host。

第一次配置可复制 `.agent/mcp.example.json` 为 `.agent/mcp.json`。密钥不要写入 JSON：
stdio 使用 `env_from`，远程 HTTP 使用 `headers_from`，二者都只保存宿主环境变量的名称。服务连接状态可通过
`GET /api/mcp/servers` 查看。

```bash
# 作为本地 stdio MCP Server 启动（Host 通常会自动执行这条命令）
python -m mcp_server.server

# 或作为 Streamable HTTP MCP Server 启动：http://127.0.0.1:8765/mcp
set DRAMAFLOW_MCP_TRANSPORT=streamable-http
python -m mcp_server.server

# 不调用大模型的 MCP 端到端回归
python demo/mcp_invariants.py
```

当前对外工具：`list_canvases`、`inspect_canvas`、`create_story_canvas`、`export_prompts`；
媒体生成没有直接暴露为 MCP 工具，仍由主应用走人工审批，避免外部 Host 绕过费用确认。

### 事件契约（前端渲染的唯一依据）

`POST /api/agent` 与 `/api/team` 返回 NDJSON，一行一个事件。持久化事件统一携带
`event_id`（递增游标）与 `run_id`（隔离同一会话的不同运行），前端把它归约成
**一条 assistant 消息 = 有序 `parts[]`**（思考 / 工具 / 正文内联）：

| 事件 | 关键字段 | 前端用途 |
|---|---|---|
| `session` / `agent_start` / `turn_start` / `turn_end` / `agent_end` | `session_id`、`turn` | 会话与轮次边界 |
| `message_start` / `message_update` / `message_end` | `block_id`、`delta`、`content`、`chars` | 一条回复的分组与流式正文 |
| `thinking_update` / `thinking_end` | `block_id`、`delta`、`chars` | 思考块（**仅推理模型**产生） |
| `tool_execution_start` / `tool_execution_end` | `tool`、`tool_call_id`、`args`、`isError`、`duration_ms`、`result`、`result_chars`；画布写操作另带 `canvas_changed`/`nodes`/`edges` | 工具卡（按 id 配对，同名工具不串卡） |
| `sub_agent_start` / `sub_agent_report` / `sub_agent_usage` / `sub_agent_end` / `sub_agent_state` | `sub_agent` | 子 Agent 块与嵌套步骤 |
| `usage` | `input_tokens`、`output_tokens`、`cached_tokens`、`total_tokens`、`label` | 用量条 |
| `team_step_start` / `team_done` | `member`、`role`、`tools`、`result`、`usage` | 团队流水线 |
| `done` / `aborted` / `error` | `stats` / `message` | 收尾（残留的"运行中"会被标成中断） |
| `ping` | `elapsed`、`dropped` | 5 秒无事件时的心跳：让界面能区分"服务端在忙"与"连接已死" |

> `sub_agent` 的语义是「**这个事件由哪个子 Agent 发出**」；委托工具结果里的子 Agent 名字叫
> `sub_agent_name`（两者曾撞名，导致父的工具结果被误挂进子块）。

### 前端开发工具

| 工具 | 用途 |
|---|---|
| `python demo/capture_events.py "任务"` | 抓真实事件流存 `workspace/events-*.ndjson` + 打印类型/字段统计 |
| `python demo/thinking_check.py` | 诊断推理内容是否可用（换模型/换 key 后先跑它） |
| `http://127.0.0.1:8000/?demo=1#chat` | 用假数据铺出全部形态（思考/工具成败/画布变更/子 Agent/Markdown），改样式不用花 API |
| `node web-vue/scripts/stream-selftest.mjs` | 事件 → parts 的确定性回归（不花 API 费用） |

## 与 Toonflow 的差距（如实说明）

**等价实现**：手写 agent loop、ToolContext（沙箱更严）、ToolPlugin 契约 + 动态装载/热重载、
`previousEntries` 增量计量、事件打标转发、父子双通道、JSONL 会话（含分支）、技能渐进式披露、
声明式团队、分段提示词、NDJSON 流。

**简化**：
- `ToolContext` 的 `media` / `question` / `canvas` 只有**协议能力位**（Protocol + 可空 + 缺失时 `CapabilityUnavailable`），
  没有真实 provider；Toonflow 的 `ffmpeg`、`sdk` 未实现。能力自检见 `tools/capabilities/`
- 子 Agent 状态机 5/6（缺 `inputRequired`，它依赖 `question` 能力）

**已补齐（原先是缺口）**：
- 子 Agent **状态与上报写进父会话 JSONL**（custom entry，不进上下文）；子会话**独立落盘**
- 重启后恢复子 Agent 列表，未跑完的标 `interrupted`「上次运行已中断」（对应 Toonflow 的孤儿判定）
- 同名子 Agent 复用：历史从文件加载，**增量计量的起始偏移也跟着恢复**
- 用量结算移入 `finally`（成功/中止/被取消/报错四条路径都不漏账）
- 子 Agent 结果 16000 字符截断 + `limited` 状态 + 空结果校验
- 服务端 `session_id` 的历史重启后可续（`Session.load`）

**未做**：A2A 协议、远程团队、@文件 mentions、memory 工具、前端交互桥（画布/提问卡片）、
askUser / canvas / ffmpeg / mediaGeneration / webSearch / webFetch 工具、多用户与鉴权。

定位说明：这不是"把 Toonflow 改成 Python"（TS 项目无法配置切换语言），
而是**参考其架构用 Python 重写编排层核心**；SDK 为 `openai`（provider 用 DeepSeek，改配置即可切 OpenAI）。

LangGraph 横向对照（需先 `pip install -r langgraph_branch/requirements-langgraph.txt`）：

| 脚本 | 验什么 |
|---|---|
| `python langgraph_branch/lg_minimal_agent.py` | StateGraph 版最小 Agent（与手写拓扑等价） |
| `python langgraph_branch/lg_interrupt.py` | 工具前审批 + SQLite checkpoint 续跑 |
| `python langgraph_branch/lg_send_fanout.py` | Send 扇出并行（实测 2.07x） |
| `python langgraph_branch/lg_subagent.py` | 子图委派 + 取消级联 + 增量归因（对照模块二） |
| `python langgraph_branch/compare.py` | **手写 vs LangGraph 量化对比**（两轮交叉取平均） |

## 目录

```
core/     config 配置 · events 事件 · llm 请求 · session 会话与用量账本（JSONL 落盘）
          compaction 上下文压缩（空摘要拒收）
          context ToolContext（能力包+沙箱） · plugin 插件契约 · loader 装载器 · mcp MCP Client
          agent 循环 · subagent 委派
mcp_server/ 对外 MCP Server（工具、资源、Prompt；stdio / Streamable HTTP）
tools/    word_count（自研） · workspace（读写范例）
teams/    storyboardTeam：team.json + members/*.md（声明式多 Agent）
skills/   storyboard（含 references/）· code_review —— 清单常驻，正文按需读
.agent/   sessions/*.jsonl（会话落盘，含分支与压缩记录）
server/   FastAPI：/api/agent（NDJSON 流）、取消、工具清单、热装载、MCP 状态
web/      index.html + app.js + style.css
workspace/ Agent 唯一能读写的沙箱目录
demo/     上面的验收脚本
langgraph_branch/ LangGraph 对照实现（主线不依赖它）
```

## 两个模块

**模块一 · 插件化工具系统**
工具不拿裸 `open()`，而是 `ToolContext` 能力包（`core/context.py`）；
路径必须先过沙箱（越界 + 符号链接双重校验）；
插件契约 `ToolPlugin = validate_config + create_tools`（`core/plugin.py`）；
元数据 `TOOL_META` 经 Pydantic 校验，`importlib` 动态装载 + 清缓存热重载（`core/loader.py`）。

**模块二 · 子 Agent 委派**
同名子 Agent 复用会话；取消通过 `asyncio.Event` + `Task.cancel()` 双路级联（`core/subagent.py`）；
父→子用 `subagent` 工具，子→父用 `report` 工具（事件 + 文本，不共享变量）；
用量只算新增：跑前记 `usage_offset()`，跑后 `usage_since(offset)`。

## 实测数据（deepseek-chat）

| 项 | 结果 |
|---|---|
| 增量 4817 vs 全量 7032 tokens | **重复计入 2215，虚高 46.0%** |
| 子 Agent 委派一次 | 子会话本次 5908 tokens |
| 取消级联 | 3.0s 触发 → 子会话 `aborted` |

复现：`python demo/usage_experiment.py`

## TS → Python 对照

| Toonflow（TS） | 本项目 |
|---|---|
| `createAgentToolContext()` | `core/context.py: ToolContext` |
| `ToolPlugin` interface（Zod） | `Protocol` + Pydantic |
| 文件头 `/*! toonflowTool */` 元数据 | 模块级 `TOOL_META` 常量 |
| `pathToFileURL` + import / 清 `require.cache` | `importlib` / `sys.modules.pop` |
| `AbortSignal.any` + `throwIfAborted` | `asyncio.Event` + `Task.cancel()` |
| `previousEntries` 偏移切片 | `session.usage_offset()` / `usage_since()` |
| Express + NDJSON | FastAPI + `StreamingResponse` |

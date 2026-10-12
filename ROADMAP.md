# Roadmap：从 Agent 运行时到短剧生成工具

参考 Toonflow（已实证调研，见 `Agent学习/PythonLab/T9-01 短剧生成工具改造方案.md`）。
这份是**可执行的清单**，方案与取舍写在笔记里，这里只放做什么 / 怎么验收。

## 目标链路

```
一句话创意 → ①剧本 → ②分镜(片段→镜头) → ③资产(角色/场景) → ④片段提示词({{ref N}})
          → 画布可视化(节点+连线+依赖顺序) → 导出全片提示词 →（P2）生成媒体
```

## 关键决策（先定死，避免返工）

| 决策 | 选择 | 理由 |
|---|---|---|
| 画布状态源 | **服务端**（工作区 JSON 文件） | 不需要前端↔服务端双向桥；前端没开时 Agent 也能建图 |
| 画布前端 | **自研极简**（DOM 节点 + SVG 贝塞尔 + transform 视口） | 现状是零构建 vanilla JS；节点 ≤100 无需库 |
| 执行顺序 | **服务端 DAG 拓扑排序 + 环检测** | Toonflow 靠 Agent 现场推理（无拓扑代码），这里做得更硬 |
| 引用编号 | `{{ref N}}` 按入边 `(source, source_handle)` 升序 | 对齐 Toonflow 的 `canvasExecution.md:54-58` |
| 正文存储 | 画布 JSON 只存路径，正文落 `assets/<node_id>/content.md` | 对齐 Toonflow（画布不存正文） |

## 数据模型

画布文件 `workspace/<name>.canvas.json`：
```json
{"toonflow_canvas": true, "name": "第1集", "viewport": {"x":0,"y":0,"zoom":1}, "nodes": [], "edges": []}
```

节点类型：`text` · `storyboard` · `asset` · `prompt`（P2 加 `image_gen` / `video_gen`）
端口类型：`STRING` · `IMAGE` · `VIDEO` · `AUDIO`（+ `*`）
节点状态：`draft` · `ready` · `generating` · `done` · `error`

---

## P0 · 图能生成、能看见 ✅ 已完成

- [x] `core/canvas.py` —— 画布模型 + 原子读写 + 拓扑排序/环检测 + ref 编号
- [x] `tools/canvas/tool.py` —— `get_canvas` / `add_node` / `update_node` / `connect_nodes` / `delete_nodes` / `set_text`
- [x] `skills/drama_script/SKILL.md` —— 剧本与分镜的产物格式（分镜 6 字段）
- [x] `web/canvas.js` —— 渲染 + 缩放平移 + 拖拽回写 + 详情侧栏 + 按拓扑分层整理
- [x] `demo/drama_demo.py` —— 一句创意 → Agent 建图 → 打印图与拓扑顺序

**门禁结果**（`python demo/drama_demo.py`）
- [x] 9 节点、13 条边、四类节点齐全
- [x] 通过环检测（拓扑序长度 9）、体检 0 问题、正文落盘
- [x] 浏览器可见：`http://127.0.0.1:8000/#canvas`
- 成本：99,004 tokens / 27 次工具调用（P1 优化）

## P1 · 流程闭环（进行中）

已完成：
- [x] `skills/shot_prompt/SKILL.md` —— 三段式提示词 + `{{ref N}}` 用法 + "不手写参考声明"铁律
- [x] `{{ref N}}` 自动展开（`Canvas.render_prompt`）：编号 → `【参考N】` + 参考声明（描述只取资产节点）
- [x] `export_prompt` 工具 + `POST /api/canvas/export` → 全片提示词 Markdown 落盘
- [x] 前端：拖拽保存、节点详情侧栏、状态色、导出弹层
- [x] **自纠闭环**：未展开编号能被算出 → 喂回模型 → `--repair` 模式实测清零（40808 tokens / 9 次调用）

待做：
- [x] `skills/asset_design/SKILL.md` —— 角色三视图 / 场景 / 道具 / 风格
- [x] `storyboard` 节点结构校验（`set_storyboard` 结构化写入 + 时长总和 = 目标时长）
- [x] `teams/dramaTeam/` —— 导演 → 编剧 → 美术 → 分镜 → 提示词 → 审校
- [x] `demo/canvas_inspect.py` —— 画布体检报告（不花钱的质检工具）
- [ ] 成本优化：团队一次 335186 tokens（上游只传摘要 + 产物路径、批量建节点）

**门禁（P1 达成）**
- [x] 导出文件里 `{{ref N}}` 与资产节点严格对应（`canvas_inspect` 可校验：未展开 = 0）
- [x] 图结构通过环检测与体检
- [x] 全流程无人工干预（dramaTeam 跑通：30s 短剧、体检 0 问题）
- [x] 时长超预算时报错（实测 60s 通过 / 50s 报错）

## P2 · 亮点（进行中）

已完成：
- [x] 结构校验器：`validate()` + `demo/canvas_inspect.py`（时长/覆盖/编号/资产 tags 全部可判定）
- [x] `MediaCapability` 真实接上：`core/media.py` 的 mock provider（**真 PNG 文件**、配色由提示词决定、可复现）
- [x] 生成节点 `image_gen` / `video_gen` + `run_canvas` 按**拓扑序**执行
- [x] 一键生成：`POST /api/canvas/run` + 前端按钮 + 逐节点结果提示
- [x] 前端缩略图（`/assets` 静态挂载）+ mock 徽章 + 生成中呼吸动画 + 画布下拉切换

待做：
- [ ] 真实生图 provider（实现 `MediaProvider` 协议 → 注入 `MediaRegistry`，工具零改动）
- [ ] `ffmpeg` 拼接（本地二进制）
- [ ] 多集 = 子目录
- [x] 成本优化（部分达成）：
  - [x] 入口/上游只传摘要（director 规划 -46%）
  - [x] `add_nodes` 批量建节点（asset_designer -70%）
  - [x] 成员白名单去冗余（schema -13.5%）
  - [x] 子会话压缩阈值 12000→8000（会话压缩 **-90%**，见 `demo/compact_bench.py`）
  - [ ] 仍未到目标：团队 335186 → 300055（-10.5%），下一步靠"验收失败自动重派"与模型路由
- [ ] **验收失败 → 自动重派该成员（限次）**：实测团队会在"片段2 未落盘"时结束
- [ ] 模型路由：分镜/审校等结构性环节换便宜模型（降单价，比降 tokens 更直接）

## MCP 阶段一 · 外部插件接入 ✅ 已完成

- [x] `.agent/mcp.json` 配置 stdio / Streamable HTTP 服务
- [x] 启动期工具发现、统一命名、JSON Schema 参数校验
- [x] 服务级工具白名单/黑名单、调用超时、结果长度限制
- [x] 单服务故障隔离与 `GET /api/mcp/servers` 可观测状态
- [x] 外部工具并入主 Agent 与子 Agent 的现有工具裁剪链路

## MCP 阶段二 · DramaFlow 作为 MCP Server ✅ 已完成

- [x] stdio 与 Streamable HTTP 两种传输
- [x] 画布列表/检查/创建/提示词导出 4 个工具
- [x] `dramaflow://canvas/{name}` 只读资源
- [x] `short_drama_workflow` Prompt 模板
- [x] 画布名和路径边界校验；媒体生成不对外直通，继续由人工审批
- [x] `demo/mcp_invariants.py` 覆盖 Server 与 Client Adapter 端到端调用

**暂不做**：3D 导演台、MCP 插件市场与安装 UI、A2A、远程团队、多用户鉴权、真实视频生成。

---

## 新增文件预估（≈1800 行，不含 P2）

```
core/canvas.py                 ~260   画布模型 + 拓扑排序 + ref 编号 + 导出
tools/canvas/tool.py           ~220   画布工具插件（复用 ToolPlugin 契约）
skills/drama_script/           ~150   剧本与分镜规范
skills/shot_prompt/            ~180   片段提示词模板（三段式）
skills/asset_design/           ~120   资产设定规范
web/canvas.js                  ~420   自研画布视图
server/app.py (路由)            ~90   /api/canvas + PATCH + export
teams/dramaTeam/                ~60   编剧/分镜/审校
demo/drama_demo.py             ~120   全链路验收
```

复用已有：`core/context.py`（沙箱+写锁）、`core/plugin.py`+`loader.py`（插件装载）、
`core/subagent.py`+`team.py`（委派）、`core/skills.py`（技能）、`core/session.py`（custom entry）、
`server/app.py`（NDJSON 流）、`web/`（对话与面板）。

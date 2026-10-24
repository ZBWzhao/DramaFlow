# 阶段 1：链路打通 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通"管线产物 → 产品系统"链路：导入 API 落库、剧本页渲染大纲+分集剧本（编辑落库）、制片台只读渲染四类产物、剧本页 AI 助手解绑画布（只读问答）、产物可导出。

**Architecture:** 新增 `core/pipeline_repository.py`（asyncpg 访问 `pipeline_artifacts` 表）+ `core/pipeline_import.py`（校验与合并核心）+ `server/pipeline.py`（`/api/pipeline` router，仿 `server/studio.py` 的 router 模式挂载）。前端遵循现有约定：数据全部由后端算好返回、显式保存（无 autosave）、新视图注册进 `router.js` + `AppShell.vue`。**项目身份 = 画布名**（`/api/studio/*` 全按 canvas 键，`pipeline_artifacts.project_id` 存画布名，TEXT）。

**Tech Stack:** FastAPI / asyncpg / pytest / Vue 3（options API 风格沿现有文件）

**工作区：** 本分支 `ZBWzhao/phase1-link`（merge `c36e431`：前端线 `feat/frontend-redesign` + 阶段 0 `ZBWzhao/tropicbird`），基线 153 测试全绿。

## Global Constraints

- 画布冻结：禁止修改 `skills/`、`teams/`、`core/skills.py`、`web-vue/src/canvas2/`、`web-vue/src/store.js`（画布页数据层）、`FloatingAgent.vue`/`ChatPanel.vue`（画布页 agent UI）
- 回归红线（不得破坏）：produce attach 事件流、四件套 single-flight NDJSON、资产表 sync 只增语义、AI 模型切换、`api.js` 流式签名
- `pipeline_artifacts.project_id` = **画布名**（TEXT，全局决策，写进契约文档）
- 导入合并语义（本计划 Task 1 定案并写入契约文档）：`character_refs` content = `{"characters":[<asset 对象>...]}`（导入接受单角色对象或合并对象，逐角色不互相覆盖）；`script` content 保持上游结构，导入按**集数**合并（同集号替换、他集保留）；其余五类 latest-wins 整行替换
- `schema_version` 导入时自动取 `core/pipeline_artifacts.py` 对应技能的 frontmatter 版本锚（契约文档"已知缺口"定义），请求体显式给的版本优先
- 错误形态沿 `web-vue/src/api.js:10-44` 期待：422 返回 `{"message": str, "findings": [str]}`；非法 JSON 拒绝且报错到字段
- 测试命令 `python -m pytest tests/ -v`；基线 153 通过，任何任务提交后不得少于基线
- 前端任务验收 = `npm run build`（web-vue 下）零错误 + 手动冒烟路径写在报告里；禁止重构共享组件
- 提交信息 `feat:`/`docs:`/`test:` 前缀，每任务至少一次提交
- 实施基地=本 worktree；完成后分支 `ZBWzhao/phase1-link` 即阶段 1 交付物

---

### Task 1: 契约收口 + 注册表顺序断言

**Files:**
- Modify: `docs/pipeline/data-contract.md`（存储与导入约定两条 + 新增"项目身份"一条）
- Modify: `tests/test_pipeline_artifacts.py`（追加顺序断言）

**Interfaces:**
- Produces: 全局定案三条——①`project_id` = 画布名；②`character_refs` 合并形状 `{"characters":[...]}`；③`script` 按集数合并（同集号替换、他集保留）。Task 2-8 依赖这三条。

- [ ] **Step 1: 写失败测试**

`tests/test_pipeline_artifacts.py` 追加：

```python
def test_artifacts_in_canonical_pipeline_order():
    names = [a.name for a in ARTIFACTS]
    assert names == ["outline", "script", "characters", "art", "staging", "storyboard", "character_refs"]
```

- [ ] **Step 2: 运行确认通过**（顺序本就正确，此测试是防回归锁）

Run: `python -m pytest tests/test_pipeline_artifacts.py -v` → 全 PASS

- [ ] **Step 3: 契约文档更新**

`docs/pipeline/data-contract.md` 存储与导入约定追加/修订三条：

```markdown
- **项目身份**：`pipeline_artifacts.project_id` = 画布名（`/api/studio/*` 的 canvas 键）。画布即项目，不另建映射；
- **character_refs 合并形状（阶段 1 定案）**：落库 content 为 `{"characters": [<上游 asset 对象>, ...]}`。导入端接受两种输入：单个 asset 对象（自动包装）或已合并对象；合并时按角色标识逐角色并入，同角色整对象替换，不得丢失其他角色；
- **script 分批合并（阶段 1 定案）**：导入端按集数合并——新批次中的集按集号替换对应集，未出现的集保留原样；集号字段以本契约 script 节字段表为准；非预期结构（无法识别集数组）时整行替换并记录告警。
```

- [ ] **Step 4: 提交**

```bash
git add docs/pipeline/data-contract.md tests/test_pipeline_artifacts.py
git commit -m "docs: lock phase-1 contract decisions (project id, merge shapes) + order assertion"
```

---

### Task 2: 产物仓库 + 导入合并核心

**Files:**
- Create: `core/pipeline_repository.py`
- Create: `core/pipeline_import.py`
- Test: `tests/pipeline/test_pipeline_import.py`、`tests/pipeline/test_pipeline_repository.py`

**Interfaces:**
- Consumes: `core/pipeline_artifacts.py`（ARTIFACTS/get）、Postgres `pipeline_artifacts` 表（0005 已应用，容器 dramaflow-postgres 运行中）、`core/postgres.py` 的 `PostgresDatabase`（先读该文件与 `core/production_repository.py` 学既有 asyncpg 模式与测试的 DB 夹具方式，照抄其连接/夹具惯例）
- Produces（Task 3+ 依赖，签名固定）:

```python
# core/pipeline_import.py
def prepare(
    art,                    # ArtifactType
    payload,                # dict | list —— 请求体 content
    existing: dict | None,  # 库中现有 content（同 project+type），无则 None
) -> tuple[dict | list, list[str]]:
    """返回 (落库 content, findings)。findings 非空表示有告警但可落库；
    结构非法时 raise PipelineImportError(message, findings)。"""

class PipelineImportError(Exception):
    def __init__(self, message: str, findings: list[str]): ...
    # 序列化为 {"message":..., "findings":[...]}（api.js 422 形态）

# core/pipeline_repository.py（asyncpg，模式照抄 production_repository）
async def save(db, project_id: str, art_type: str, content, source_run_id: str = "") -> dict
async def get(db, project_id: str, art_type: str) -> dict | None   # {"content":..., "schema_version":..., "updated_at":...}
async def list_all(db, project_id: str) -> list[dict]              # 按 artifact_type
async def delete(db, project_id: str, art_type: str) -> bool
```

- [ ] **Step 1: 写失败测试（合并核心，纯函数优先）**

`tests/pipeline/test_pipeline_import.py`（目录不存在则创建，无需 `__init__.py`）覆盖：
- `prepare` 对 outline：payload 为 dict → 原样返回，findings 为空
- 对 `characters`：单角色对象（含标识字段，字段名以 `docs/pipeline/data-contract.md` characters 节为准）→ 包装成 `{"characters":[obj]}`；已合并对象 → 逐角色并入 existing（同标识整对象替换、他角色保留）
- 对 `script`：existing 为 None → 原样落库；existing 有集 A、B，新 payload 含集 B'、C → 结果含 A、B'(新)、C（集号字段名以契约 script 节为准）；payload 缺集数组 → `PipelineImportError`
- 对 `staging`：非 dict → `PipelineImportError`
- 每类：payload 顶层出现契约字段表完全不认识的键 → finding 告警（不拒绝，与契约"不硬要求"条款一致）
- `PipelineImportError` 序列化形态 `{"message":..., "findings":[...]}`

- [ ] **Step 2: 运行确认失败**（模块不存在）

- [ ] **Step 3: 实现 `core/pipeline_import.py`**（最小实现让上述测试通过；集号/角色标识字段名从契约文档读出后写死为常量并注明来源）

- [ ] **Step 4: 写失败测试（仓库层）**

`tests/pipeline/test_pipeline_repository.py`：DB 夹具照抄本分支既有 DB 测试的惯例（先 `grep -rl "asyncpg\|PostgresDatabase" tests/` 找样板；若既有测试用假仓库，则真 DB 测试用 `pipeline_test` 库：session 级夹具连 dramaflow-postgres 容器，`CREATE DATABASE IF NOT EXISTS` 语义用 try/except，测试前 `TRUNCATE pipeline_artifacts`）。覆盖：save→get 往返（JSONB 内容保形）、save 同键覆盖（latest-wins）、list_all 返回 7 类、delete 返回 True/False。

- [ ] **Step 5: 运行确认失败 → 实现 `core/pipeline_repository.py` → 全绿**

Run: `python -m pytest tests/pipeline/ -v`

- [ ] **Step 6: 全套回归 + 提交**

```bash
python -m pytest tests/ -v   # ≥153+新增 全绿
git add core/pipeline_repository.py core/pipeline_import.py tests/pipeline/
git commit -m "feat: pipeline artifact repository and import merge core"
```

---

### Task 3: /api/pipeline 导入·查询·导出路由

**Files:**
- Create: `server/pipeline.py`（APIRouter(prefix="/api/pipeline")，模式仿 `server/studio.py:47` 与其 `configure()` 依赖注入写法）
- Modify: `server/app.py`（挂载点仿 `app.py:2709-2718` 的 studio 挂载，置于其后）
- Test: `tests/pipeline/test_pipeline_api.py`（FastAPI in-process 模式，先读本分支既有 API 测试样板）

**Interfaces:**
- Consumes: Task 2 全部；`server/registry.py` 的 db/registry 句柄获取方式（照 studio.py 的 configure 模式）
- Produces（Task 4-8 依赖，路由契约固定）:
  - `POST /api/pipeline/projects/{canvas}/artifacts/{artifact_type}`，body `{"content": <JSON>, "source_run_id"?: str, "schema_version"?: str}` → 200 `{"artifact_type","schema_version","updated_at","findings":[]}`；非法 → 422 `{"message","findings"}`；未知类型 → 404。幂等：同内容重复导入结果不变
  - `GET /api/pipeline/projects/{canvas}/artifacts` → `{"artifacts":[{"artifact_type","schema_version","updated_at"}...]}`
  - `GET /api/pipeline/projects/{canvas}/artifacts/{artifact_type}` → `{"artifact_type","schema_version","content",...}`；无 → 404
  - `GET /api/pipeline/projects/{canvas}/artifacts/{artifact_type}/export` → `application/json` 附件（Content-Disposition 文件名=注册表 filename），body=content
- [ ] **Step 1: 写失败测试**：用 `tests/fixtures/pipeline_run1/` 全部 7 类走 happy path（逐类导入→查→导出往返保形）；非法 JSON（截断/顶层数组给 staging）→ 422 且 findings 非空；未知类型 404；幂等（同 payload 导入两次，第二次 200 且 updated_at 不回退、内容不变）；characters 单角色对象导入后 GET 返回合并形状
- [ ] **Step 2: 确认失败 → 实现 router + 挂载 → 测试全绿**（导入调 `pipeline_import.prepare` + `pipeline_repository.save`；无 DB 时（CANVAS_STORE_BACKEND=file 且无 Postgres）路由返回 503 `{"message":"pipeline artifacts 需要 Postgres（compose.postgres.yml）"}`）
- [ ] **Step 3: 全套回归 + 提交** `feat: pipeline artifact import/query/export API`

---

### Task 4: 剧本页·管线数据开关与大纲渲染

**Files:**
- Modify: `web-vue/src/studio/studioStore.js`（追加 pipeline state + 方法，不改动既有方法）
- Modify: `web-vue/src/views/ScriptView.vue`（追加"管线大纲"tab，复用既有 tab 结构 `ScriptView.vue:268-422` 的写法）
- Create: `web-vue/src/views/script/PipelineOutlinePanel.vue`
- Test: 无前端单测框架——`npm run build` + 报告冒烟路径

**Interfaces:**
- Consumes: Task 3 GET 路由；studioStore 既有模式（`:139-151` loadAll fan-out）
- Produces: `studio.pipeline = { loaded:false, artifacts:{outline:{...content...}, ...} }`；`studio.loadPipeline(canvas)`；大纲 tab 组件 props `{ outline }`

- [ ] **Step 1: studioStore 追加**：`pipeline` state、`loadPipeline()`（GET artifacts → 有 outline 再 GET 详情）、方法保持既有风格（`studioStore.js:81-151` 模式）
- [ ] **Step 2: PipelineOutlinePanel.vue**：只读渲染 outline.json 五件套（改编说明/人物表/爽点表/分集梗概/资产清单——分节渲染，字段路径以契约 outline 节为准；数组渲染表格，缺失键留空不报错；顶部注明"来自管线产物，只读；修订走重导入（阶段 2 接体检模式）"）
- [ ] **Step 3: ScriptView 挂 tab**：`loadAll()` 里并行 `loadPipeline()`；tab 栏追加"管线大纲"，仅当 `pipeline.artifacts.outline` 存在时显示 tab（画布老项目不受影响）
- [ ] **Step 4: `cd web-vue && npm run build` 零错误 + 提交** `feat: script page pipeline outline tab (read-only)`

---

### Task 5: 剧本页·分集剧本切换管线数据源 + 编辑落库

**Files:**
- Modify: `web-vue/src/views/ScriptView.vue`（集列表与正文数据源分支：有 script 产物 → 产物优先，画布路径保留为 fallback）
- Modify: `web-vue/src/studio/studioStore.js`（追加 `savePipelineEpisode(no, text)`）
- Modify: `server/pipeline.py` + `core/pipeline_import.py`（新增集级端点）
- Test: `tests/pipeline/test_pipeline_api.py` 追加集级端点用例

**Interfaces:**
- Produces:
  - `PUT /api/pipeline/projects/{canvas}/artifacts/script/episodes/{no}`，body `{"text": str}` → 200；集号不存在 → 404。实现：读整行 → 定位该集对象 → 更新其正文文本字段（字段名以契约 script 节为准）→ `prepare` 复检 → save（沿用按集合并语义，天然只动这一集）
- [ ] **Step 1: 后端 TDD**：fixtures script.json 导入后 PUT 集 1 → GET 确认仅集 1 变化、集 2/3 逐字节不变；PUT 不存在集号 → 404
- [ ] **Step 2: 前端**：`scriptArtifactEpisodes()` 派生集列表（号/标题/正文字段路径以契约为准）；ScriptView：`hasPipelineScript` 时集列表与正文取产物，保存按钮走 `savePipelineEpisode`（只存当前集，替代"整本重导入"），dirty-guard 逻辑复用 `ScriptView.vue:181-217`；无产物时原路径原样
- [ ] **Step 3: `npm run build` 零错误 + 全套 pytest 回归 + 提交** `feat: pipeline-first episode scripts with per-episode save`

---

### Task 6: 制片台·管线产物只读视图

**Files:**
- Create: `web-vue/src/views/PipelineView.vue` + `web-vue/src/views/pipeline/{CastPanel,ArtPanel,StagingPanel,StoryboardPanel}.vue`
- Modify: `web-vue/src/router.js`（子路由 `pipeline`，沿 `router.js:1-5` 模式）、`web-vue/src/shell/AppShell.vue`（tab，沿 `AppShell.vue:87-92`）、`web-vue/src/studio/studioStore.js`（复用 Task 4 的 `pipeline` state，补全四类详情加载）

**Interfaces:**
- Consumes: Task 3 GET/GET 详情；AssetsView/studioStore 的"后端算好、前端只读"约定
- [ ] **Step 1: 四个 Panel**：cast=角色卡表格（形象/音色提示词可折叠复制）；art=场景/道具设定表；staging=分场站位表（走位句整段展示）；storyboard=分段分镜表（段→镜两级，提示词列可复制）。全部只读；空态显示"该项目无此管线产物"；组件各自容错（缺键留空）
- [ ] **Step 2: 路由 + tab**："管线产物" tab 置于"制作"之后；路由守卫无需（数据懒加载）
- [ ] **Step 3: `npm run build` 零错误 + 提交** `feat: studio pipeline artifacts read-only view`

---

### Task 7: 剧本页 AI 助手解绑画布（只读问答）

**Files:**
- Modify: `server/app.py`（`ChatRequest` 加 `mode: Literal["canvas","script_qa"] = "canvas"`，沿 `app.py:159,644-694`；`script_qa` 时 `tools_for(..., allowed_tools=[])` + prompt 追加只读块，机制沿 `app.py:658,671-678` 与 `server/registry.py:75-91`）
- Modify: `web-vue/src/ai/AiPanel.vue`（props 加 `mode`，请求体带 `mode`，`ScriptView.vue:395-402` 挂载点传 `mode="script_qa"`；AppShell 抽屉不传保持 canvas）
- Test: `tests/pipeline/test_script_qa_mode.py`

**Interfaces:**
- Produces: `script_qa` 语义——零工具（连 read_file 都不给）、系统提示追加块：

```
<assistant_mode mode="script_qa">
你是剧本页的只读问答助手。你不能调用任何工具、不能修改任何数据；
只依据下方项目概要和对话内容回答问题。
<project_summary>{canvas 名 + meta.story_title/story_summary/episodes 列表}</project_summary>
</assistant_mode>
```

- [ ] **Step 1: 后端 TDD**：POST /api/agent with `mode="script_qa"` → 断言 session 工具集为空（检查 live.agent.tools 或 tools_for 返回）；断言 system prompt 含 `script_qa` 块与项目概要；`mode` 缺省 → 行为与现状逐字段一致（回归红线）
- [ ] **Step 2: 前端**：AiPanel 透传 mode；ScriptView 挂载点传 `script_qa`；输入框上方加一行小字"只读模式：不会修改项目数据"
- [ ] **Step 3: `npm run build` + 全套 pytest 回归 + 提交** `feat: script page assistant read-only qa mode (canvas decoupled)`

---

### Task 8: 端到端验收联测 + 清单核对

**Files:**
- Create: `tests/pipeline/test_phase1_acceptance.py`
- Modify: `docs/superpowers/specs/2026-10-24-skill-layer-refactor-design.md`（勾选阶段 1 验收清单）

- [ ] **Step 1: 写验收测试**（一条龙）：7 类 fixtures 依序导入同一画布名 → GET list 返回 7 类 → outline/script/cast GET 内容与 fixtures 逐字段一致 → script PUT 集 2 后导出往返只变集 2 → characters 单角色再导入不丢第一角色 → 非法 payload 422 → **跨产物一致性断言**（继承事项④）：导入后 outline 的集数参数 == script 的集数 == storyboard 覆盖的集数（fixtures 为 3；字段路径以契约为准，断言函数化以便阶段 2 复用为导入告警）
- [ ] **Step 2: 手动冒烟**（写进报告）：起服务（run.py/compose），用 fixtures 建项目导入，浏览器过一遍：剧本页两 tab、集编辑保存、制片台只读四段、剧本页助手零画布动作（问"现在有哪些集？"验证只答不写）；`npm run build` 终验
- [ ] **Step 3: 全套 pytest + 提交** `test: phase-1 acceptance end-to-end`；勾选设计文档阶段 1 验收清单（如实）

---

## 完成后

对照 spec 阶段 1 验收线：导入成功/非法拒绝 ✓、剧本页显示+编辑保存 ✓、制片台四类 ✓、助手零画布调用 ✓、人工干预 <1h（fixtures 就绪时导入+验证流程）✓ → 阶段 2（大脑入产品）另立计划。

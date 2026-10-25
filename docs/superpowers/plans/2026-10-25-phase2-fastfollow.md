# 阶段 2 快速跟进实施计划：真链路最后一公里

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉挡住"真实全链可用"的三件事——script 时长门真模型不收敛、F5 后 run 卡丢失、顶栏项目名串项目——并用真模型复冒烟验证收敛。

**Architecture:** Task 1 全在执行器侧（repair_note 静态提示 + 修复尾按策略分支，不碰 vendored 技能文件）；Task 2 后端补 list/sweep 两个小接口 + 前端 run 恢复（复用 Task 4 的 project_id 门与 hello 同步机制）；Task 3 两处前端小修。Task 4 是验证任务（花真钱，有上限）。

**Tech Stack:** FastAPI + asyncpg（Postgres dramaflow 库）、Vue3 + pinia、DeepSeek API（冒烟）。

## Global Constraints

- 画布冻结红线——不许触碰：`web-vue/src/canvas2/`、`web-vue/src/store.js`（画布相关 state）、`web-vue/src/ai/FloatingAgent.vue`、`web-vue/src/ai/ChatPanel.vue`、`server/registry.py`、`tools/`、`skills/`、`core/skills.py`、`teams/`（注意 `web-vue/src/shell/AppShell.vue` 不在红线内）
- outline → script 人工确认门不可跳过；不碰 `core/pipeline_run.py` 里 `GATE_STAGE` 相关守卫
- 质量门阈值（±15% 时长）是产品决策，本计划不改门脚本、不放宽阈值（候选 d 被否，除非复冒烟后用户另行拍板）
- vendored 技能文件（`pipeline_skills/**`）只读——口径调整只走执行器侧 repair_note / 纪律段
- 每任务收尾：`python -m pytest tests/ -q` 全绿（基线 262）+ 涉前端任务 `cd web-vue && npm run build` 零错误
- 仓库无 pytest-asyncio，测试用 asyncio.run 风格（参照 tests/pipeline/test_pipeline_run.py）
- Commits：lowercase conventional prefix，每任务一个提交
- ⚠️ 磁盘注意：C: 盘曾满（清出 1.85GB 余量）。避免批量构建/大文件写盘；前端只 build 不装新依赖

---

### Task 1: script 时长门收敛（执行器侧）+ chain_repair 修复尾策略化

**Files:**
- Modify: `core/skill_executor.py`（STAGE_SPECS["script"] 的 repair_note；run_stage 尾部修复消息 ~line 610-616）
- Test: `tests/pipeline/test_skill_executor.py`

**Interfaces:**
- Consumes: `STAGE_SPECS`（各 spec 可选 `repair_note` 字段，被 `_compose_system` line 474/487 与 run_stage 修复消息 line 614 拼接）；run_stage 主循环与 `box.run_gate()` 权威门
- Produces: 无新接口——纯口径修正。修复消息对 chain_repair 政策不再指示自检轮

背景（2026-10-25 冒烟实证）：deepseek-chat 跑 script 阶段 20 轮未收敛，门报"每集欠 X 秒（估 Ys/目标 120s）"但模型只加一两句对话，欠时最多的集始终没补够。同时 outline 冒烟显示模型会忠实执行具体的折算指令（data 传错被工具纠正后立即改对）。

- [ ] **Step 1: 写失败测试**

在 `tests/pipeline/test_skill_executor.py` 追加三个测试（沿用 ScriptedLLM + 真门 + asyncio.run 惯例）：

```python
def test_script_repair_note_mentions_duration_conversion():
    # script spec 的 repair_note 必须给出时长折算与定向补足指令
    note = STAGE_SPECS["script"].get("repair_note") or ""
    assert "4.5" in note and "动作行" in note   # 折算公式
    assert "欠时最多" in note                    # 定向补足策略

def test_agent_repair_tail_demands_self_gate():
    # agent 政策（outline）：修复尾要求修完自检再 finish
    result = asyncio.run(run_stage(  # 用 outline spec + 令首轮 finish 触发失败门的 ScriptedLLM
        ...))  # 捕获第二轮注入的 user 消息（ScriptedLLM 快照 messages）
    repair_msg = [m for m in result.messages if m["role"] == "user"][-1]["content"]
    assert "run_quality_gate" in repair_msg

def test_chain_repair_repair_tail_skips_self_gate():
    # chain_repair（characters）：修复尾禁止自检（预算 2 轮，自检=白烧）
    ...  # 同上，用 characters spec
    assert "立刻再 finish" in repair_msg and "run_quality_gate，报告全过" not in repair_msg
```

（`...` 处按文件内既有 `test_...repair_message_reaches_model` 的替身写法补全；核心断言是注入的修复消息内容。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/pipeline/test_skill_executor.py -q -k "repair_note or repair_tail"`
Expected: FAIL（repair_note 缺失 / 尾巴未分支）

- [ ] **Step 3: 实现**

`core/skill_executor.py` STAGE_SPECS["script"]（现 ~line 90-110）加：

```python
"repair_note": ("时长门按集报差距（估Xs/目标Ys）：每欠 4.5 秒 ≈ 补 20 字对白或 1 个动作行"
                "（动作按 2.5 秒/行折算）。优先补欠时最多的集，一轮把差额一次补够，"
                "不要把轮数花在给已接近达标的集微调；episodes 整组重发时已达标集原样带上，"
                "不许降低它们的时长。"),
```

run_stage 修复消息尾（现 ~line 615）按策略分支：

```python
tail = ("修完同一轮立刻再 finish，不要先 run_quality_gate（预算只有 2 轮，自检等于白烧）。"
        if spec.get("policy") == "chain_repair"
        else "修完 run_quality_gate，报告全过就立刻 finish。")
messages.append({"role": "user",
                 "content": f"质量门未过（第 {rnd} 轮），报告如下：\n" + output[-GATE_TAIL:]
                            + "\n逐条修复：报告点名哪个键，就重调一次 write_draft 覆盖那个键"
                              "的完整数据（数组键整组重发全部条目，别只发单条）。只动点名的键。"
                              + (spec.get("repair_note") or "")
                              + tail})
```

- [ ] **Step 4: 全套测试 + 提交**

Run: `python -m pytest tests/ -q` → 262+ 全绿

```bash
git add core/skill_executor.py tests/pipeline/test_skill_executor.py
git commit -m "fix: script repair note with duration conversion; chain_repair tail skips self-gate"
```

---

### Task 2: runs-list 接口 + F5 重挂载 + 启动清扫

**Files:**
- Modify: `core/pipeline_run.py`（RunStore 加 list_by_project / sweep_interrupted）
- Modify: `server/pipeline_runs.py`（GET 列表端点；导出 sweep 调用）
- Modify: `server/app.py`（lifespan 里 pipeline_api.startup() 后调 sweep，~line 103）
- Modify: `web-vue/src/studio/studioStore.js`（loadPipeline 尾部恢复 run）
- Test: `tests/pipeline/test_pipeline_run.py`（sweep/list）、`tests/pipeline/test_pipeline_runs_api.py`（列表端点）

**Interfaces:**
- Consumes: `RunStore`（`_row()` 行整形、0006 表结构）；前端 `studio.run` 形状 `{id, project_id, status, stage, stages, events, link}`、`_openRunStream()`（hello 帧同步 status/stage）、跨项目门 `run.project_id === studio.canvas`
- Produces: `GET /api/pipeline/projects/{canvas}/runs` → `{"runs": [{run_id, status, stage, updated_at}...]}`（updated_at 倒序，limit 10）；`RunStore.sweep_interrupted() -> int`

- [ ] **Step 1: 写失败测试**

```python
# test_pipeline_run.py
def test_list_by_project_orders_and_filters():
    # 建两个项目的 runs（直接 RunStore.create），update 触碰其一 updated_at
    rows = asyncio.run(store.list_by_project(pid_a))
    assert [r["run_id"] for r in rows] == [rid_a2, rid_a1]   # 新的在前
    assert all(r["project_id"] == pid_a for r in rows)

def test_sweep_interrupted_fails_only_running():
    # 建 running / gated / done 各一 → sweep → running 变 failed 且 findings 含"重启"，
    # gated / done 原状；返回值为 1
```

```python
# test_pipeline_runs_api.py
def test_runs_list_endpoint_empty_and_isolation():
    r = client.get(f"/api/pipeline/projects/{quote('空项目')}/runs")
    assert r.status_code == 200 and r.json() == {"runs": []}
    # 项目 A 有 run，查项目 B 仍空；查 A 得一条且字段齐
```

- [ ] **Step 2: 跑失败** → `AttributeError`/404

- [ ] **Step 3: 实现**

`core/pipeline_run.py` RunStore：

```python
async def list_by_project(self, project_id: str, limit: int = 10) -> list[dict]:
    rows = await self._client().fetch(
        "SELECT * FROM pipeline_runs WHERE project_id=$1 ORDER BY updated_at DESC LIMIT $2",
        project_id, limit)
    return [self._row(r) for r in rows]

async def sweep_interrupted(self) -> int:
    """服务器重启后，running 状态的 run 没有驱动任务，永远停在原地——
    启动时统一转 failed 并留 findings，让用户可以重试。gated 是 parked
    状态（不需要任务驱动），保留给重挂载后的批准流程。"""
    n = await self._client().execute(
        "UPDATE pipeline_runs SET status='failed', updated_at=$1, "
        "findings = COALESCE(findings,'[]'::jsonb) || $2::jsonb WHERE status='running'",
        time.time(), json.dumps([{"gate": "runtime",
                                  "detail": "服务器重启中断本次运行，可重试"}]))
    return int(n.split()[-1])
```

`server/pipeline_runs.py` 路由（与既有端点同文件同风格）+ 模块级：

```python
@router.get("/projects/{canvas}/runs")
async def list_runs(canvas: str):
    db = ...
    store, _, _ = _STATE  # 与既有端点同取法
    return {"runs": [{"run_id": r["run_id"], "status": r["status"],
                      "stage": r["stage"], "updated_at": r["updated_at"]}
                     for r in await store.list_by_project(canvas)]}
```

（`_STATE` 取法按文件内既有端点的实际形态照抄；sweep 导出：`async def sweep_interrupted_runs()` 供 lifespan 调。）

`server/app.py` lifespan（line 103 后）：

```python
await pipeline_api.startup()
from server import pipeline_runs as pipeline_runs_api
await pipeline_runs_api.sweep_interrupted_runs()   # 重启清扫：running→failed 可重试
```

（import 放文件顶部既有管线 import 区，跟随现有风格。）

`web-vue/src/studio/studioStore.js` loadPipeline 尾部（跨项目安全：恢复的 run 立即带 project_id）：

```javascript
// F5 重挂载：本项目有进行中的 run（running/gated/failed 可重试）就恢复 run 卡，
// done/cancelled 不恢复（终态没有后续事件）。hello 帧负责同步最新 status/stage。
try {
  const { runs } = await getJSON(`/api/pipeline/projects/${encodeURIComponent(canvas)}/runs`);
  const live = runs.find(r => ["running", "gated", "failed"].includes(r.status));
  if (live && (!studio.run.id || studio.run.project_id !== canvas)) {
    studio.run = { id: live.run_id, project_id: canvas, status: live.status,
                   stage: live.stage || "", stages: {}, events: [], link: "restored" };
    _openRunStream();
  }
} catch { /* 列表接口失败不阻断产物加载 */ }
```

（`getJSON`/studio/run 细节按文件现状对齐；注意恢复条件里 `studio.run.project_id !== canvas` 防止覆盖刚创建的本地 run。）

- [ ] **Step 4: 全套测试 + build + 提交**

Run: `python -m pytest tests/ -q` 全绿；`cd web-vue && npm run build` 零错误

```bash
git add core/pipeline_run.py server/pipeline_runs.py server/app.py web-vue/src/studio/studioStore.js tests/
git commit -m "feat: runs list endpoint, F5 run reattach, boot sweep of interrupted runs"
```

---

### Task 3: 顶栏项目名刷新 + retry 防抖

**Files:**
- Modify: `web-vue/src/shell/AppShell.vue`（line 28 ptitle 计算）
- Modify: `web-vue/src/views/pipeline/CreateRunPanel.vue`（重试按钮 ~line 425-433/510）
- Test: 无（前端；build + 浏览器复验）

**Interfaces:** Consumes `route.params.name`、`studio.project`、`retryRun()`。Produces 无。

- [ ] **Step 1: ptitle 防串项目**

AppShell.vue line 28 改为：store 里项目与路由不一致时回退路由参数（解码）——直接 URL 进项目、store 尚未加载新项目时顶栏如实显示 canvas 名而不是上一个项目的标题：

```javascript
const ptitle = computed(() => {
  if (studio.project?.canvas === pname.value && studio.project?.title) return studio.project.title;
  try { return decodeURIComponent(pname.value); } catch { return pname.value; }
});
```

（若 AppShell 已有 route watcher 在 line 78 附近设置 `studio.canvas`，顺路核对 `studio.project` 的加载时机，有现成的项目元数据加载函数就在 watcher 里调用；没有就只做上面的回退——回退已保证"顶栏永远如实"。）

- [ ] **Step 2: retry in-flight 防抖**

CreateRunPanel.vue：加 `const retrying = ref(false)`；`retryFailedStage` 里 try/finally 置位复位；按钮 `:disabled="retrying"`、文案进行中变「重试中…」。参照同文件 `approving`/`starting` 的既有模式。

- [ ] **Step 3: build + 提交**

Run: `cd web-vue && npm run build` 零错误

```bash
git add web-vue/src/shell/AppShell.vue web-vue/src/views/pipeline/CreateRunPanel.vue
git commit -m "fix: header title follows route project; retry button in-flight guard"
```

---

### Task 4: 真模型复冒烟（成本上限 ¥3）+ 零成本重挂载浏览器验证

**Files:**
- Modify: `docs/pipeline/phase2-acceptance.md`（第 2 层表格补结果）
- 无代码（验证任务）

**Interfaces:** Consumes Task 1 的执行器口径、smoke CLI `python -m core.skill_executor <stage> <小说.txt> --project smoke-outline-final --provider deepseek`（上游 outline 已在 pipeline_test 库）。Produces 验收文档证据。

- [ ] **Step 1: script 复冒烟**

```bash
CANVAS_STORE_BACKEND=postgres DATABASE_URL=postgresql://postgres:postgres@localhost:5432/dramaflow \
  python -X utf8 -m core.skill_executor script tests/fixtures/sample_novel_v1.txt \
  --project smoke-outline-final --provider deepseek
```

判定：`ok=True` + 落库 → 收敛成功，记 iterations/gate_runs；仍失败 → 记录新 deficits（对比上轮 27.6s 是否收窄），把阈值决策（候选 d）上升给用户，不再自动重试。

- [ ] **Step 2: 过了则顺跑四阶段**

characters → art → staging → storyboard 依次冒烟（同项目；characters/art 走 chain_repair 两轮纪律验证终审标记的风险点）。任一阶段失败即停，记录。

- [ ] **Step 3: 零成本验证 F5 重挂载**

用 python 往 dramaflow 库直接插一条 `status='gated'` 的 pipeline_runs 行（造 run_id='smoke-reattach', project=《署名》canvas, novel_text 随意），CDP 打开 `/project/署名/pipeline` → 断言 run 卡出现且「确认，开写剧本」可见；刷新页面 → run 卡仍在（重挂载生效）；调 approve 应得到真实推进（会跑真 LLM，改为断言按钮可见即可，不点击）。完事删该行。

- [ ] **Step 4: 余额核对 + 文档 + 提交**

前后查 DeepSeek 余额（本任务累计 >¥3 立即停止并报告）。验收文档第 2 层表格补齐实测行，第 3 层改写为"已具备全链条件 / 剩余风险"。

```bash
git add docs/pipeline/phase2-acceptance.md
git commit -m "docs: re-smoke results — script convergence and production stages evidence"
```

---

## 验收清单

- [ ] 全套 pytest ≥ 266 全绿（262 + Task 1 三条 + Task 2 至少四条）
- [ ] script 真模型冒烟收敛落库（或明确上升阈值产品决策）
- [ ] gated run 的 run 卡在 F5 后依然可见（零成本浏览器验证）
- [ ] 顶栏项目名在 direct-URL 换项目时如实显示当前项目
- [ ] 服务器重启后 running run 自动转 failed 且可重试
- [ ] 前端 build 零错误；红线文件零触碰

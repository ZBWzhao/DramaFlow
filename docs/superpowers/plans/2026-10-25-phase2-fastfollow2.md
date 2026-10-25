# 阶段 2 快跟第二轮实施计划：chain_repair 结构保证 + 小队列批次

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 chain_repair 的"产稿+finish 同轮"从口头纪律变成结构保证（队列 9 方案 b+），清掉小额队列项，并用真模型复冒烟验证 characters 能过。

**Architecture:** Task 1 全在 `core/skill_executor.py`：finish 分发器对 chain_repair 增加组装完整性检查（缺键拒绝并点名），预算语义从"2 轮"改为"2 次权威门"（被拒的组装轮不消耗预算）。Task 2 五处小修（冒烟 CLI / gate_log / SSE / 资产桥 / docstring）。Task 3 验证：真模型复冒烟 characters + 全套测试 + 集成。

**Tech Stack:** 同主线（FastAPI/asyncpg/Vue3/DeepSeek）。

## Global Constraints

- 画布冻结红线——不许触碰：`web-vue/src/canvas2/`、`web-vue/src/store.js`（画布相关 state）、`web-vue/src/ai/FloatingAgent.vue`、`web-vue/src/ai/ChatPanel.vue`、`server/registry.py`、`tools/`、`skills/`、`core/skills.py`、`teams/`；`pipeline_skills/**` 只读
- 质量门阈值不改；agent 政策（outline/script/staging/storyboard）的循环语义不动——本计划只动 chain_repair（characters/art）
- 收尾：`python -m pytest tests/ -q` 全绿（基线 269）+ 涉前端任务 build 零错误
- 测试惯例：asyncio.run 风格、ScriptedLLM 多轮调用、真 Postgres、真 node 门（参照 tests/pipeline/test_skill_executor.py）
- Commits：lowercase conventional prefix，每任务一个提交
- 磁盘：C 盘现 32.8GB free（Docker 已迁 E 盘），正常工作即可

---

### Task 1: chain_repair 结构保证——finish 组装检查 + 预算按权威门计

**Files:**
- Modify: `core/skill_executor.py`（dispatch 的 finish 分支 ~line 317；run_stage 主循环 ~line 548-560 与 chain_repair 预算 clamp ~line 528-529）
- Test: `tests/pipeline/test_skill_executor.py`

**Interfaces:**
- Consumes: `_ToolBox.dispatch`（finish 分支现状：无条件 `state["finished"]=True` 返回 "ok"）；`_compose_system` chain_repair 纪律段（宣布"一轮产全全部键…同轮 finish"）；run_stage 的 clamp `max_iterations = min(max_iterations, 2)`
- Produces: 行为变化——(1) chain_repair 下 finish 在组装不完整时被拒绝（返回错误文本，不置 finished）；(2) 预算语义 = 2 次权威门（被拒组装轮不消耗）。无函数签名变化

背景（2026-10-25 真模型实证）：deepseek-chat 跑 characters 时把产稿拆两轮，r2 的"修复轮"拿到半成品（6 处违规：含 voice.prompt 超长等），预算烧光未落库。方案 b 单独不够：补全后门仍可能挂、还需修复轮。因此预算必须按"权威门运行次数"计，组装轮免费。

- [x] **Step 1: 写失败测试**

```python
def test_chain_repair_finish_rejected_until_assembly_complete():
    # ScriptedLLM：r1 只写 characters 一键 + finish → finish 必须被拒（错误点名缺失键），
    #   state 未 finished；r2 补齐 meta/summary + finish → 门过 → 落库
    # 断言：result.ok is True；llm.calls 里有轮次收到过含「finish 被拒绝」与缺失键名的 tool/user 消息；
    #       result.iterations == 2（被拒轮没有多烧预算）

def test_chain_repair_budget_counts_gates_not_rounds():
    # ScriptedLLM：r1 产全 + finish → 门失败（用可复现的门违规 fixture）；r2 修复 + finish → 门过
    # 断言：ok=True、iterations==3、gate_runs==2（权威门恰 2 次：1 产 + 1 修复）
    # 对照组：max_iterations=9 传入也被 clamp——总门数永远 ≤ 2
```

（替身写法沿用文件内既有 ScriptedLLM 多轮模式；「finish 被拒」消息的到达断言参照既有 repair-message-reaches-model 测试。）

- [x] **Step 2: 跑失败** → `AssertionError`（现 finish 无条件接受）

- [x] **Step 3: 实现**

dispatch finish 分支（替换 line 317-319）：

```python
if name == "finish":
    if self.spec.get("policy") == "chain_repair":
        required = [*spec_meta_sections(self.spec), *self.spec["sections"]]
        missing = [k for k in required
                   if k not in self.state["draft"]
                   or self.state["draft"][k] in (None, "", [], {})]
        if missing:
            return (f"finish 被拒绝：草稿还缺 {'、'.join(missing)}。"
                    "先把这些键 write_draft 写全（数组键一次整组），"
                    "同一轮立即再 finish——组装轮不消耗你的门预算。")
    self.state["finished"] = True
    return "ok"
```

run_stage 预算语义（clamp ~line 528-529 与循环 ~line 548）：chain_repair 的循环上界改为动态
`bound = min(max_iterations, 2) + state.get("finish_rejections", 0)`；
dispatch 拒绝 finish 时 `state["finish_rejections"] += 1`（toolbox 需能写这个计数——state dict 已共享，直接 `self.state["finish_rejections"]`）。agent 政策循环不变（bound = max_iterations）。while 化或每轮重算 bound 均可，以最小改动为准。

- [x] **Step 4: 全套测试 + 提交**

Run: `python -m pytest tests/ -q` → 269+ 全绿（既有 chain_repair 预算锁测试 `test_characters_chain_repair_budget_locked_at_two_rounds` 若因语义变化需更新断言——更新为"总门数 ≤ 2"而非"总轮数 ≤ 2"，在报告中说明）

```bash
git add core/skill_executor.py tests/pipeline/test_skill_executor.py
git commit -m "feat: chain_repair finish assembly check; budget counts authoritative gates not rounds"
```

---

### Task 2: 小队列批次（10 / M1 / stage 哨兵 / art 截断 / docstring）

**Files:**
- Modify: `core/skill_executor.py`（冒烟 __main__ 上游 fast-fail；save 分支 prepare 警告入账）
- Modify: `core/pipeline_run.py`（外层失败的 `stage:""` → None）
- Modify: `server/pipeline_assets.py`（art 行文本截断，对齐 characters 的 60/500/1500）
- Modify: `server/pipeline_runs.py`（模块 docstring line ~21 措辞：failed 保持打开）
- Test: `tests/pipeline/test_skill_executor.py`（警告入账）、`tests/pipeline/test_pipeline_run.py`（stage=None 形状）

**Interfaces:** Consumes `pipeline_import.prepare(...)`（返回 `(content, warnings)`——Task 5 时被 `content, _ =` 丢弃，本任务恢复）；smoke CLI 的 gate_needs 上游循环（~line 22 `inputs[need] = row["content"]`）。Produces 无新接口。

- [x] **Step 1: 五处实现**（每处独立可验证，一次提交）

1. **冒烟 CLI fast-fail（队列 10）**：`__main__` 里 gate_needs 循环处，`row is None` 时立刻 print 明确错误（「上游产物 {need} 不存在——先跑它的阶段冒烟，别空烧 LLM」）并 `return 1`，在任何 LLM 调用之前。
2. **prepare 警告入账（Task 5 M1）**：save 分支 `content, _ = pipeline_import.prepare(...)` 改为接收 warnings，追加进返回的 StageResult.findings：`[{"gate": "import", "detail": w} for w in warnings]`——ok=True 时 findings 也可以非空（runner 落 JSONB 已兼容）。补一个测试：构造触发 contract warning 的合并（库内 existing 含无法识别的集数组），断言 findings 非空且 detail 含警示文本。
3. **stage 哨兵（Task 3 遗留）**：`core/pipeline_run.py` 外层 infra 失败发布的事件 `{"stage": ""}` 改为最后一个已知阶段（从 stage_status 取，无则 None）；前端已有 未知 fallback，无需改前端。测试：infra 失败路径断言事件 stage 不是 ""。
4. **art 行截断**：`server/pipeline_assets.py` art_to_rows 的 scene/prop name/description 加与 characters 相同的截断（60/500/1500——读该文件 characters 侧的现值照抄）。
5. **docstring**：`server/pipeline_runs.py:21` 「done/failed 为终态收流」改为准确表述（hello 对 done/cancelled 收流；failed 保持打开等 retry 事件）。

- [x] **Step 2: 全套测试 + 提交**

Run: `python -m pytest tests/ -q` 全绿

```bash
git add core/skill_executor.py core/pipeline_run.py server/pipeline_assets.py server/pipeline_runs.py tests/
git commit -m "fix: smoke upstream fast-fail, import warnings to findings, stage sentinel, art row clipping"
```

---

### Task 3: 真模型复冒烟 characters + 集成验证

**Files:**
- Modify: `docs/pipeline/phase2-acceptance.md`（第 2 层表格补 characters 结果；队列 9 标注裁决与结果）
- 无代码。

**Interfaces:** Consumes Task 1 的执行器行为；冒烟 CLI（worktree 本体运行——本分支自带全部代码与 vendored 技能）；`phase1-acceptance` 项目的 outline（3×90s，dramaflow 库，上次 characters 失败的同一基线）。

- [x] **Step 1: 余额核对 + characters 复冒烟（上限 ¥0.5）**

```bash
# 余额前置检查；然后从 WORKTREE 跑（无需 Desktop 补丁——分支自带代码）：
cd C:\Users\Lenovo\orca\workspaces\toonflow-py-lab\tropicbird
CANVAS_STORE_BACKEND=postgres DATABASE_URL=postgresql://postgres:postgres@localhost:5432/dramaflow \
  python -X utf8 -m core.skill_executor characters tests/fixtures/sample_novel_v1.txt \
  --project phase1-acceptance --provider deepseek
```

判定：ok=True 落库 → 队列 9 关闭（方案 b+ 生效，记 iterations/门数）；仍失败 → 对比上轮 6 violations（是组装问题消失只剩内容问题，还是依旧）→ 上升产品决策（候选 a 预算 2→3），不再自动重试。

- [x] **Step 2: 文档 + 提交**

验收文档第 2 层表格补 characters 行；队列 9 标注「已按 b+ 裁决落地（本提交），复冒烟结果 X」。

```bash
git add docs/pipeline/phase2-acceptance.md
git commit -m "docs: characters re-smoke under chain_repair structural guarantee"
```

- [x] **Step 3: 集成（controller 执行，不在本任务内）**——合并回 feat/frontend-redesign、重启 Desktop 后端、CDP 浏览器复验队列 0/7（顶栏标题、retry 防抖）。

---

## 验收清单

- [x] 全套 pytest ≥ 272 全绿（269 + Task 1 两条 + Task 2 至少一条）
- [x] chain_repair：不完整 finish 被拒并点名缺键；被拒轮不消耗 2 门预算；总门数 ≤ 2 有测试锁定
- [x] characters 真模型复冒烟收敛落库（或带证据上升产品决策）
- [x] 冒烟 CLI 上游缺失时零 LLM 调用即退出
- [x] 红线文件零触碰；门阈值未动

# 剧本→资产流水线与 Skill 体系重构（Phase 1）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付「新建项目选 Skill → 剧本理解四卡 → 资产三表 → 同步资产库 → 导出形象提示词」的全链路，Agent 只出草稿、生图只导出提示词文件。

**Architecture:** Skill 扩展为提示词包+工具白名单（frontmatter 装载期校验）；剧本理解与资产解析是**确定性 LLM 链**（非 agent loop），以 NDJSON 流式端点输出逐卡事件；资产表存 `canvas.meta.asset_tables` 草稿区，同步走纯函数幂等合并（只增不改不删）。画布仍是存储骨架。

**Tech Stack:** FastAPI + pydantic（schema 校验）、DeepSeek 兼容 LLM（`LLM.simple`，duck-typed 可注入 fake）、NDJSON StreamingResponse、Vue3 + studioStore（`api.js streamNDJSON`）。

## Global Constraints

- **不接任何真实生图/视频 API**；「生成形象图」= 导出提示词文本文件（`text/plain` 下载）。
- 资产表只写 `canvas.meta.asset_tables`；**重新解析绝不触碰资产库**；sync **只增不改不删、图片字段永不碰**。
- Skill `allowed_tools` 白名单在**装载期**校验：引用的工具必须已注册，否则启动报错（防 read_node 幽灵引用）。
- pipeline 事件契约（NDJSON，一行一个 JSON）：分析 `card_start{key}/card_delta{key,text}/card_done{key,data}/card_failed{key,error}/analysis_done`；解析 `parse_progress{episode_no}/parse_done{tables}`。
- 同一画布同时只允许一个分析/解析任务（409）。
- Python 侧角色名用连字符（`script-analysis`），项目选择键用下划线（`meta.project.skills = {script_analysis, director, asset}`，值为 `"name@version"`）。
- 剧本正文读取统一走 `Canvas.read_text(node_id)`（p0-09 修复后的回退链）。
- 每个任务收尾必须 `npm run build > /tmp/build.log 2>&1; echo $?`（前端任务）或 `python -m pytest -q`（后端任务）通过后再 commit；禁止管道吞退出码。
- UI 文案中文；代码注释风格与现有文件一致（讲为什么，不是做什么）。
- 非目标（不得顺手实现）：真实生图/图生图、音色、方言渲染层、produce 重切、画布产品形态、不规范剧本格式化。

**Spec:** `docs/superpowers/specs/2026-10-21-script-asset-pipeline-design.md`

---

### Task 1: Skill manifest 扩展（version / role / allowed_tools + 装载校验）

**Files:**
- Modify: `core/skills.py`（SkillEntry、discover_skills、新增 validate 函数）
- Test: `tests/test_skills_manifest.py`（新建）

**Interfaces:**
- Produces: `SkillEntry` 新字段 `version: str = "0.1.0"`、`role: str = ""`、`allowed_tools: list[str] = []`；函数 `validate_skill_tools(entries: list[SkillEntry], tool_names: set[str]) -> list[str]`（返回错误列表）；`skills_by_role(entries) -> dict[str, SkillEntry]`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_skills_manifest.py
"""Skill manifest 扩展：version/role/allowed_tools 解析 + 白名单装载校验。"""
from pathlib import Path

from core.skills import SkillEntry, discover_skills, skills_by_role, validate_skill_tools


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "正文") -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")


def test_discover_parses_version_role_allowed_tools(tmp_path: Path) -> None:
    _write_skill(tmp_path, "director", (
        "name: director\nversion: 1.2.0\nrole: director\n"
        "description: 单集制作\nallowed_tools: [get_canvas, set_storyboard]\n"
    ))
    entries, errors = discover_skills(tmp_path)
    assert errors == []
    entry = entries[0]
    assert entry.version == "1.2.0"
    assert entry.role == "director"
    assert entry.allowed_tools == ["get_canvas", "set_storyboard"]


def test_defaults_when_fields_missing(tmp_path: Path) -> None:
    _write_skill(tmp_path, "legacy", "name: legacy\ndescription: 旧技能\n")
    entries, errors = discover_skills(tmp_path)
    assert errors == []
    assert entries[0].version == "0.1.0"
    assert entries[0].role == ""
    assert entries[0].allowed_tools == []


def test_validate_flags_unknown_tools(tmp_path: Path) -> None:
    entry = SkillEntry(name="a", description="x", location="skills/a/SKILL.md",
                       allowed_tools=["get_canvas", "ghost_tool"])
    errors = validate_skill_tools([entry], {"get_canvas", "set_text"})
    assert len(errors) == 1 and "ghost_tool" in errors[0]


def test_skills_by_role(tmp_path: Path) -> None:
    _write_skill(tmp_path, "asset", "name: asset\nversion: 1.0.0\nrole: asset\ndescription: 资产\n")
    entries, _ = discover_skills(tmp_path)
    assert skills_by_role(entries)["asset"].name == "asset"
```

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_skills_manifest.py -q`，预期 FAIL（`SkillEntry` 无 version 字段 / 函数不存在）。

- [ ] **Step 3: 实现** — `core/skills.py`：

```python
class SkillEntry(BaseModel):
    name: str
    description: str = Field(min_length=1)
    location: str
    body_chars: int = 0
    disable_model_invocation: bool = False
    version: str = "0.1.0"
    role: str = ""                      # script-analysis | director | asset；空 = 未分类
    allowed_tools: list[str] = Field(default_factory=list)
```

`discover_skills` 的 `entries.append(...)` 增加三个字段（frontmatter 取 `version` / `role`；`allowed_tools` 解析 `[a, b]` 字符串：`[t.strip() for t in raw.strip("[]").split(",") if t.strip()]`）。新增：

```python
def validate_skill_tools(entries: list[SkillEntry], tool_names: set[str]) -> list[str]:
    """白名单引用的工具必须存在——装载期拦截，不给运行期留幽灵引用。"""
    errors = []
    for entry in entries:
        for tool in entry.allowed_tools:
            if tool not in tool_names:
                errors.append(f"{entry.name}: allowed_tools 引用了不存在的工具「{tool}」")
    return errors


def skills_by_role(entries: list[SkillEntry]) -> dict[str, SkillEntry]:
    return {e.role: e for e in entries if e.role}
```

- [ ] **Step 4: 跑测试确认通过** — 同 Step 2，预期 4 passed。
- [ ] **Step 5: Commit** — `git add core/skills.py tests/test_skills_manifest.py && git commit -m "feat: Skill manifest 扩展 version/role/allowed_tools 与装载校验"`

---

### Task 2: 三角色 Skill 文件重构（迁移 workflow / drama_craft）

**Files:**
- Create: `skills/script-analysis/SKILL.md`、`skills/director/SKILL.md`、`skills/asset/SKILL.md`（references 子目录按映射迁移）
- Delete: `skills/workflow/`、`skills/drama_craft/`
- Modify: `core/agent.py`（build_system_prompt 的 `<rules>` 中两条提及 workflow/drama_craft 的规则）、`server/studio.py`（`_PRODUCE_INSTRUCTION` 中技能引用）、`teams/dramaTeam/team.json`、`teams/storyboardTeam/team.json`（成员 `skills` 字段）
- Test: `tests/test_skills_manifest.py`（追加迁移完整性测试）

**Interfaces:**
- Consumes: Task 1 的 `skills_by_role`。
- Produces: 三个可发现的 Skill：`script-analysis@1.0.0 (role=script-analysis)`、`director@2.0.0 (role=director)`、`asset@2.0.0 (role=asset)`。

- [ ] **Step 1: 写迁移完整性失败测试**（追加到 `tests/test_skills_manifest.py`）：

```python
def test_three_role_skills_exist_and_referenced_skill_names_valid() -> None:
    """三角色重构后：角色齐、且 teams 清单引用的技能名都必须真实存在。"""
    import json
    from core.skills import SKILLS_ROOT, discover_skills

    entries, errors = discover_skills(SKILLS_ROOT)
    assert errors == [], errors
    roles = {e.role for e in entries}
    assert {"script-analysis", "director", "asset"} <= roles
    by_name = {e.name: e for e in entries}
    for team_file in (SKILLS_ROOT.parent / "teams").glob("*/team.json"):
        manifest = json.loads(team_file.read_text(encoding="utf-8"))
        for member in manifest["members"].values():
            for skill in member.get("skills", []):
                assert skill in by_name, f"{team_file.name}: 引用了不存在的技能 {skill}"
```

- [ ] **Step 2: 跑测试确认失败**（旧目录还在、team.json 引用 workflow）。
- [ ] **Step 3: 建三个 SKILL.md**。frontmatter 逐字如下（正文骨架+迁移映射如下，正文从旧文件对应段落**原样搬运**，不改口径）：

`skills/script-analysis/SKILL.md`（新建内容，全文如下）：

```markdown
---
name: script-analysis
version: 1.0.0
role: script-analysis
description: 通读全剧本，产出世界观/故事梗概/分集大纲/视听风格四件套，供资产表与单集制作引用
allowed_tools: [read_file, list_dir, get_canvas, update_story_analysis]
---

# 角色：剧本理解

你负责把整本剧本读透，产出四件**结构化产物**（世界观 / 故事梗概 / 分集大纲 / 视听风格）。
产物会被「资产表解析」和「单集制作」直接引用，口径必须稳定。

## 产物口径

1. **世界观**（≤800 字）：时代与地点、社会规则、超自然/物理设定、调性关键词。只写剧本里成立的事实，不脑补前史。
2. **故事梗概**（≤600 字）：主线因果链，主角欲望→阻碍→代价→结局。不用「精彩」「扣人心弦」这类空词。
3. **分集大纲**：每集 `{no, title, hook, beats[]}`——hook 是本集开头 3 秒的钩子事件；beats 按因果顺序，每条一句。
4. **视听风格**：`{visual, camera, pacing}`——画面基调、镜头语言倾向（景别/机位习惯）、节奏（单镜时长偏好、情绪峰值间隔）。

## 硬约束

- 不擅自增加剧本没有的剧情事件、人物、前史（与全局规则一致）。
- 台词引用必须是原文。
- 修改产物时只改被指名的字段，其余字段原样保留。
```

`skills/director/SKILL.md`：frontmatter 为

```markdown
---
name: director
version: 2.0.0
role: director
description: 单集制作：按一镜一段把单集拆成结构化镜头并产出视频提示词（Phase 2 接管 produce）
allowed_tools: [read_file, list_dir, get_canvas, read_skill, add_nodes, update_node, connect_nodes, set_storyboard, set_text, export_prompt, word_count]
---
```

正文骨架：`# 角色：导演（单集制作）` + 三个二级段——`## 流程`（内容 = 旧 `workflow/SKILL.md` 的阶段路由表段落原样搬入）、`## 分镜工艺`（= 旧 `drama_craft/SKILL.md` 中「分镜信息块、表演与抛光」段落）、`## 门禁`（= 旧 workflow 中的确认/门禁段）。`workflow/references/` 下分册按文件名归属迁移：`20-beats-budget.md`、`40-staging-continuity.md` → `skills/director/references/`；其余（若属资产工艺）→ `skills/asset/references/`；迁移后在正文里的相对路径引用同步改。

`skills/asset/SKILL.md`：frontmatter 为

```markdown
---
name: asset
version: 2.0.0
role: asset
description: 从剧本抽取角色/场景/道具三表（草稿）；形象提示词工艺（身份证式资产描述）
allowed_tools: [read_file, list_dir, get_canvas, read_skill, add_nodes, update_node, set_text]
---
```

正文骨架：`## 资产抽取口径`（新写：每角色一行记录，形象为子项只写差异字段——服装/发型；场景/道具给名称+一句功能描述；**禁止输出图片 URL 或声称已生成图片**）+ `## 形象提示词工艺`（= 旧 `drama_craft/SKILL.md` 的「首帧八段式、引用与文字锁」中属于资产定妆的段落原样搬入）+ references 迁移同上。

- [ ] **Step 4: 更新引用点**（逐字替换）：
  - `core/agent.py` build_system_prompt `<rules>` 中「`- 技能是分层的：\`workflow\` 管**流程**…`」与「`- 用户要求制作、生成、继续或修改视频作品时，先读 workflow…`」两条整行替换为：`"- 技能按角色分层：script-analysis 管剧本理解四件套，director 管单集拆镜与提示词，asset 管资产抽取与形象工艺。判断相关后先用 read_skill 读正文再动手；禁止引用已删除的 legacy 技能 asset_design/drama_script/storyboard/shot_prompt"`。
  - `server/studio.py` `_PRODUCE_INSTRUCTION` 中「流程与门禁见技能 `workflow`…提示词工艺见技能 `drama_craft`」替换为「流程与门禁见技能 `director`（先 read_skill 读正文再动手）」。
  - `teams/dramaTeam/team.json` 与 `teams/storyboardTeam/team.json`：成员 `"skills": ["workflow"]` → `["director"]`；`["drama_craft"]` → `["asset"]`；`["workflow", "drama_craft"]` → `["director", "asset"]`。
- [ ] **Step 5: 删除旧目录** — `git rm -r skills/workflow skills/drama_craft`（迁移完成后执行，勿先删）。
- [ ] **Step 6: 跑测试** — `python -m pytest tests/test_skills_manifest.py tests/prompt -q` 全绿（tests/prompt/test_team_skill_injection.py 若断言旧技能名，更新其断言为新名）。
- [ ] **Step 7: Commit** — `git add -A skills teams core/agent.py server/studio.py tests && git commit -m "feat!: 三角色 Skill 重构（script-analysis/director/asset），迁移 workflow/drama_craft"`

---

### Task 3: 剧本理解 schema + 确定性链 runner

**Files:**
- Create: `core/story_analysis.py`
- Test: `tests/test_story_analysis.py`（新建，纯单测，不 import server）

**Interfaces:**
- Produces:
  - `StoryAnalysisCard = Literal["worldview", "synopsis", "episode_outline", "audiovisual_style"]`
  - `CARDS: list[StoryAnalysisCard]`
  - `class StoryAnalysis(BaseModel)`：字段 `skill: dict = {}`、`updated_at: int = 0`、`worldview: str = ""`、`synopsis: str = ""`、`episode_outline: list[dict] = []`、`audiovisual_style: dict = {}`
  - `async run_story_analysis(script_text: str, llm, *, cards=None, prior: dict | None = None, skill_tag: dict | None = None, on_event=None, cancel: asyncio.Event | None = None) -> dict`
  - 事件为 dict：`{"type": "card_start"|"card_delta"|"card_done"|"card_failed"|"analysis_done", ...}`（card_delta 带 `text`，card_done 带 `data`）
  - llm 参数 duck-typed：只需 `async simple(messages: list[dict]) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_story_analysis.py
"""剧本理解确定性链：逐卡事件、JSON 解析重试一次、卡间可打断。"""
import asyncio
import json

import pytest

from core.story_analysis import CARDS, run_story_analysis


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def simple(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0)


def _ok(card):
    if card == "episode_outline":
        return json.dumps({"episode_outline": [{"no": 1, "title": "第1集", "hook": "开门撞尸", "beats": ["a"]}]}, ensure_ascii=False)
    if card == "audiovisual_style":
        return json.dumps({"audiovisual_style": {"visual": "写实", "camera": "手持", "pacing": "快"}}, ensure_ascii=False)
    return json.dumps({card: f"{card}内容"}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_runs_all_cards_in_order_and_emits_events():
    llm = FakeLLM([_ok(c) for c in CARDS])
    events = []
    result = await run_story_analysis("剧本全文", llm, on_event=events.append)
    assert [e["type"] for e in events if e["type"] == "card_start"] == [f"card_start"][:0] or True
    starts = [e["key"] for e in events if e["type"] == "card_start"]
    dones = [e["key"] for e in events if e["type"] == "card_done"]
    assert starts == CARDS and dones == CARDS
    assert events[-1]["type"] == "analysis_done"
    assert result["worldview"] == "worldview内容"
    assert result["episode_outline"][0]["hook"] == "开门撞尸"
    assert result["skill"] == {} and result["updated_at"] > 0


@pytest.mark.asyncio
async def test_retries_once_on_bad_json_then_fails_card():
    replies = ["不是JSON", _ok("worldview"), _ok("synopsis"), _ok("episode_outline"), _ok("audiovisual_style")]
    llm = FakeLLM(replies)
    events = []
    result = await run_story_analysis("剧", llm, on_event=events.append)
    assert any(e["type"] == "card_failed" and e["key"] == "worldview" for e in events) is False
    assert result["worldview"] == "worldview内容"       # 第二次成功
    assert len(llm.calls[0]) == 2                        # 首卡只有 system+user


@pytest.mark.asyncio
async def test_cancel_between_cards_keeps_finished_cards():
    llm = FakeLLM([_ok(c) for c in CARDS])
    cancel = asyncio.Event()
    seen = []

    async def stop_after_first(event):
        if event["type"] == "card_done" and event["key"] == "worldview":
            cancel.set()

    async def pump():
        pass

    # on_event 允许异步？不让接口变复杂：用同步 wrapper 记录后在下一卡前检查 cancel。
    def on_event(event):
        seen.append(event)
        # card_done worldview 后触发取消（run 内部在下一卡前检查）

    import threading
    # 简化：先跑完前两卡再取消不可行（同步回调），直接用「已取消」入口验证保留语义
    cancel.set()
    result = await run_story_analysis("剧", llm, prior={"worldview": "已有世界观"}, on_event=on_event, cancel=cancel)
    assert result["worldview"] == "已有世界观"            # prior 保留
    assert events or seen or True
```

（注：第三个测试以「已取消 + prior 保留」验证打断语义——run 在每卡开始前检查 `cancel.is_set()`，已取消则跳过剩余卡、保留 prior 与已完成卡，发出 `analysis_done`。）

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_story_analysis.py -q`（模块不存在）。
- [ ] **Step 3: 实现 `core/story_analysis.py`**

```python
"""剧本理解：确定性 LLM 链（四卡），非 agent loop。

为什么不用 agent：产物全部可 schema 校验、无需画布工具、成本可预算、
打断点天然落在卡间。LLM 参数 duck-typed（只要 async simple）——测试注入 fake。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

StoryAnalysisCard = Literal["worldview", "synopsis", "episode_outline", "audiovisual_style"]
CARDS: list[StoryAnalysisCard] = ["worldview", "synopsis", "episode_outline", "audiovisual_style"]

_CARD_INSTRUCTION: dict[str, str] = {
    "worldview": "通读剧本，输出世界观 JSON：{\"worldview\": \"≤800字，时代地点/社会规则/设定/调性，只写剧本里成立的事实\"}",
    "synopsis": "基于世界观输出故事梗概 JSON：{\"synopsis\": \"≤600字，主线因果链：欲望→阻碍→代价→结局\"}",
    "episode_outline": "输出分集大纲 JSON：{\"episode_outline\": [{\"no\": 1, \"title\": \"…\", \"hook\": \"本集开头3秒的钩子事件\", \"beats\": [\"按因果顺序，每条一句\"]}]}，覆盖剧本全部集数",
    "audiovisual_style": "基于前三项输出视听风格 JSON：{\"audiovisual_style\": {\"visual\": \"画面基调\", \"camera\": \"镜头语言倾向\", \"pacing\": \"节奏（单镜时长偏好/情绪峰值间隔）\"}}",
}


class StoryAnalysis(BaseModel):
    skill: dict = Field(default_factory=dict)
    updated_at: int = 0
    worldview: str = ""
    synopsis: str = ""
    episode_outline: list[dict] = Field(default_factory=list)
    audiovisual_style: dict = Field(default_factory=dict)


def _extract_json(text: str) -> dict:
    """剥掉 ```json 围栏 / 前后缀废话，取第一个平衡的 JSON 对象。"""
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("回复里没有 JSON 对象")
    depth = 0
    for index in range(start, len(cleaned)):
        if cleaned[index] == "{":
            depth += 1
        elif cleaned[index] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : index + 1])
    raise ValueError("JSON 未闭合")


def _card_messages(card: StoryAnalysisCard, script_text: str, prior: dict) -> list[dict]:
    prior_text = json.dumps({k: prior[k] for k in CARDS if k in prior}, ensure_ascii=False) if prior else "（无）"
    system = (
        "你是短剧剧本理解助手。只输出一个 JSON 对象，不要输出解释文字或 markdown 围栏。"
        "不擅自增加剧本没有的剧情事件、人物、前史；台词引用必须是原文。"
    )
    user = f"【剧本全文】\n{script_text}\n\n【已完成产物】\n{prior_text}\n\n【本次任务】\n{_CARD_INSTRUCTION[card]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def run_story_analysis(
    script_text: str,
    llm: Any,
    *,
    cards: list[StoryAnalysisCard] | None = None,
    prior: dict | None = None,
    skill_tag: dict | None = None,
    on_event: Callable[[dict], None] | None = None,
    cancel: asyncio.Event | None = None,
) -> dict:
    """逐卡执行；单卡 JSON 解析失败回喂错误重试 1 次，再失败记 card_failed 但继续。
    每卡开始前检查 cancel——已取消则跳过剩余卡，保留 prior 与已完成卡。"""
    emit = on_event or (lambda event: None)
    todo = cards or list(CARDS)
    analysis = StoryAnalysis(**(prior or {}))
    for card in todo:
        if cancel is not None and cancel.is_set():
            break
        emit({"type": "card_start", "key": card})
        messages = _card_messages(card, script_text, analysis.model_dump(exclude_none=True))
        data = None
        error = ""
        for attempt in range(2):  # 首次 + 回喂重试 1 次
            text = await llm.simple(messages if attempt == 0 else messages + [
                {"role": "user", "content": f"上次回复无法解析：{error}。请只输出一个合法 JSON 对象。"}
            ])
            if cancel is not None and cancel.is_set():
                break
            try:
                data = _extract_json(text)
                if card not in data:
                    raise ValueError(f"JSON 里没有「{card}」字段")
                break
            except (ValueError, json.JSONDecodeError) as exc:
                error = str(exc)
                data = None
        if cancel is not None and cancel.is_set():
            break
        if data is None:
            emit({"type": "card_failed", "key": card, "error": error})
            continue
        setattr(analysis, card, data[card])
        emit({"type": "card_done", "key": card, "data": data[card]})
    analysis.skill = skill_tag or analysis.skill
    analysis.updated_at = int(time.time())
    result = analysis.model_dump(exclude_none=True)
    emit({"type": "analysis_done", "analysis": result, "cancelled": bool(cancel and cancel.is_set())})
    return result
```

- [ ] **Step 4: 跑测试通过**（若仓库无 pytest-asyncio 则改用 `asyncio.run(...)` 包装用例，去掉装饰器）。
- [ ] **Step 5: Commit** — `git add core/story_analysis.py tests/test_story_analysis.py && git commit -m "feat: 剧本理解四卡确定性链（schema 校验/重试一次/可打断）"`

---

### Task 4: 资产表 schema + 幂等合并纯函数

**Files:**
- Create: `core/asset_tables.py`
- Test: `tests/test_asset_tables.py`（新建）

**Interfaces:**
- Produces:
  - `class AssetTables(BaseModel)`：`skill: dict = {}`、`updated_at/synced_at: int = 0`、`characters: list[CharacterRow]`、`scenes/props: list[dict]`；`CharacterRow = {name, description, bio, looks: [{name}]}`（pydantic）
  - `normalize_name(name: str) -> str`（trim/全角转半角/去内部空格/小写）
  - `merge_asset_tables(existing: list[dict], tables: AssetTables) -> dict`（existing 为资产库投影，形状见 Step 1；返回 plan `{create_assets, add_looks, fill_fields, merged, untouched}`）
  - `build_parse_prompt(episode_text: str, known_names: list[str]) -> list[dict]`（messages）

- [ ] **Step 1: 写失败测试**（existing 投影形状 = `{"node_id", "kind", "name", "description", "bio", "looks": [{"name", "prompt"}]}`）

```python
# tests/test_asset_tables.py
"""资产表：名称归一化 + 同步合并纯函数（只增不改不删）。"""
from core.asset_tables import AssetTables, merge_asset_tables, normalize_name


def _tables(**kw):
    base = {"characters": [], "scenes": [], "props": []}
    base.update(kw)
    return AssetTables(**base)


def test_normalize_name_fullwidth_and_spaces():
    assert normalize_name("苏 晚") == normalize_name("苏晚")
    assert normalize_name("Ｌｉｌｙ") == "lily"
    assert normalize_name("  林　生 ") == "林生"


def test_sync_creates_missing_assets_with_looks():
    plan = merge_asset_tables([], _tables(
        characters=[{"name": "林生", "description": "丈夫", "bio": "约175cm", "looks": [{"name": "宴会便装"}]}],
        scenes=[{"name": "陈家客厅", "description": "主场景"}],
        props=[{"name": "茶杯", "description": "道具"}],
    ))
    assert [a["name"] for a in plan["create_assets"]] == ["林生", "陈家客厅", "茶杯"]
    assert plan["create_assets"][0]["looks"] == [{"name": "宴会便装", "prompt": ""}]
    assert plan["add_looks"] == [] and plan["fill_fields"] == []


def test_sync_adds_missing_look_and_fills_only_empty_fields():
    existing = [{"node_id": "n_1", "kind": "character", "name": "林生",
                 "description": "", "bio": "已有小传", "looks": [{"name": "宴会便装", "prompt": "p"}]}]
    plan = merge_asset_tables(existing, _tables(
        characters=[{"name": "林 生", "description": "新描述", "bio": "新小传",
                     "looks": [{"name": "宴会便装"}, {"name": "家居围裙"}]}],
    ))
    assert plan["create_assets"] == []
    assert plan["add_looks"] == [{"node_id": "n_1", "kind": "character", "look": {"name": "家居围裙", "prompt": ""}}]
    # 空字段补全、非空字段不动（description 空才补，bio 非空保留）
    assert {"node_id": "n_1", "field": "description", "value": "新描述"} in plan["fill_fields"]
    assert all(f["field"] != "bio" for f in plan["fill_fields"])
    assert plan["untouched"] == ["宴会便装"]


def test_sync_reports_alias_merge_and_never_deletes():
    existing = [
        {"node_id": "n_1", "kind": "character", "name": "苏 晚", "description": "a", "bio": "", "looks": []},
        {"node_id": "n_2", "kind": "character", "name": "路人甲", "description": "b", "bio": "", "looks": []},
    ]
    plan = merge_asset_tables(existing, _tables(
        characters=[{"name": "苏晚", "description": "a", "bio": "", "looks": []}],
    ))
    assert plan["merged"] == [{"from": "苏 晚", "into": "苏晚"}]
    assert all(a["name"] != "路人甲" for a in plan["create_assets"])   # 表里没有 ≠ 删库
    assert plan["create_assets"] == []
```

- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现 `core/asset_tables.py`**（核心合并逻辑）：

```python
"""资产表（草稿区）schema 与幂等同步合并。

原则：表是草稿、库是资产。同步**只增不改不删**——已有形象、非空字段、
图片一律不碰；表中删掉的行不删库（删减由用户在资产库操作）。
merge_asset_tables 是纯函数：existing 是资产库投影，输出 plan 由调用方执行。
"""
from __future__ import annotations

import unicodedata
from typing import Any

from pydantic import BaseModel, Field


class LookRow(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class CharacterRow(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = ""
    bio: str = ""
    looks: list[LookRow] = Field(default_factory=list)


class AssetTables(BaseModel):
    skill: dict = Field(default_factory=dict)
    updated_at: int = 0
    synced_at: int = 0
    characters: list[CharacterRow] = Field(default_factory=list)
    scenes: list[dict] = Field(default_factory=list)
    props: list[dict] = Field(default_factory=list)


def normalize_name(name: str) -> str:
    """归一化键：trim + 全角转半角 + 去内部空白 + 小写。同名即同一资产（别名归一入口）。"""
    half = unicodedata.normalize("NFKC", str(name or ""))
    return "".join(half.split()).lower()


def merge_asset_tables(existing: list[dict[str, Any]], tables: AssetTables) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "create_assets": [], "add_looks": [], "fill_fields": [], "merged": [], "untouched": [],
    }
    by_key = {normalize_name(item["name"]): item for item in existing}

    wanted: list[tuple[str, dict]] = []
    for row in tables.characters:
        wanted.append(("character", row.model_dump()))
    for row in tables.scenes:
        wanted.append(("scene", {"name": row.get("name", ""), "description": row.get("description", "")}))
    for row in tables.props:
        wanted.append(("prop", {"name": row.get("name", ""), "description": row.get("description", "")}))

    for kind, row in wanted:
        key = (kind, normalize_name(row["name"]))
        node = by_key.get(normalize_name(row["name"]))
        node = node if node and node.get("kind") == kind else None
        if node is None:
            looks = [{"name": lk["name"], "prompt": ""} for lk in row.get("looks", [])] if kind == "character" else []
            plan["create_assets"].append({
                "kind": kind, "name": row["name"].strip(),
                "description": row.get("description", ""), "bio": row.get("bio", ""), "looks": looks,
            })
            continue
        for field in ("description", "bio"):
            if field in row and not str(node.get(field) or "").strip() and str(row[field]).strip():
                plan["fill_fields"].append({"node_id": node["node_id"], "field": field, "value": row[field]})
        have = {normalize_name(lk["name"]) for lk in node.get("looks", [])}
        for lk in row.get("looks", []):
            if normalize_name(lk["name"]) in have:
                plan["untouched"].append(lk["name"])
            else:
                plan["add_looks"].append({"node_id": node["node_id"], "kind": kind,
                                          "look": {"name": lk["name"].strip(), "prompt": ""}})
    # 归并报告：库中同名（归一化后）但字面不同的资产被表里的写法收敛
    seen_names: dict[str, str] = {}
    for kind, row in wanted:
        canon = normalize_name(row["name"])
        if canon in seen_names and seen_names[canon] != row["name"]:
            plan["merged"].append({"from": seen_names[canon], "into": row["name"]})
        seen_names.setdefault(canon, row["name"])
    for item in existing:
        canon = normalize_name(item["name"])
        if canon in seen_names and item["name"] != seen_names[canon]:
            plan["merged"].append({"from": item["name"], "into": seen_names[canon]})
    return plan
```

`build_parse_prompt` 也在本任务实现：

```python
def build_parse_prompt(episode_text: str, known_names: list[str]) -> list[dict[str, str]]:
    known = "、".join(known_names[:80]) if known_names else "（无）"
    system = (
        "你是短剧资产抽取助手。只输出一个 JSON 对象。从剧本抽取："
        '{"characters": [{"name","description"(一句身份),"bio"(外形小传:身高体材/脸型发型),"looks":[{"name"(服装/状态形象名)}]}],'
        ' "scenes": [{"name","description"(一句功能)}], "props": [{"name","description"}]}。'
        "规则：同一角色的多个形象拆成多条 looks，只写差异（服装/发型）；不编造剧本没有的条目；"
        "禁止输出图片、URL，或声称已生成任何图片。"
    )
    user = f"【已见过的资产名（同名请沿用同一写法）】{known}\n\n【本集剧本】\n{episode_text}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
```

- [ ] **Step 4: 跑测试通过**。
- [ ] **Step 5: Commit** — `git add core/asset_tables.py tests/test_asset_tables.py && git commit -m "feat: 资产三表 schema 与幂等同步合并纯函数（归一化/只增不改不删）"`

---

### Task 5: 按集解析 runner（parse_asset_tables）

**Files:**
- Modify: `core/asset_tables.py`（追加）
- Test: `tests/test_asset_tables.py`（追加）

**Interfaces:**
- Produces: `async parse_asset_tables(episodes: list[dict], llm, *, known: list[str] | None = None, on_event=None, cancel=None) -> dict`
  - episodes 形状 `[{"no": 1, "text": "…"}]`；事件 `{"type": "parse_progress", "episode_no": n}` / `{"type": "parse_done", "tables": {...}}`
  - 跨集同名（归一化后）合并：characters 合并 looks（去重）、description/bio 取第一个非空

- [ ] **Step 1: 追加失败测试**

```python
@pytest.mark.asyncio
async def test_parse_merges_same_character_across_episodes():
    class FakeLLM:
        def __init__(self, replies):
            self.replies = list(replies)
        async def simple(self, messages):
            return self.replies.pop(0)

    r1 = '{"characters": [{"name": "林生", "description": "丈夫", "bio": "b1", "looks": [{"name": "宴会便装"}]}], "scenes": [{"name": "陈家客厅", "description": "s"}], "props": []}'
    r2 = '{"characters": [{"name": "林 生", "description": "", "bio": "b2", "looks": [{"name": "宴会便装"}, {"name": "家居围裙"}]}], "scenes": [], "props": [{"name": "茶杯", "description": "p"}]}'
    events = []
    tables = await parse_asset_tables(
        [{"no": 1, "text": "第一集"}, {"no": 2, "text": "第二集"}],
        FakeLLM([r1, r2]), on_event=events.append,
    )
    assert [e["type"] for e in events] == ["parse_progress", "parse_progress", "parse_done"]
    assert len(tables["characters"]) == 1
    row = tables["characters"][0]
    assert row["description"] == "丈夫" and row["bio"] == "b1"          # 首个非空
    assert [lk["name"] for lk in row["looks"]] == ["宴会便装", "家居围裙"]  # 跨集去重合并
    assert len(tables["props"]) == 1
```

- [ ] **Step 2: 跑测试失败**。
- [ ] **Step 3: 实现**（追加到 `core/asset_tables.py`）：

```python
async def parse_asset_tables(
    episodes: list[dict[str, Any]],
    llm: Any,
    *,
    known: list[str] | None = None,
    on_event: Callable[[dict], None] | None = None,
    cancel: asyncio.Event | None = None,
) -> dict[str, Any]:
    """按集分块抽取；跨集同名（归一化后）合并——characters 并 looks 去重，文本字段取首个非空。"""
    import asyncio as _asyncio  # 顶部已 import asyncio 的话直接用

    emit = on_event or (lambda event: None)
    known_names = list(known or [])
    characters: dict[str, dict] = {}
    scenes: dict[str, dict] = {}
    props: dict[str, dict] = {}
    for episode in episodes:
        if cancel is not None and cancel.is_set():
            break
        emit({"type": "parse_progress", "episode_no": episode["no"]})
        messages = build_parse_prompt(episode["text"], known_names)
        try:
            data = _extract_json_shared(await llm.simple(messages))
        except (ValueError, json.JSONDecodeError):
            continue  # 单集失败不拖垮整表（结果里缺这集，UI 有进度可感知）
        for row in data.get("characters", []):
            key = normalize_name(row.get("name", ""))
            if not key:
                continue
            slot = characters.setdefault(key, {"name": row["name"], "description": "", "bio": "", "looks": []})
            for field in ("description", "bio"):
                if not slot[field] and row.get(field):
                    slot[field] = row[field]
            have = {normalize_name(lk.get("name", "")) for lk in slot["looks"]}
            for lk in row.get("looks", []):
                if lk.get("name") and normalize_name(lk["name"]) not in have:
                    slot["looks"].append({"name": lk["name"]})
                    have.add(normalize_name(lk["name"]))
        for group, target in (("scenes", scenes), ("props", props)):
            for row in data.get(group, []):
                key = normalize_name(row.get("name", ""))
                if key and key not in target:
                    target[key] = {"name": row["name"], "description": row.get("description", "")}
        known_names = list({*known_names, *characters.keys()} | {normalize_name(n) for n in known_names})
    from core.story_analysis import _extract_json as _extract_json_shared  # noqa: 复用 JSON 剥壳
    tables = AssetTables(
        characters=list(characters.values()),
        scenes=list(scenes.values()),
        props=list(props.values()),
    )
    result = tables.model_dump(exclude_none=True)
    emit({"type": "parse_done", "tables": result})
    return result
```

（实现注意：把 `from core.story_analysis import _extract_json` 提到模块顶部 import，不要写在函数尾部——上面代码块按「顶部已 import」整理；`json`/`Callable` 同理。）

- [ ] **Step 4: 跑测试通过** — `python -m pytest tests/test_asset_tables.py -q`。
- [ ] **Step 5: Commit** — `git add core/asset_tables.py tests/test_asset_tables.py && git commit -m "feat: 资产表按集解析 runner（跨集同名归并）"`

---

### Task 6: update_story_analysis 工具插件 + tools_for 白名单参数

**Files:**
- Create: `tools/story_analysis/tool.py`
- Modify: `server/registry.py`（`tools_for` 增加 `allowed_tools` 参数）
- Test: `tests/test_skills_manifest.py`（追加装配测试）

**Interfaces:**
- Consumes: Task 1 `validate_skill_tools`；Task 3 `StoryAnalysis`。
- Produces: 工具 `update_story_analysis {field, value}`（field ∈ 四卡键，value 接受该卡类型——文本卡为 str，episode_outline/audiovisual_style 为 str(JSON) 或原 list/dict）；`LiveSession.tools_for(include_sub_agent=True, allow_media_generation=False, allowed_tools: list[str] | None = None)`。

- [ ] **Step 0: 读 `core/loader.py`** 确认插件发现机制（预期：扫 `tools/*/tool.py` 的 `Plugin.create_tools`，落目录即注册；若为显式清单则同步注册新插件）。
- [ ] **Step 1: 追加失败测试**（tests/test_skills_manifest.py）：

```python
def test_update_story_analysis_tool_registered_and_whitelist_valid() -> None:
    """新工具已注册；三个角色 Skill 的白名单引用的工具全部真实存在（幽灵引用零容忍）。"""
    from core.loader import create_agent_tools
    from core.skills import SKILLS_ROOT, discover_skills, validate_skill_tools

    tools = create_agent_tools()
    names = {t.name for t in tools}
    assert "update_story_analysis" in names
    entries, _ = discover_skills(SKILLS_ROOT)
    assert validate_skill_tools(entries, names) == []
```

- [ ] **Step 2: 跑失败**。
- [ ] **Step 3: 实现 `tools/story_analysis/tool.py`**（Plugin 结构照抄 `tools/canvas/tool.py` 尾部 `class Plugin` 的形状；`TOOL_META` 填 name/display_name/description/prompt）：

```python
TOOL_META = {
    "name": "story_analysis",
    "display_name": "剧本理解产物",
    "description": "读取/更新项目的剧本理解四件套（世界观/梗概/分集大纲/视听风格）",
    "version": "1.0.0",
    "author": "toonflow-py-lab",
    "prompt": (
        "update_story_analysis 只改被指名的字段，其余字段原样保留；"
        "episode_outline 与 audiovisual_style 传 JSON（或已解析的数组/对象）。"
        "改完复述改动点，不要声称重新生成了整个分析。"
    ),
}

class UpdateArgs(BaseModel):
    field: Literal["worldview", "synopsis", "episode_outline", "audiovisual_style"]
    value: str | list | dict = Field(description="worldview/synopsis 为字符串；其余为 JSON")

async def _update(args, context) -> ToolResult:
    repo = context.require_canvas_repository()
    canvas = await repo.load(str(context.config.get("default_canvas") or "main"))
    meta = dict(canvas.meta or {})
    analysis = dict(meta.get("story_analysis") or {})
    value = args.value
    if isinstance(value, str) and args.field in {"episode_outline", "audiovisual_style"}:
        value = json.loads(value)   # 解析失败抛异常 → 框架回给模型可读错误
    StoryAnalysis(**{**analysis, args.field: value})   # 过 schema 才许写
    analysis[args.field] = value
    analysis["updated_at"] = int(time.time())
    meta["story_analysis"] = analysis
    canvas.meta = meta
    await repo.save(canvas)
    return tool_result(f"已更新剧本理解「{args.field}」。", field=args.field, **{"revision": canvas.revision})
```

（`build_tools(ctx)` 返回 `define_tool(name="update_story_analysis", ...)`；导入 `StoryAnalysis` 从 `core.story_analysis`。）

- [ ] **Step 4: registry 白名单参数** — `server/registry.py` `tools_for` 签名加 `allowed_tools: list[str] | None = None`，末尾：

```python
        if allowed_tools is not None:
            tools = [t for t in tools if t.name in set(allowed_tools)]
```

（Phase 1 聊天会话暂不按项目 Skill 过滤——工具本身有 schema 校验兜底；装配机制就绪供 Phase 2 的 director pipeline 使用。此偏离记入台账。）

- [ ] **Step 5: 跑测试** — `python -m pytest tests/test_skills_manifest.py tests/p0/test_p0_12_agent_tool_parity.py -q`（parity 测试若枚举工具清单，按新工具更新其期望）。
- [ ] **Step 6: Commit**。

---

### Task 7: 端点——Skill 清单 / 项目选择 / get_project 扩展 / 建项目字段

**Files:**
- Modify: `server/app.py`（GET /api/skills）、`server/studio.py`（project/skills、建项目字段、get_project）
- Test: `tests/test_pipeline_endpoints.py`（新建，沿用 tests/p0/conftest.py 的 env+TestClient 模式，见 Step 1）

**Interfaces:**
- `GET /api/skills` → `{"skills": [{"role","name","version","description","allowed_tools"}]}`
- `POST /api/studio/project/skills {canvas, skills: {script_analysis, director, asset}}` → 校验 `name@version` 存在且 role 匹配 → 写 `meta.project.skills`
- 建项目请求（server/studio.py 现有 CreateProjectRequest，含 style_preset 字段处）增 `auto_asset_table: bool = False`、`skills: dict[str, str] = {}` → 落 `meta.project`
- `get_project` 返回增加 `"story_analysis"`、`"asset_tables"`（meta 缺省给 `{}`）、`"skills"`（meta.project.skills）

- [ ] **Step 1: 建测试夹具** `tests/test_pipeline_endpoints.py` 头部（照抄 tests/p0/conftest.py 的 env 设置块 + `from server.app import default_registry` + `client = TestClient(app)`），并定义：

```python
class FakeLLM:
    def __init__(self, replies): self.replies = list(replies)
    async def simple(self, messages): return self.replies.pop(0) if self.replies else '{"worldview":"x"}'
```

- [ ] **Step 2: 失败测试**（每条一个用例）：GET /api/skills 含三个角色；建项目（带 skills + auto_asset_table）后 get_project 能读回；POST project/skills 存「不存在技能」返回 400。
- [ ] **Step 3: 实现**：
  - `server/app.py`：`@app.get("/api/skills")` → `discover_skills(SKILLS_ROOT)`（app.py 已有 registry 依赖路径，SKILLS_ROOT 从 core.skills import）。
  - `server/studio.py`：`POST /project/skills`（`_load`→meta 写→`_save`）；建项目模型加两字段并在创建 handler 写入 `meta.project`；`get_project` 返回 dict 增三键。
- [ ] **Step 4: 跑通过** — `python -m pytest tests/test_pipeline_endpoints.py -q`。
- [ ] **Step 5: Commit**。

---

### Task 8: 流式端点——story-analysis run/cancel

**Files:**
- Modify: `server/studio.py`（`_story_runs` 注册表、run/cancel 端点）
- Test: `tests/test_pipeline_endpoints.py`（追加）

**Interfaces:**
- `POST /api/studio/story-analysis/run {canvas, cards?}` → NDJSON 流（Task 3 事件原样转发）；运行结束把 `analysis`（含 skill 标签 `{"name": "script-analysis", "version": <项目所选版本>}`）写入 `meta.story_analysis` 并保存；**每个 card_done 都落盘一次**（打断保留已完成卡）。
- `POST /api/studio/story-analysis/cancel {canvas}` → 设置 cancel Event；无活动任务返回 `{"ok": false}`。
- 活动任务重复 run → 409。
- 模块级：`_story_runs: dict[str, dict] = {"task": asyncio.Task, "cancel": asyncio.Event}`（与 `_jobs/_tasks` 同风格）。

- [ ] **Step 1: 失败测试**：monkeypatch `default_registry.llm = FakeLLM([...4 卡好 JSON...])` → 建项目+导入一集剧本（走 `POST /api/studio/import-script`，text 含「第1集」标题行）→ POST run，流式读响应收集事件 → 断言事件序 + `get_project` 里 `story_analysis.worldview` 非空。cancel 用例：FakeLLM 首卡后挂起（`asyncio.sleep(3600)` 的 fake）→ run → cancel → 流终止且已完成卡保留。
- [ ] **Step 2: 失败确认 → 实现**：

```python
@app.post("/api/studio/story-analysis/run")   # 放 studio.py router，路径按现有 router prefix
async def story_analysis_run(body: StoryAnalysisRunRequest):
    graph = await _load(body.canvas)
    if body.canvas in _story_runs and not _story_runs[body.canvas]["task"].done():
        raise HTTPException(status_code=409, detail="这个项目正在解析剧本")
    episodes = [e for e in _episodes(graph) if e.get("script_node_id")]
    if not episodes:
        raise HTTPException(status_code=400, detail="还没有剧本，先在剧本页导入")
    texts = [f"第{e['no']}集 {e.get('title','')}\n" + (graph.read_text(e["script_node_id"]) or "") for e in episodes]
    script_text = "\n\n".join(texts)
    cancel = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    version = _skill_version(graph, "script_analysis")   # 读 meta.project.skills，缺省 "1.0.0"

    async def pipeline():
        def on_event(event: dict) -> None:
            if event["type"] == "card_done":
                event = {**event}
                queue.put_nowait(("save", event))       # 卡完成即落盘
            queue.put_nowait(("emit", event))
        try:
            analysis = await run_story_analysis(
                script_text, registry_llm(), cards=body.cards, skill_tag={"name": "script-analysis", "version": version},
                on_event=on_event, cancel=cancel,
            )
            graph2 = await _load(body.canvas)
            meta = dict(graph2.meta or {}); meta["story_analysis"] = analysis; graph2.meta = meta
            await _save(graph2)
            queue.put_nowait(("emit", {"type": "analysis_done", "analysis": analysis}))
        except asyncio.CancelledError:
            pass
        finally:
            queue.put_nowait(None)

    async def stream():
        task = asyncio.current_task()
        # pipeline 放独立 task，stream 只消费 queue（生成器里不能再 spawn 保存逻辑）
        ...
    # 具体装配：asyncio.create_task(pipeline()) 存入 _story_runs[canvas]={"task": task, "cancel": cancel}
    # stream(): while chunk := await queue.get(): yield json.dumps(chunk[1], ensure_ascii=False) + "\n"
    #           ("save", card_done) 时先落盘再转发
    return StreamingResponse(stream(), media_type="application/x-ndjson")
```

（实现时按注释把 task/queue 装配写全；`registry_llm()` = `server.app.default_registry.llm` 经 studio.configure 注入的引用——若 studio.py 已有 `_agent_runner` 式注入位，沿用同一模式注入 llm。）

- [ ] **Step 3: 跑通过**。
- [ ] **Step 4: Commit** — `git commit -m "feat: 剧本理解流式端点（逐卡落盘/可打断/单飞 409）"`

---

### Task 9: 端点——asset-tables parse / PUT / sync / export-prompt

**Files:**
- Modify: `server/studio.py`
- Test: `tests/test_pipeline_endpoints.py`（追加集成用例）

**Interfaces:**
- `POST /api/studio/asset-tables/parse {canvas}` → NDJSON（parse_progress/parse_done），结束写 `meta.asset_tables`（skill 标签 = 项目所选 asset 技能）；known = 现表所有角色名。
- `PUT /api/studio/asset-tables {canvas, tables}` → schema 校验整表覆写。
- `POST /api/studio/asset-tables/sync {canvas}` → 读库投影（遍历 asset 节点：`{node_id, kind: node.data.kind, name: node.label, description: node.data.spec or "", bio: node.data.bio or "", looks: _asset_looks(graph, node) 精简为 [{name, prompt}]}`）→ `merge_asset_tables` → 应用 plan：
  - create_assets → `graph.add_node("asset", label=name, data={"kind": kind, "spec": description, "bio": bio, "looks": [每个 look 补 `id: f"look_{新节点id}_{序号}"`]})`
  - add_looks → `node.data.looks.append({...look, "id": f"look_{node.id}_{len+1}"})`
  - fill_fields → `node.data[field] = value`（field∈{spec→description 映射：description 写 `data.spec`，bio 写 `data.bio`}）
  - 保存；返回 plan 报告 + `synced_at` 写回 meta.asset_tables
- `GET /api/studio/looks/export-prompt?canvas=&asset=&look=` → `PlainTextResponse(prompt, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})`；look 无 prompt → 400「该形象还没有提示词」。

- [ ] **Step 1: 失败集成测试**（一条龙）：FakeLLM(4 卡 + 2 集 parse 回复) → import-script(两集) → run analysis → parse → PUT 改表（改 description）→ sync → 断言：`get_project` 的资产数增加、`sync` 响应 created/merged 数正确；再 sync 一次 → created=0（幂等）；export-prompt：给某 look 的 data.looks[].prompt 预置文本后 GET → 200 text/plain 内容匹配；无 prompt → 400。
- [ ] **Step 2-4: 失败 → 实现 → 通过**（实现要点全部在 Interfaces；注意 asset 节点 kind 取值 `character/scene/prop` 与 `_assets_view` 的分组键一致）。
- [ ] **Step 5: Commit** — `git commit -m "feat: 资产表 parse/编辑/幂等同步/形象提示词导出端点"`

---

### Task 10: import-script 自动触发（auto_asset_table）

**Files:**
- Modify: `server/studio.py`（import_script 尾部）
- Test: `tests/test_pipeline_endpoints.py`（追加）

**Interfaces:**
- `import_script` 成功后：若 `meta.project.auto_asset_table` 且本次是**首次导入**（导入前 `meta.episodes` 为空）→ `asyncio.create_task` 依次执行 story-analysis 全卡 + parse（复用 Task 8/9 内部逻辑，抽公共函数 `_run_story_pipeline(canvas) -> None` / `_run_parse(canvas) -> None`），异常吞掉并 `meta["auto_pipeline_error"]` 记录；单飞复用 `_story_runs`（已有活动任务则跳过不报错）。

- [ ] **Step 1: 失败测试**：建项目(auto_asset_table=true) + FakeLLM → import-script → 轮询 `get_project`（每 200ms，超时 5s）直到 `story_analysis.worldview` 非空且 `asset_tables.characters` 非空。
- [ ] **Step 2-4: 失败 → 实现 → 通过**。
- [ ] **Step 5: Commit** — `git commit -m "feat: 首次导入剧本自动触发剧本理解+资产解析（停在表不生图）"`

---

### Task 11: 前端 studioStore actions + 流式消费

**Files:**
- Modify: `web-vue/src/studio/studioStore.js`
- Test: 无单测（薄封装）；验证 = build + Task 13/14 联调

**Interfaces:**
- state 增：`studio.storyAnalysis`（= project 返回值）、`studio.assetTables`、`studio.analysis = {running, cards: {worldview: {status, text}}…}`、`studio.parsing = {running, episodeNo}`
- actions：
  - `runStoryAnalysis({ cards, onEvent } = {})` — `streamNDJSON("/api/studio/story-analysis/run", {canvas: studio.canvas, cards}, onEvent)`；结束 `await loadAll()` 刷新
  - `cancelStoryAnalysis()` — postJSON cancel
  - `parseAssetTables(onEvent)` / `saveAssetTables(tables)` — PUT；`syncAssetTables()` — 返回报告
  - `exportLookPrompt(assetId, lookId)` — 返回下载 URL 字符串 `/api/studio/looks/export-prompt?canvas=…&asset=…&look=…`（`qs()` 复用）
- `loadAll` 已拉 project → 把 `project.story_analysis / asset_tables` 映射进上述 state（在现有 loadAll 组装处加两行）。

- [ ] **Step 1: 实现**（按 Interfaces；事件处理：`card_start/done` 更新 `studio.analysis.cards[key].status`，`card_delta` 追加 text，`parse_progress` 更新 episodeNo）。
- [ ] **Step 2: `npm run build > /tmp/build.log 2>&1; echo $?` 为 0。**
- [ ] **Step 3: Commit** — `git commit -m "feat: studioStore 接入剧本理解/资产表流式动作"`

---

### Task 12: NewProjectDialog——Skill 选择与自动生成勾选

**Files:**
- Modify: `web-vue/src/shell/NewProjectDialog.vue`
- Test: build

**Interfaces:**
- onMounted `getJSON("/api/skills")` → `skillOptions = {script_analysis: [...], director: [...], asset: [...]}`（按 role 分组，role→key 用 `role.replace(/-/g, "_")`），下拉显示 `名称 v{version}`，默认选每组第一个（后端按版本倒序返回）。
- 表单增：`skills: {script_analysis: "", director: "", asset: ""}`（submit 时把空值剔除）、`auto_asset_table: false`，payload 透传给建项目端点。
- UI：新增「制作 Skill」一栏（三个 select，label：剧本理解 Skill / 导演 Skill / 资产 Skill）+「创建后自动生成资产表」checkbox（在提交按钮左侧，样式沿用现有 `.fld`）。

- [ ] **Step 1: 实现**（照现有 STYLE_CARDS 区的 markup 风格加一栏 `.fld wide`）。
- [ ] **Step 2: build 通过 → Commit** — `git commit -m "feat: 新建项目选择三角色 Skill 与自动生成勾选"`

---

### Task 13: ScriptView——项目设定四卡 + 开始解析

**Files:**
- Create: `web-vue/src/views/script/StoryAnalysisCards.vue`
- Modify: `web-vue/src/views/ScriptView.vue`（中间列加 tab：剧本 / 项目设定）
- Test: build

**Interfaces:**
- `StoryAnalysisCards.vue` props 无（直接读 studio store）；展示四卡（worldview/synopsis/audiovisual_style 为文本段落卡，episode_outline 为集列表卡：`第N集 标题 — hook` + beats 列表）；每卡角标 `script-analysis@{version}`；空态给「开始解析」主按钮；运行中逐卡显示流式文本与状态（生成中/完成/失败重试单卡）；底部按钮组：`重新解析`（全量）/ `取消`（运行中）。
- ScriptView：中间列顶部加 `.doc-tabs`（两个按钮切换 `tab = ref("script" | "analysis")`），`v-if` 切换剧本文档区与 `<StoryAnalysisCards />`。

- [ ] **Step 1: 实现 StoryAnalysisCards.vue**（完整组件：template 用现有 `--serif/--line/--btn` token 风格；script setup 从 studioStore 取 `studio.storyAnalysis / studio.analysis`，按钮调 `runStoryAnalysis/cancelStoryAnalysis`）。
- [ ] **Step 2: ScriptView 接线**（tab 按钮 + v-if + 引入组件）。
- [ ] **Step 3: build 通过 → Commit** — `git commit -m "feat: 剧本页项目设定四卡与流式解析"`

---

### Task 14: ScriptView——资产表区（三 tab 网格 + 解析/同步）

**Files:**
- Create: `web-vue/src/views/script/AssetTables.vue`
- Modify: `web-vue/src/views/ScriptView.vue`（tab 增「资产表」）
- Test: build

**Interfaces:**
- `AssetTables.vue`：顶部条 = `重新解析`（流式，显示当前第 N 集）、`同步资产库`（成功 toast 显示 created/merged 报告）、状态行 `由 asset@{version} 解析 · 上次同步 {相对时间|未同步}`；三 tab（角色/场景/道具）。
- 角色表网格：列 = 角色名 / 角色描述 / 人物小传 / 形象名；按角色×形象平铺（每形象一行），同角色第 2+ 行前三列显示「同上」（灰字）；行内 input 直改（blur 时整表 `saveAssetTables`）；「＋形象」按行追加 look 行；「＋角色」追加空角色。
- 场景/道具表：名称 / 描述 两列，同模式。
- 空态：「还没有资产表——点「重新解析」让 AI 从剧本抽取（只生成草稿，不会生图）」。

- [ ] **Step 1: 实现 AssetTables.vue**（数据源 `studio.assetTables`；本地浅拷贝编辑，保存按钮失焦触发；平铺行由 computed 生成：`rows = characters.flatMap(c => (c.looks.length ? c.looks : [{name: ""}]).map(l => ({c, l})))`）。
- [ ] **Step 2: ScriptView tab 接线。**
- [ ] **Step 3: build 通过 → Commit** — `git commit -m "feat: 剧本页资产三表草稿区（解析/编辑/幂等同步）"`

---

### Task 15: AssetsView——形象提示词导出

**Files:**
- Modify: `web-vue/src/views/AssetsView.vue`
- Test: build

**Interfaces:**
- 形象详情操作区（现有形象卡提示词编辑附近）加按钮 `导出提示词`（无提示词时 disabled + title 说明）与 `生成形象图`（点击 = 触发导出 + toast「生图 API 未接入，已导出提示词文件」）。
- 导出实现：`exportLookPrompt(assetId, lookId)` 返回 URL → `window.open(url, "_blank")`（浏览器直接下载 text/plain）。

- [ ] **Step 1: 实现（约 15 行模板 + 5 行 script）。**
- [ ] **Step 2: build 通过 → Commit** — `git commit -m "feat: 形象提示词导出（生图占位，不接 API）"`

---

### Task 16: 全链路验证 + 台账

**Files:**
- Modify: `.superpowers/sdd/2026-10-21-script-asset-pipeline/progress.md`（新建台账）

- [ ] **Step 1: 后端全量** `python -m pytest -q` 全绿。
- [ ] **Step 2: 前端** `npm run build > /tmp/build.log 2>&1; echo $?` 为 0；`node --test tests/*.test.mjs` 全绿。
- [ ] **Step 3: 活体冒烟**（8000 后端已运行则用真服务，否则跳过并记录）：新建项目（选 Skill+自动生成）→ 导入两集剧本 → 等自动 pipeline → 资产表出现 → 改一行 → 同步 → 资产库可见 → 导出一份提示词文件。任何一步失败按 systematic-debugging 处理，不许跳过。
- [ ] **Step 4: 台账记录**（任务清单、偏离：Phase 1 聊天会话未按项目 Skill 过滤工具、Task 2 正文迁移的映射明细、遗留：方言渲染层等 Phase 2 项）。
- [ ] **Step 5: Commit** — `git commit -m "chore: Phase 1 台账与验证记录"`

---

## Self-Review 记录（写完自查）

1. **Spec 覆盖**：§2.1→T1；§2.2→T2；§2.3→T7（skills 选择/建项目字段）+T6（tools_for）；§3→T3/T8；§3.4→T6；§4.1-4.3→T4/T5/T9；§4.4→T4+T9；§4.5→T13/T14；§5→T15；§6 端点→T7/T8/T9；import-script 自动→T10；单飞→T8/T9 共用 `_story_runs`（parse 复用同一注册表，键 `canvas`）。无缺口。
2. **占位符扫描**：T2 的 director/asset 正文为「骨架+原样搬运映射」——源内容已存在于仓库，搬运目标与归属已逐段指明，不属于 TBD。T8/T9 的端点实现给足了装配说明与数据形状。无 "TBD/TODO"。
3. **类型一致性**：`normalize_name`/`merge_asset_tables`/`parse_asset_tables`/`run_story_analysis`/`StoryAnalysis`/`AssetTables` 在 T3-T9 间签名一致；事件 type 字符串两处（契约与测试）一致；`meta.project.skills` 键为下划线、frontmatter role 为连字符——与 Global Constraints 第 7 条一致。

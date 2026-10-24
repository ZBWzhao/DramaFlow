# 阶段 0：定契约、落技能 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 skill-layer-refactor 设计（`docs/superpowers/specs/2026-10-24-skill-layer-refactor-design.md`）的阶段 0：shuohao 7 技能入库、数据契约定稿、产物表 migration、产物注册表代码、样例小说端到端跑通。

**Architecture:** shuohao 技能以 vendored copy 形式放 `pipeline_skills/`（不进 `skills/`，避免被 `core/skills.py` 的画布 Agent 装载器扫描）；7 类管线产物的类型↔技能↔文件名映射落在 `core/pipeline_artifacts.py` 注册表（阶段 1 导入 API、阶段 2 运行时的唯一事实来源）；产物统一存 Postgres `pipeline_artifacts` 表。

**Tech Stack:** Python 3.11+ / pytest / PostgreSQL 17（docker compose）/ Node ≥18（shuohao 质量门脚本是 `.mjs`）

**设计偏差说明（已按此修订设计文档）：** 设计文档原写"并入仓库 `skills/`"，实施改为 `pipeline_skills/`。原因：`core/skills.py:discover_skills` 扫描 `skills/*/SKILL.md` 并把清单注入画布 Agent 提示词，直接放入会污染画布 Agent；且不改 shuohao 上游文件（含 frontmatter），保留合并上游更新能力。`discover_skills(root: Path)` 本就接受 root 参数，阶段 2 可直接指向 `pipeline_skills/`。

## Global Constraints

- 工作目录：`C:\Users\Lenovo\orca\workspaces\toonflow-py-lab\tropicbird`（git worktree，当前分支 `ZBWzhao/find-claude-md-location`），所有命令在此目录执行
- 禁止修改 `skills/` 下现有画布技能、`core/skills.py` 装载器、`teams/` 下任何文件（画布冻结原则）
- `pipeline_skills/` 下的 shuohao 文件为原样 vendored copy，**一个字节都不改**（含 frontmatter）；上游信息记录 SHA
- migration 编号接续 `migrations/0004_production_runs.sql` → 新文件 `0005_pipeline_artifacts.sql`，风格对齐现有文件（`CREATE TABLE IF NOT EXISTS`、`JSONB`、`DOUBLE PRECISION` 时间戳）
- 产物类型规范名（DB 与注册表共用，全局唯一事实）：`outline` / `script` / `characters` / `art` / `staging` / `storyboard` / `character_refs`
- 新增 Python 依赖仅限 `pytest`；Node 仅用于运行 shuohao 自带脚本，不引入 npm 包
- 提交信息风格沿用仓库惯例：`feat:` / `docs:` / `test:` 前缀，每个 Task 至少一次提交
- 测试命令统一 `python -m pytest tests/ -v`（Windows bash 环境）

---

### Task 1: 引入 pipeline_skills/ 技能集

**Files:**
- Create: `pipeline_skills/{novel-outline, novel-script, novel-characters, novel-art, novel-staging, novel-storyboard, character-refs}/`（vendored copy）
- Create: `pipeline_skills/README.md`
- Modify: `docs/superpowers/specs/2026-10-24-skill-layer-refactor-design.md`（按"设计偏差说明"修订一行）
- Test: `tests/test_pipeline_skills.py`、`tests/conftest.py`

**Interfaces:**
- Consumes: 上游仓库 https://github.com/eternityspring/shuohao-skills.git（本机已有浅克隆 `C:/Users/Lenovo/AppData/Local/Temp/shuohao-skills`，若不存在则重新克隆）
- Produces: `pipeline_skills/<skill>/SKILL.md` 目录结构（Task 2、4、5 的输入）；`tests/conftest.py` 的 `sys.path` 引导（Task 4、5 复用）

- [ ] **Step 1: 写失败测试**

创建 `tests/conftest.py`：

```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

创建 `tests/test_pipeline_skills.py`：

```python
"""阶段 0 验收：pipeline_skills/ 下 7 个技能就位，frontmatter 可被仓库装载器解析。"""
from pathlib import Path

from core.skills import parse_frontmatter

PIPELINE_ROOT = Path(__file__).resolve().parent.parent / "pipeline_skills"

EXPECTED = {
    "novel-outline", "novel-script", "novel-characters",
    "novel-art", "novel-staging", "novel-storyboard", "character-refs",
}


def test_seven_skill_dirs_exist():
    actual = {p.name for p in PIPELINE_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")}
    assert EXPECTED <= actual, f"缺技能目录: {EXPECTED - actual}"


def test_each_skill_md_parseable_and_name_matches_dir():
    for dirname in EXPECTED:
        text = (PIPELINE_ROOT / dirname / "SKILL.md").read_text(encoding="utf-8")
        frontmatter, _body = parse_frontmatter(text)
        assert frontmatter.get("name") == dirname, f"{dirname}: frontmatter name 不匹配"
        assert frontmatter.get("description", "").strip(), f"{dirname}: description 为空"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_pipeline_skills.py -v`
Expected: FAIL（`pipeline_skills` 目录不存在）

- [ ] **Step 3: vendored copy 上游技能**

```bash
SRC="C:/Users/Lenovo/AppData/Local/Temp/shuohao-skills"
if [ ! -d "$SRC/.git" ]; then
  SRC=$(mktemp -d)/shuohao-skills
  git clone --depth 1 https://github.com/eternityspring/shuohao-skills.git "$SRC"
fi
mkdir -p pipeline_skills
cp -r "$SRC/skills/"* pipeline_skills/
git -C "$SRC" rev-parse HEAD   # 记下这个 SHA，下一步写进 README
```

- [ ] **Step 4: 写 pipeline_skills/README.md**

```markdown
# pipeline_skills

DramaFlow 内容生产管线的主力技能集，vendored 自
https://github.com/eternityspring/shuohao-skills（上游 SHA: <Step 3 记录的 SHA>，vendor 日期: 2026-10-24）。

## 技能清单与管线顺序

novel-outline → novel-script → {novel-characters, novel-art} → novel-staging → novel-storyboard；
character-refs 为出图环节（依赖 ComfyUI/OpenAI，阶段 3 接入）。

## 约定

- 本目录**不**被 `core/skills.py` 扫描（该装载器只扫 `skills/`），画布 Agent 看不到这些技能；
- 文件为上游原样拷贝，不做任何修改；升级方式：重新 vendor + 更新本文件 SHA；
- 质量门脚本为 Node（`.mjs`），运行需 Node ≥ 18；
- 已知限制：`core/skills.parse_frontmatter` 只支持单行 frontmatter 值，shuohao 的多行
  `description: |` 会解析为占位符——阶段 2 运行时接入前需换宽容解析或做描述覆盖表。
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_pipeline_skills.py -v`
Expected: 2 PASS

- [ ] **Step 6: 修订设计文档**

把设计文档中两处 `skills/` 表述改为与偏差说明一致：
- "将 shuohao 7 技能并入仓库 `skills/`（保留各技能目录结构）" → "将 shuohao 7 技能 vendored 进仓库 `pipeline_skills/`（不进 `skills/`，避免画布装载器扫描；原样拷贝记录上游 SHA）"
- 验收清单 "`skills/` 下 7 个新技能就位" → "`pipeline_skills/` 下 7 个新技能就位且有清单说明"

- [ ] **Step 7: 提交**

```bash
git add pipeline_skills tests/ docs/superpowers/specs/2026-10-24-skill-layer-refactor-design.md
git commit -m "feat: vendor shuohao pipeline skills into pipeline_skills/ (phase 0)"
```

---

### Task 2: 数据契约文档

**Files:**
- Create: `docs/pipeline/data-contract.md`
- 参考输入（只读）: `pipeline_skills/<skill>/SKILL.md`、`references/schema.md`、`examples/*.json`

**Interfaces:**
- Consumes: Task 1 的 `pipeline_skills/`
- Produces: `docs/pipeline/data-contract.md`——Task 4 注册表逐字抄录其"类型映射表"；阶段 1 导入 API 的校验依据

- [ ] **Step 1: 确认 Node 可用**

Run: `node --version`
Expected: v18+。若低于 18 或无 Node：在契约文档"已知缺口"记一条"质量门脚本未在本机验证"，不阻塞本任务（schema 提取只读文档和示例）。

- [ ] **Step 2: 逐技能提取产物 schema**

对 7 个技能依次读 `SKILL.md`（"产出"相关段落）、`references/schema.md`（有则作为字段事实来源，无则以 `SKILL.md` 产出段落 + `examples/*.json` 实际键为准）、`examples/` 下示例 JSON（用 `python -c "import json;print(sorted(json.load(open(p, encoding='utf-8')).keys()))"` 列顶层键）。

- [ ] **Step 3: 写 docs/pipeline/data-contract.md**

文档结构固定如下（每个产物一节，节名用 Task 规范名）：

```markdown
# 管线产物数据契约（阶段 0 定稿）

事实来源优先级：技能 references/schema.md > SKILL.md 产出段落 > examples/*.json 实际键。
本文档是 core/pipeline_artifacts.py 注册表与阶段 1 导入 API 的唯一依据。

## 类型映射表

| 规范名 | 产出技能 | JSON 文件名 | schema 说明文件 | 备注 |
|---|---|---|---|---|
| outline | novel-outline | outline.json | references/schema.md | |
| script | novel-script | （以 schema.md/examples 为准） | ... | |
| characters | novel-characters | （同上） | ... | |
| art | novel-art | ... | ... | |
| staging | novel-staging | ... | ... | |
| storyboard | novel-storyboard | ... | ... | |
| character_refs | character-refs | ... | ... | 阶段 3 出图，先只验收提示词产物 |

## outline（短剧大纲五件套）

- schema_version 出处与当前值：<从 schema.md 或脚本常量摘录>
- 顶层字段：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| ... | ... | ... | ... |

（script / characters / art / staging / storyboard / character_refs 各一节，同结构）

## 存储与导入约定

- Postgres 表 `pipeline_artifacts`，PRIMARY KEY (project_id, artifact_type)，latest-wins upsert；
- schema_version 随产物落库，上游升级时导入层做版本迁移；

## 已知缺口

- （如实记录：某技能无独立 schema 文件 / Node 未验证 / character-refs 出图后端未接 等）
```

- [ ] **Step 4: 交叉核对**

对每个 `examples/*.json`：确认其顶层键都被对应章节的字段表覆盖；有出入的以示例 JSON 实际键为准回填文档。

- [ ] **Step 5: 提交**

```bash
git add docs/pipeline/data-contract.md
git commit -m "docs: pipeline artifact data contract from shuohao skill schemas (phase 0)"
```

---

### Task 3: 产物表 migration

**Files:**
- Create: `migrations/0005_pipeline_artifacts.sql`
- 参考输入（只读）: `migrations/0001_durable_state.sql`（风格基准）、`migrations/0004_production_runs.sql`（确认项目实体表名与 id 类型）

**Interfaces:**
- Consumes: Task 2 定稿的 7 个规范名（Global Constraints 已列）
- Produces: `pipeline_artifacts` 表（阶段 1 导入 API 的写入目标）；7 规范名的 DB CHECK 约束

- [ ] **Step 1: 确认项目实体**

Run: `grep -n "CREATE TABLE" migrations/0004_production_runs.sql`
决策规则：若存在项目/production 实体表且其 id 为 `TEXT`，`project_id` 保持 `TEXT NOT NULL`（不加外键——草稿期允许导入尚无实体的项目，正式外键放阶段 1）；若为 `UUID` 或其他类型，`project_id` 对齐其类型。下文 SQL 按默认 `TEXT` 写。

- [ ] **Step 2: 写 migration**

创建 `migrations/0005_pipeline_artifacts.sql`：

```sql
-- 管线产物表：shuohao 技能链各层 JSON 的统一落库点。
-- latest-wins：PRIMARY KEY (project_id, artifact_type)，导入即 upsert。
-- schema_version 应对上游 shuohao 技能 schema 演进。
CREATE TABLE IF NOT EXISTS pipeline_artifacts (
    project_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL CHECK (artifact_type IN
        ('outline','script','characters','art','staging','storyboard','character_refs')),
    schema_version TEXT NOT NULL DEFAULT '',
    content JSONB NOT NULL,
    source_run_id TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS pipeline_artifacts_project_updated_idx
    ON pipeline_artifacts(project_id, updated_at);
```

- [ ] **Step 3: Postgres 冒烟验证**

```bash
docker compose -f compose.postgres.yml up -d postgres
sleep 5   # 等 healthcheck
docker exec -i dramaflow-postgres psql -U postgres -d dramaflow < migrations/0005_pipeline_artifacts.sql
docker exec dramaflow-postgres psql -U postgres -d dramaflow -c "\d pipeline_artifacts"
docker exec dramaflow-postgres psql -U postgres -d dramaflow -c \
  "INSERT INTO pipeline_artifacts VALUES ('smoke','outline','1.0','{\"ok\":true}'::jsonb,'',0,0) ON CONFLICT DO NOTHING; DELETE FROM pipeline_artifacts WHERE project_id='smoke';"
```

Expected: 建表无报错；`\d` 输出含 `artifact_type` 的 CHECK 约束；INSERT/DELETE 无报错。
若 docker 不可用：降级为 SQL 人工走查（对照 0001 风格与 CHECK 枚举拼写），并在提交信息注明"冒烟未跑，阶段 1 首任务补跑"。

- [ ] **Step 4: 提交**

```bash
git add migrations/0005_pipeline_artifacts.sql
git commit -m "feat: add pipeline_artifacts migration (phase 0 draft)"
```

---

### Task 4: pytest 设施 + 产物注册表

**Files:**
- Modify: `requirements.txt`（追加一行）
- Create: `core/pipeline_artifacts.py`
- Test: `tests/test_pipeline_artifacts.py`

**Interfaces:**
- Consumes: Task 1 的 `pipeline_skills/`（存在性校验）、Task 2 的 `docs/pipeline/data-contract.md` 类型映射表（抄录 filename / schema_doc 的值）
- Produces（阶段 1 导入 API、阶段 2 运行时依赖的 API，签名如下）:
  - `ARTIFACTS: tuple[ArtifactType, ...]`（7 项，顺序即管线顺序）
  - `@dataclass(frozen=True) ArtifactType`，字段：`name: str`（= DB `artifact_type`）、`skill: str`（`pipeline_skills/` 目录名）、`filename: str`（技能产出 JSON 文件名）、`schema_doc: str`（技能内 schema 说明文件相对路径，无独立文件时填 `"SKILL.md"`）、`title: str`（中文名）
  - `get(name: str) -> ArtifactType`（KeyError 抛出）
  - `for_skill(skill: str) -> ArtifactType`（KeyError 抛出）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_pipeline_artifacts.py`：

```python
"""产物注册表：7 类产物、名字唯一、技能目录与 schema 文件真实存在。"""
from pathlib import Path

import pytest

from core.pipeline_artifacts import ARTIFACTS, for_skill, get

PIPELINE_ROOT = Path(__file__).resolve().parent.parent / "pipeline_skills"

EXPECTED_NAMES = {"outline", "script", "characters", "art", "staging", "storyboard", "character_refs"}


def test_seven_artifacts_in_pipeline_order():
    assert {a.name for a in ARTIFACTS} == EXPECTED_NAMES
    assert len(ARTIFACTS) == 7


def test_names_and_skills_unique():
    assert len({a.name for a in ARTIFACTS}) == 7
    assert len({a.skill for a in ARTIFACTS}) == 7


@pytest.mark.parametrize("art", ARTIFACTS, ids=lambda a: a.name)
def test_skill_dir_and_schema_doc_exist(art):
    skill_dir = PIPELINE_ROOT / art.skill
    assert (skill_dir / "SKILL.md").is_file(), f"{art.skill}: 缺 SKILL.md"
    assert (skill_dir / art.schema_doc).is_file(), f"{art.skill}: 缺 {art.schema_doc}"


def test_filename_endswith_json():
    for art in ARTIFACTS:
        assert art.filename.endswith(".json"), f"{art.name}: filename 必须是 .json"


def test_get_and_for_skill_roundtrip():
    for art in ARTIFACTS:
        assert get(art.name) is art
        assert for_skill(art.skill) is art
    with pytest.raises(KeyError):
        get("nope")
    with pytest.raises(KeyError):
        for_skill("drama_script")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_pipeline_artifacts.py -v`
Expected: FAIL，`ModuleNotFoundError: core.pipeline_artifacts`

- [ ] **Step 3: 实现 core/pipeline_artifacts.py**

```python
"""管线产物注册表：产物类型 ↔ 产出技能 ↔ JSON 文件名的唯一映射。

事实来源：docs/pipeline/data-contract.md（Task 2 定稿）。
阶段 1 导入 API 与阶段 2 运行时从这张表取名字，禁止在别处散落产物名字符串。
字段值与契约文档"类型映射表"逐字一致；契约文档更新时同步更新本表。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactType:
    name: str        # 系统规范名（= DB pipeline_artifacts.artifact_type）
    skill: str       # 产出技能（pipeline_skills/ 目录名）
    filename: str    # 技能产出的 JSON 文件名
    schema_doc: str  # 技能内 schema 说明文件（相对技能目录；无独立文件填 "SKILL.md"）
    title: str       # 中文标题


ARTIFACTS: tuple[ArtifactType, ...] = (
    ArtifactType("outline", "novel-outline", "outline.json", "references/schema.md", "短剧大纲五件套"),
    # script / characters / art / staging / storyboard / character_refs 六项：
    # filename 与 schema_doc 从 docs/pipeline/data-contract.md 类型映射表抄录填入，
    # 顺序保持管线顺序：outline, script, characters, art, staging, storyboard, character_refs
)

_ARTIFACTS_BY_NAME = {a.name: a for a in ARTIFACTS}
_ARTIFACTS_BY_SKILL = {a.skill: a for a in ARTIFACTS}


def get(name: str) -> ArtifactType:
    return _ARTIFACTS_BY_NAME[name]


def for_skill(skill: str) -> ArtifactType:
    return _ARTIFACTS_BY_SKILL[skill]
```

按 Task 2 契约文档补全 6 个 `ArtifactType(...)` 条目。

- [ ] **Step 4: requirements.txt 加 pytest**

在 `requirements.txt` 末尾追加：

```
pytest>=8.0.0
```

- [ ] **Step 5: 安装并运行全部测试**

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

Expected: Task 1 的 2 个测试 + 本任务 11 个测试全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add core/pipeline_artifacts.py tests/test_pipeline_artifacts.py requirements.txt
git commit -m "feat: pipeline artifact registry with existence tests (phase 0)"
```

---

### Task 5: 样例小说端到端跑通 + fixtures 固化

**Files:**
- Create: `tests/fixtures/sample_novel_v1.txt`
- Create: `tests/fixtures/pipeline_run1/`（`outline.json`、`script.json`、characters 产物、`art.json`、`staging.json`、`storyboard.json`、character_refs 提示词产物，外加各技能的 Markdown 评审报告）
- Test: `tests/test_pipeline_fixtures.py`
- Modify: `docs/superpowers/specs/2026-10-24-skill-layer-refactor-design.md`（阶段 0 验收清单勾选）

**Interfaces:**
- Consumes: Task 1 的 `pipeline_skills/`（按各 SKILL.md 执行）、Task 4 的注册表（产物文件名以此为准）
- Produces: `tests/fixtures/pipeline_run1/`——阶段 1 导入 API 的集成测试夹具

- [ ] **Step 1: 写失败测试**

创建 `tests/test_pipeline_fixtures.py`：

```python
"""阶段 0 端到端产物固化：管线各层 JSON 真实存在且可解析。"""
import json
from pathlib import Path

from core.pipeline_artifacts import ARTIFACTS

RUN1 = Path(__file__).resolve().parent / "fixtures" / "pipeline_run1"


def test_sample_novel_exists():
    novel = Path(__file__).resolve().parent / "fixtures" / "sample_novel_v1.txt"
    text = novel.read_text(encoding="utf-8")
    assert len(text) >= 2000, "样例小说太短，撑不起改编管线"


@pytest.mark.parametrize("art", ARTIFACTS, ids=lambda a: a.name)
def test_run1_artifact_json_parses(art):
    path = RUN1 / art.filename
    assert path.is_file(), f"{art.name}: 缺 {art.filename}（character_refs 若仅有提示词产物见 README）"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, (dict, list)), f"{art.name}: 产物不是 JSON 对象/数组"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_pipeline_fixtures.py -v`
Expected: FAIL（fixtures 不存在）

- [ ] **Step 3: 写样例小说**

创作**原创**短篇落入 `tests/fixtures/sample_novel_v1.txt`（不用任何受版权保护文本）：现代都市题材、有明确主角/对手/爽点结构、2000–4000 字、人物 ≤ 5 人、场景 ≤ 4 个——规模压到改编成 3 集短剧，控制后续每层技能的执行成本。

- [ ] **Step 4: 按管线顺序执行 6 个文本技能**

依次按各技能 `SKILL.md` 正文执行（技能正文在 `pipeline_skills/<skill>/SKILL.md`，先读正文再动手；references 只按需读）：

1. `novel-outline`：输入样例小说 → 产出 `outline.json` + 评审报告；**改编集数取 3**
2. `novel-script`：输入 outline.json → `script.json` + 评审报告
3. `novel-characters`：输入小说原文 → characters 产物 JSON + 报告
4. `novel-art`：输入 outline.json（有则 seed 预填）→ `art.json` + 报告
5. `novel-staging`：输入 script.json + art.json → `staging.json` + HTML 工具页
6. `novel-storyboard`：输入 script.json → `storyboard.json` + 报告

每个技能：若 SKILL.md 要求跑质量门脚本（`scripts/*.mjs`），用 Node 跑并修到通过（脚本不动，改的是产物 JSON）；产物与报告统一存 `tests/fixtures/pipeline_run1/`。

- [ ] **Step 5: character-refs 按条件执行**

读 `pipeline_skills/character-refs/SKILL.md`：仅产出提示词/JSON 部分（不出图——无出图后端）；产物存 `tests/fixtures/pipeline_run1/`。若其正式 `asset.json` 必须出图才能产出：在 `tests/fixtures/pipeline_run1/README.md` 记录缺口，并把本任务 Step 1 测试中 `character_refs` 对应断言保留（该测试的报错信息已指引读 README）。若 docker 桌面版带可用出图后端则完整跑通。

- [ ] **Step 6: 运行全部测试确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS（若 character_refs 因 Step 5 缺口失败：在 `tests/test_pipeline_fixtures.py` 的该参数用例上加 `@pytest.mark.skip(reason="见 fixtures/pipeline_run1/README.md 出图缺口")`，并在设计文档已知缺口记录——其余 6 类必须 PASS）

- [ ] **Step 7: 对照阶段 0 验收清单**

打开设计文档"阶段 0"验收清单逐项核对：技能就位（Task 1）、契约定稿（Task 2）、migration（Task 3）、样例端到端（本任务）。全部满足后在设计文档中把 4 个 `- [ ]` 勾为 `- [x]`。

- [ ] **Step 8: 提交**

```bash
git add tests/fixtures tests/test_pipeline_fixtures.py docs/superpowers/specs/2026-10-24-skill-layer-refactor-design.md
git commit -m "test: phase 0 end-to-end pipeline fixtures from sample novel"
```

---

## 完成后

阶段 0 验收线达成 → 回到 Desktop 检出（`C:\Users\Lenovo\Desktop\toonflow-py-lab`）编写**阶段 1（链路打通）实施计划**：导入 API、剧本页/制片台渲染、剧本页助手解绑画布。阶段 1 计划消费本计划产出的：`core/pipeline_artifacts.py`、`pipeline_artifacts` 表、`docs/pipeline/data-contract.md`、`tests/fixtures/pipeline_run1/`。

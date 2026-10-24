# pipeline_run1 —— 样例小说《最后一单》端到端跑通记录

本目录是 `tests/fixtures/sample_novel_v1.txt`（原创短篇《最后一单》，约 2650 字，现代都市悬疑）按
`docs/pipeline/` 流程跑一遍的产物快照。七个 JSON 与 `core/pipeline_artifacts.py` 的 ARTIFACTS 注册表
一一对应，**直接用规范文件名**（没有 `<书名>-` 前缀的双写）：

| 文件 | 产出技能 | 质量门命令（在该目录下执行） |
| --- | --- | --- |
| `outline.json` | novel-outline | `node ../../../pipeline_skills/novel-outline/scripts/novel-outline.mjs validate outline.json` |
| `script.json` | novel-script | `... novel-script.mjs validate script.json --outline outline.json` |
| `cast.json` | novel-characters | `... novel-characters.mjs validate cast.json` |
| `art.json` | novel-art | `... novel-art.mjs validate art.json --cast cast.json` |
| `staging.json` | novel-staging | `... novel-staging.mjs validate staging.json --script script.json --art art.json` |
| `storyboard.json` | novel-storyboard | `... novel-storyboard.mjs validate storyboard.json --script script.json --outline outline.json --cast cast.json` |
| `asset.json` | character-refs | `... character-refs.mjs check asset.json`（无图状态 exit 0） |

六个文本技能全部通过官方 `validate` 门（2026-10-24 跑通）：
outline（14 门）、script（3 集 / 6 场 / 50 句，275.7s / 目标 270s）、cast（5 角色，lang=zh）、
art（3 场景 + 3 道具）、staging（3 地点 / 6 场走位）、storyboard（3 集 / 28 段 / 87 分镜，270.2s / 270s）。
各 `*-report.md` / `staging-report.html` / `storyboard-report.md` 是对应的 `render` 产物。

## 输入选择（与技能默认的关系）

- storyboard `promptLang: en`（官方默认，与 渡口 样例一致）、`aspectRatio: 9:16`（竖屏短剧）。
- staging 吃 `script.json + art.json`；storyboard 吃 `script.json`（+ outline/cast 做校验）——与 SKILL.md 的输入表一致。

## 出图缺口（character-refs）

`asset.json` 与 `character_refs/` 下的 intake、prompt 产物已按 SKILL.md Step 0–2 产出
（`intake-check` 通过 → `new --look 写实` 建资产，与 art.json 的摄影式画风一致）。
**未出 PNG**：`gen` 需要先 `config` 选定出图后端（qwen/ComfyUI、codex 订阅或 openai key），
本机无出图后端，`gen` 在配置门处失败（「还没完成初次设置，缺：主图（锚点）用哪个模型……」）。
因此 `asset.json` 处于「零图片、无过期」状态，`check` exit 0——测试不断言图片存在。

顶层 `asset.json` 当前仅含程野一人的资产（本样例只跑了一个角色）；多角色时为合并集，形状见 `docs/pipeline/data-contract.md` 存储与导入约定。

- `character_refs/chengye-intake.json`、`zhouxiaotong-intake.json`：Step 1 一次性输入（主角 C01 程野、C04 周晓桐）
- `character_refs/*-front-full-prompt.json`：`prompt <asset> front-full` 打印的锚点提示词 / 反向词 / 比例
- `character_refs/zhouxiaotong-asset.json`：第二角色的 asset.json
- `character_refs/character-refs.html`：`render` 报告（无图卡片）

## 已知的改笔（相对小说原文的派生决策）

- 小说里的物业值班经理原名含「雨」字，会让 outline 的 `雨戏` 风险门误报，改名周晓桐（小说与全部产物一致）。
- 为满足 storyboard 的台词-fit 门（任何分镜 ≤5s），script 里 8 条超 5s 的台词在剧本层拆成两句短句，
  并为压回每集 ±15% 时长做了轻度收紧；`script.json` 是最终通过门的版本，小说原文是叙事源。
- `.gates.jsonl` 是跑门时的原始输出记录，留作证据。

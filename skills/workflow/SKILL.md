---
name: workflow
description: 制作流程主控：判断从哪开始、按阶段读哪一册、何时确认与恢复、交付什么。只管流程，不管工艺。
---

# workflow：流程路由表

**职责边界**：本 skill 只管流程——顺序、门禁、交付物。**所有工艺写法（首帧八段式、引用与文字锁、分镜信息块、表演与抛光细节）以 `drama_craft` 为唯一来源**。两者是分层，不是二选一：流程说「这一步要做分镜」，工艺说「分镜怎么写」。禁止再引用或叠加旧的 asset_design / drama_script / storyboard / shot_prompt。

## 按阶段读哪一册

| 你现在在做什么 | 读这一册 |
|---|---|
| 刚接手需求、判断从哪开始、推进画布、阶段确认、中断恢复 | `references/10-launch.md` |
| 锁剧情事实、拆节拍、算时长预算、分镜分批 | `references/20-beats-budget.md` |
| 定输出结构、写 shots 与资产节点、跑校验与修正 | `references/30-output-check.md` |
| 每镜站位与朝向、镜间衔接、跨镜连续性 | `references/40-staging-continuity.md` |
| 表演层、最后一轮抛光、穿帮审计 | `references/50-polish.md` |

一次只读当前阶段那一册；到下一阶段再读，不要预先全读。

## 提示词正文

正文由服务端按结构化字段渲染（`data.prompt`，`rendered_by="server"`），人和模型都不手写长正文。字段定义见 `references/30-output-check.md`，每类字段怎么落笔见 `drama_craft`。

## 旧参考（按需查，不是主路径）

- `references/sourceAndScript.md`、`references/assets.md`：剧本未定稿、需要设计提案时。
- `references/canvasExecution.md`、`references/stateAndDelivery.md`：画布工具与状态交付细节查不到时。
- `references/methods/`：要选创作方法时按需读单个文件。

## 底线

用户明确给出的剧情事实、时长、镜头数，优先于本 skill 的任何建议。

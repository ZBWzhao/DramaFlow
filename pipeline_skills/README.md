# pipeline_skills

DramaFlow 内容生产管线的主力技能集，vendored 自
https://github.com/eternityspring/shuohao-skills（上游 SHA: 608ac83d0e6cd5a039de1f0f7d67d4dfcf202886，vendor 日期: 2026-10-24）。

## 技能清单与管线顺序

novel-outline → novel-script → {novel-characters, novel-art} → novel-staging → novel-storyboard；
character-refs 为出图环节（依赖 ComfyUI/OpenAI，阶段 3 接入）。

## 约定

- 本目录**不**被 `core/skills.py` 扫描（该装载器只扫 `skills/`），画布 Agent 看不到这些技能；
- 文件为上游原样拷贝，不做任何修改；升级方式：重新 vendor + 更新本文件 SHA；
- 质量门脚本为 Node（`.mjs`），运行需 Node ≥ 18；
- 已知限制：`core/skills.parse_frontmatter` 只支持单行 frontmatter 值，shuohao 的多行
  `description: |` 会解析为占位符——阶段 2 运行时接入前需换宽容解析或做描述覆盖表。

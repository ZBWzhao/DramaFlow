# 验收报告（2026-10-05 22:07）

通过 22/22

| # | 验收项 | 结果 | 耗时(s) | tokens | 摘要 |
|---|---|---|---|---|---|
| 1 | 阶段0 · 流式 + tool calling | PASS | 4.3 | 841 | === usage: {'input_tokens': 643, 'output_tokens': 198, 'total_tokens': 841} === |
| 2 | 模块一 · 工具系统 + 路径沙箱 | PASS | 9.8 | 20848 | 本次用量：{'input_tokens': 20065, 'output_tokens': 783, 'total_tokens': 20848} |
| 3 | 模块二 · 委派 + 双通道 + 用量 | PASS | 12.9 | 11344 | 覆盖范围确认：list_dir 显示 workspace 根目录为 README.md、notes.txt、sample.py、main.canvas.json、team-demo |
| 4 | 模块二 · 增量 vs 全量计量 | PASS | 5.7 | - | 结论：子会话复用时，全量统计会把上一轮的历史重复计费 —— 这就是 previousEntries 偏移存在的理由。 |
| 5 | 模块二 · 父中止 → 子中止 | PASS | 6.0 | 1078 | >>> ✅ 硬取消路径也结算了：回收 1078 tokens（累计 1078） |
| 6 | 上下文 · 压缩 + 会话落盘 | PASS | 12.2 | 9700 | 最终用量：{'input_tokens': 8547, 'output_tokens': 1153, 'total_tokens': 9700}（含压缩请求本身） |
| 7 | 上下文 · 会话树分支 | PASS | 7.7 | - | 结论：活动分支只有一条链，但文件里两个分支都在 —— 这就是 JSONL 会话树。 |
| 8 | 协作 · 声明式多 Agent 团队 | PASS | 34.6 | - | - **边界**：导演不代写正文，落盘后交 reviewer 复核。 |
| 9 | 技能 · 渐进式披露 | PASS | 5.4 | 3471 | - 需要更细的镜头规范（景别缩写、运动术语等）可以再让我读 `references/shot-format.md`。 |
| 10 | 画布 · 成环防护 + 精确定位 | PASS | 0.7 | - | （临时画布已清理：cycle-demo.canvas.json） |
| 11 | 事件总线 · 不丢生命周期事件 | PASS | 0.3 | - | ✓ 全部通过 |
| 12 | 会话不变量 · 取消/并发/工具集/恢复 | PASS | 3.7 | - | ✓ 全部通过 |
| 13 | 后端 · GET /api/health | PASS | 0.1 | - | HTTP 200｜{"ok": true, "sessions": 1, "model": "deepseek-chat", "session_model": "deepseek- |
| 14 | 后端 · GET /api/tools | PASS | 0.0 | - | HTTP 200｜{"workspace": "C:\\Users\\Lenovo\\Desktop\\toonflow-py-lab\\workspace", "tools":  |
| 15 | 后端 · GET /api/skills | PASS | 0.0 | - | HTTP 200｜{"skills": [{"name": "asset_design", "description": "给短剧角色/场景/道具做设定、写 consistency |
| 16 | 后端 · GET /api/teams | PASS | 0.0 | - | HTTP 200｜{"teams": [{"name": "dramaTeam", "display_name": "短剧生产团队", "entry": "director", " |
| 17 | 后端 · POST /api/agent（NDJSON 流） | PASS | 1.7 | - | HTTP 200｜事件 {"session": 1, "agent_start": 1, "turn_start": 2, "message_start": 2, "usage": |
| 18 | 后端 · GET /api/sessions/{id}/stats | PASS | 0.0 | - | HTTP 200｜{"session_id": "acceptance-1791209270", "main": {"input_tokens": 11091, "output_t |
| 19 | 后端 · GET /api/sessions/{id}/entries | PASS | 0.0 | - | HTTP 200｜{"session_id": "acceptance-1791209270", "entries": [{"ts": 1791209270.8605735, "t |
| 20 | 后端 · GET /api/sessions/{id}/subagents | PASS | 0.0 | - | HTTP 200｜{"session_id": "acceptance-1791209270", "sub_agents": []} |
| 21 | 后端 · POST /api/canvas/connect（类型校验） | PASS | 0.0 | - | HTTP 400｜类型不匹配：text(STRING) → ref(IMAGE) |
| 22 | 后端 · POST /api/canvas/connect（拒绝成环） | PASS | 0.0 | - | HTTP 400｜这条边会形成环：剧本正文 → 第1场分镜 → 剧本正文，流程将无法排序（画布是流水线，不能互相依赖） |

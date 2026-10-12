# LangGraph 对照实现（同一项目的第二条路径）

同一批工具、同一个模型 Key、同一份 `ToolContext`，把迷你 Agent 用 **LangGraph** 再实现一遍。
目的不是"改用框架"，而是**用实测数字回答"手写 agent loop 值不值"**。

> 主线仍是 `core/`（手写）。这个目录是**横向对照**，两者互不依赖 langgraph（除本目录）。

## 跑法

```bash
python langgraph_branch/lg_minimal_agent.py   # StateGraph 版最小 Agent
python langgraph_branch/lg_interrupt.py       # 审批中断 + SQLite checkpoint 续跑
python langgraph_branch/lg_send_fanout.py     # Send 扇出并行
python langgraph_branch/lg_subagent.py        # 子图委派 + 取消级联 + 增量归因
python langgraph_branch/compare.py            # 手写 vs LangGraph 量化对比
```

## 拓扑等价

```
        ┌────────── 有 tool_calls ────────┐
        ▼                                 │
   START → call_model ── 条件边 ──▶ tools ─┘   →  END
              │
              └────── 无 tool_calls ────────────────▶
```

- 手写版：这个圈写在 `core/agent.py` 的 `Agent.run()` 循环里
- LangGraph 版：写成节点 + 条件边，由 Pregel 调度

## 实测（`compare.py`，同一任务、同一模型、两轮交叉顺序取平均）

| 指标 | 手写 | LangGraph |
|---|---|---|
| 步骤数 | 5 | 6 |
| input tokens | 1950 | 1840 |
| output tokens | 208 | 283 |
| **total tokens** | **2157** | **2123** |
| 墙钟 (s) | 2.09 | 4.87 |
| 等价最小循环行数 | 143 | 69 |
| 整个运行时行数 | 1449 | 179（不含 checkpoint/interrupt） |

**结论先行**：token 用量几乎持平（差 1.6%）——**框架不省 token，省的是"什么时候该考虑重跑/审批/续跑"的思考成本**。

行数要把口径说清楚才不被问倒：
- 143 vs 69 —— 只算"最小循环"这一层，手写也只多一倍，那两倍换来的是可控的流式、取消点、事件流
- 1449 vs 179 —— 手写包含了沙箱、压缩、委派、团队、技能；LangGraph 侧这些能力一部分由框架内置（没算进去），一部分仍需自己写

## 框架"白送"的能力（手写要自己实现）

| 能力 | LangGraph 侧 | 手写侧对应物 |
|---|---|---|
| 断点续跑 | `SqliteSaver` + thread_id | `core/session.py` 的 JSONL 快照 + 分支恢复 |
| 工具前人工审批 | `interrupt_before` + `Command(resume=...)` | 自己握手（我已实现 aborts/skills 分支取消与 resume） |
| 并发分发 | `Send` 扇出 | `asyncio.gather` / TaskGroup（实测 6.5x→2.07x 视并发而定） |
| Token 分层归因 | 需在 state reducer 里自己累加 | 已实现 delegator/worker/expert 三层归因 |

## 踩到的坑（真实发生，不是抄文档）

1. **usage reducer 被 None 炸掉**：DeepSeek 的 `usage.model_dump()` 含 `prompt_tokens_details: None`，
   `+=` 直接 `TypeError`。 → reducer 必须自己过滤非数字字段，框架不替你兜 provider 差异。
2. **工具抛错导致后续请求 400**：工具节点直接抛异常时，assistant 的 `tool_calls` 没有对应 tool 消息，
   下一轮请求被 provider 拒（*"must be followed by tool messages"*）。
   → **工具失败必须当成结果喂回模型**（这与 `core/agent.py` 的处理一致）。
3. **路径基准要写进提示词**：模型会照抄任务里的 `workspace/sample.py`，而沙箱 root 已经是 `workspace/`。
   → system prompt 明确"路径相对工作区根目录"。
4. **thread_id 写死 = 历史污染**：复用固定 `thread_id` 时 LangGraph 会从 checkpoint 恢复旧历史，
   新一轮 `user` 追加在旧消息后 → 旧 `assistant(tool_calls)` 找不到 tool 消息，直接 400。
   → `thread_id` 必须每次运行唯一（我用 `parent-main-<run_id>`）。

## 子 Agent 委派（`lg_subagent.py`）

- **独立子会话 = 手动 `ainvoke` 一个独立 thread**，不是 `add_node(compiled_child)`（那个共享父 state）。
- **父子取消不是框架能力**：级联靠 asyncio 的 `Task.cancel()` 传播（同事件循环）。
  跨进程/远程执行时不会传过去——手写版显式维护 abort 信号正是为此。
- **增量归因两边同一招**（偏移相减）：框架的好处是偏移存在 checkpointer 里，还白送 `status.metadata["step"]`。
- 做取消实验必须**等委派真的开始再掐**，否则测的是"取消一个还没委派的父图"。

完整笔记见 `Agent学习/PythonLab/T7-01 LangGraph 横向对比.md`。

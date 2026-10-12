"""toonflow-py-lab 内核：以 Toonflow（TS/Bun）的 Agent 架构为参考，用 Python 独立实现。

分层（对照 Toonflow）：
    core/config.py    配置与凭据          —— 对应 utils/conf
    core/events.py    事件契约与总线      —— 对应 agent/runtime/types.ts 的 AgentEvent
    core/llm.py       一次模型请求        —— 对应 Pi SDK 的 agent-loop 里的单次请求
    core/session.py   会话 + 用量记录     —— 对应 runtime/sessions.ts
    core/context.py   ToolContext 能力包  —— 对应 agent/tools/index.ts 的 createAgentToolContext
    core/plugin.py    ToolPlugin 契约     —— 对应 packages/toolScaffold/src/runtime.ts
    core/loader.py    工具动态装载        —— 对应 utils/plugins/tools.ts
    core/agent.py     agent 循环          —— 对应 Pi 的 agent-loop.ts
    core/subagent.py  子 Agent 委派       —— 对应 runtime/subAgent.ts + delegation.ts
"""

"""技能执行器：gated agent loop —— 阶段 2 的"大脑"内核（Task 2）。

一个阶段 = 一个 vendored 技能 + 一个有界工具环：
  system 上下文运行时读 pipeline_skills/<skill>/SKILL.md + references/schema.md
  全文（vendored 文件即口径，零手抄漂移），LLM 只能调四个工具
  （write_draft / run_quality_gate / read_gate_report / finish）；
  finish 触发权威质量门（node 子进程），门过才经 pipeline_repository.save
  落库（source_run_id="stage:<stage>"），迭代耗尽带 findings 退出、绝不落半成品。

红线约束落点：
- 门只经 node 子进程（唯一可执行文件），timeout 120s，每阶段一次 tempfile.mkdtemp；
- 迭代上限默认 5，超限 = StageResult(ok=False, findings=...)，无脏数据；
- 产物写入只有 pipeline_repository.save 一个口；
- LLM 经 LLMPort 协议注入，本模块不绑任何 SDK——真链路用 _RouterLLM
  （core/llm.LLM 的响应整形），Task 3 起 runner 注入。

事件流（on_event）：{"type":"tool",...} 每次工具调用 / {"type":"gate",...} 每次
权威门 / {"type":"done",...} 收尾——Task 3 的 SSE 直播按此透传。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from . import pipeline_repository

ROOT_DIR = Path(__file__).resolve().parents[1]
_SKILLS_ROOT = ROOT_DIR / "pipeline_skills"

MAX_GATE_TIMEOUT = 120        # 门超时（秒）：node 子进程的唯一预算
GATE_TAIL = 2000              # run_quality_gate 回给模型的输出尾部
FINDING_TAIL = 800            # findings 里带的病灶尾部（进 pipeline_runs.findings）

# 阶段规格表：新增创作阶段 = 加一份 spec（Task 4+），loop 与工具环不动。
# gate 列表首元素是技能目录内的脚本相对路径，其余是参数；cwd = 阶段工作目录。
STAGE_SPECS: dict[str, dict] = {
    "outline": {
        "skill": "novel-outline", "artifact": "outline", "policy": "agent",
        "playbook": ["SKILL.md", "references/schema.md"],
        "sections": ["adaptation", "characters", "scenes", "props", "beats", "episodes"],
        # 元数据键也由模型 write_draft 写入：source/lang/params 是 14 道门的硬输入
        # （params.episodes 驱动分集数/主场景上限），不写死进产物拼装逻辑，
        # 声明在 spec 里让工具环保持通用。
        "meta_sections": ["source", "lang", "params"],
        "gate": ["scripts/novel-outline.mjs", "validate", "outline.json", "--stage", "full"],
        "gate_needs": [],   # 无上游文件
        "task": "按 playbook 把用户小说改编为短剧大纲五件套，写全六节后跑质量门，全过才 finish。",
    },
}


@dataclass
class StageResult:
    ok: bool
    artifact_type: str
    iterations: int
    gate_log: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)   # [{"gate": ..., "detail": ...}]


class LLMPort(Protocol):
    """执行器对 LLM 的最小要求；Task 3 的 runner 与测试替身都照此实现。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> object: ...


# ---------------------------------------------------------------------------
# 工具环
# ---------------------------------------------------------------------------

class _ToolBox:
    """四个工具的实现；草稿/门状态都收在 state 里（run_stage 持有并读取）。"""

    def __init__(self, spec: dict, workdir: Path, inputs: dict, state: dict):
        self.spec, self.workdir, self.inputs, self.state = spec, workdir, inputs, state

    def specs(self) -> list[dict]:
        """OpenAI 风格 tool 定义；section 枚举直接约束顶层键。"""
        sections = [*self.spec["meta_sections"], *self.spec["sections"]]
        return [
            {"type": "function", "function": {
                "name": "write_draft",
                "description": "写入大纲的一个顶层键；重复调用即覆盖该键。九个键全部写完后才允许跑门。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {"type": "string", "enum": sections, "description": "顶层键名"},
                        "data": {"description": "该键的完整 JSON 数据：source/lang 是字符串，"
                                               "params/adaptation 是对象，characters/scenes/props/beats/episodes 是数组"},
                    },
                    "required": ["section", "data"],
                },
            }},
            {"type": "function", "function": {
                "name": "run_quality_gate",
                "description": "把当前草稿组装成产物 JSON 并跑质量门（14 道全查），返回报告尾部。有违规就修对应键。",
                "parameters": {"type": "object", "properties": {}},
            }},
            {"type": "function", "function": {
                "name": "read_gate_report",
                "description": "重跑质量门并返回完整报告（run_quality_gate 只给尾部，要看全量违规清单时用）。",
                "parameters": {"type": "object", "properties": {}},
            }},
            {"type": "function", "function": {
                "name": "finish",
                "description": "结束阶段：触发最终质量门，通过即落库。门没全过绝不调用。",
                "parameters": {"type": "object", "properties": {}},
            }},
        ]

    # 键 → data 的期望类型：不符合直接打回（真模型会偶发漏 section、或把 data
    # 双重编码成 JSON 字符串——静默收下会让门报出难懂的"episodes 为空"）。
    _DATA_KINDS = {"source": str, "lang": str, "params": dict, "adaptation": dict,
                   "characters": list, "scenes": list, "props": list,
                   "beats": list, "episodes": list}

    async def dispatch(self, name: str, args: dict) -> str:
        if name == "write_draft":
            section = str(args.get("section", ""))
            if section not in self.spec["meta_sections"] + self.spec["sections"]:
                return (f"错误：缺 section 或不是合法键。可选键："
                        f"{'、'.join([*self.spec['meta_sections'], *self.spec['sections']])}；"
                        f"重发时把 section 和 data 都带上。")
            data = args.get("data")
            kind = self._DATA_KINDS.get(section)
            if kind is not None and not isinstance(data, kind):
                return (f"错误：{section} 的 data 必须是{'字符串' if kind is str else '对象' if kind is dict else '数组'}本体，"
                        f"收到的是 {type(data).__name__}。"
                        + ("不要把 JSON 再编码成字符串，直接给数组/对象。"
                           if isinstance(data, str) else "按 schema.md 该节的类型重发。"))
            self.state["draft"][section] = data
            return "ok"
        if name in ("run_quality_gate", "read_gate_report"):
            ok, output = await self.run_gate()
            self.state["last_gate"] = {"ok": ok, "output": output}
            self.state["gate_log"].append(output[-GATE_TAIL:])
            return output if name == "read_gate_report" else output[-GATE_TAIL:]
        if name == "finish":
            self.state["finished"] = True
            return "ok"
        return f"未知工具 {name}"

    async def run_gate(self) -> tuple[bool, str]:
        """组装当前草稿 → 产物 JSON，然后跑真门。自检与 finish 共用这一条路。"""
        path = self.workdir / f"{self.spec['artifact']}.json"
        path.write_text(
            json.dumps(_finalize_draft(self.spec, self.state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return await _run_gate(self.spec, self.workdir)


def _finalize_draft(spec: dict, state: dict) -> dict:
    """把 write_draft 攒下的草稿拼成完整产物 JSON。只排序、不补内容——
    门对什么算合格有最终解释权，执行器不替模型圆场。"""
    draft = state["draft"]
    ordered = [*spec["meta_sections"], *spec["sections"]]
    content: dict[str, Any] = {key: draft[key] for key in ordered if key in draft}
    for key, value in draft.items():          # 规格外的键原样保留在尾部
        content.setdefault(key, value)
    return content


# ---------------------------------------------------------------------------
# 质量门
# ---------------------------------------------------------------------------

async def _run_gate(spec: dict, workdir: Path) -> tuple[bool, str]:
    """质量门 = node 子进程（红线：门只准以这种方式跑）。

    sync-vs-async 取舍：这里选 asyncio.create_subprocess_exec 而不是
    subprocess.run——门虽是秒级脚本，但 Task 3 起 run_stage 跑在服务端事件
    循环里（SSE 直播进度、多阶段串行），同步 run 最坏把整个 loop 卡死 120s；
    纯异步 + wait_for 超时 kill 的代价只是几行样板。
    """
    script, *gate_args = spec["gate"]
    cmd = ["node", str(_SKILLS_ROOT / spec["skill"] / script), *gate_args]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=MAX_GATE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"质量门超时（>{MAX_GATE_TIMEOUT}s），进程已终止"
    output = (stdout.decode("utf-8", errors="replace")
              + "\n" + stderr.decode("utf-8", errors="replace")).strip()
    return proc.returncode == 0, output


# ---------------------------------------------------------------------------
# 提示词（vendored 口径 + 执行器纪律）
# ---------------------------------------------------------------------------

def _compose_system(spec: dict) -> str:
    """系统上下文 = playbook 文件全文原样拼接 + 覆盖"需要真人"步骤的纪律段。

    全文原样进 prompt（不摘要不改写）：上游技能更新时口径自动跟随，这是
    "vendored 文件即口径"的硬要求，test_system_prompt_is_vendored_playbook_verbatim 锁死。
    """
    skill_root = _SKILLS_ROOT / spec["skill"]
    playbook = "\n\n".join(
        f"### pipeline_skills/{spec['skill']}/{rel}\n\n{(skill_root / rel).read_text(encoding='utf-8')}"
        for rel in spec["playbook"]
    )
    sections = [*spec["meta_sections"], *spec["sections"]]
    artifact_file = f"{spec['artifact']}.json"
    return f"""{playbook}

## 执行器纪律（覆盖 playbook 里需要真人参与的步骤）

你在 DramaFlow 制片管线的 {spec['artifact']} 阶段工具环里自主工作，没有用户可问：
Step 0 的参数不再向用户询问——episodes、minutesPerEpisode、genre 等由你根据小说文本
判断决定；Step 3/4 的用户拍板取消；Step 7 的报告渲染不归你管。

1. 只能调用四个工具：write_draft / run_quality_gate / read_gate_report / finish。
   严禁把 {artifact_file} 的 JSON 直接写在回复文本里；回复文本（如非空）只是一句话进度说明。
   没有 Bash、Read、Write、Task 等任何其他工具——playbook 里的命令行是给人类
   会话的示例，你没有 shell，也不要试图寻找或执行脚本。
2. 按此顺序写全每一个顶层键，各调一次 write_draft：{'、'.join(sections)}。
   一轮可以并行发多个 write_draft（推荐 3-4 个一组）。data 严格符合上面 schema.md
   对应节的字段定义，一次写到位，不留空壳占位。
   注意：source 是书名字符串（去掉书名号）；lang 固定写 "zh"；
   params.episodes 必须与 episodes 数组长度一致。episodes 数量按小说体量定：
   几千字的短篇用 6–12 集，更长的原作才逐步上探；集数自检通过后不要再改。
   write_draft 的 data 必须是该键的 JSON 数据本体（数组/对象/字符串），
   绝不能是再编码过的 JSON 字符串。
3. 九个键全部写完调 run_quality_gate 自检；门没全过禁止 finish。
   修复纪律：报告点名哪个键，就只重调 write_draft 覆盖那个键，data 给该键的
   完整数据（数组键必须整组重发全部条目，别只发单条）；点名第几集缺什么预警，
   就在该集的 warnings 数组里补上原词。不点名的键一律不动。
4. run_quality_gate 报告全过后，下一轮立刻 finish 结束，不要再打磨其他键。
5. 每一轮都必须发出至少一个工具调用——没有工具调用的一轮就是白白消耗一轮预算。
6. 梗概/hook/suspense 等叙述栏一律叙述体，严禁出现引号对白（「」『』""）。"""


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

async def run_stage(
    db,
    project_id: str,
    stage: str,
    inputs: dict,
    *,
    llm: LLMPort,
    max_iterations: int = 5,
    on_event=None,
) -> StageResult:
    """跑一个创作阶段：有界 agent loop，门过才落库，超限绝不落库。

    一轮 = 一次 llm.complete() + 顺序执行该轮全部工具调用；finish 触发权威门
    （自检门只是预演），失败则 findings 留档、重置 finished 让模型继续修。
    """
    spec = STAGE_SPECS[stage]
    workdir = Path(tempfile.mkdtemp(prefix=f"stage-{stage}-"))   # 每阶段一次，门的 cwd
    state: dict = {"draft": {}, "finished": False, "gate_log": [], "last_gate": None}
    box = _ToolBox(spec, workdir, inputs, state)
    gate_log: list[str] = state["gate_log"]

    messages: list[dict] = [
        {"role": "system", "content": _compose_system(spec)},
        {"role": "user", "content": spec["task"] + "\n\n【小说原文】\n" + inputs["novel_text"]},
    ]
    findings: list[dict] = []
    for rnd in range(1, max_iterations + 1):
        resp = await llm.complete(messages, tools=box.specs())
        calls = list(getattr(resp, "tool_calls", None) or [])
        # OpenAI 协议要求 assistant 带回它发出的 tool_calls（id 配对 tool 消息）；
        # 替身实现不给 id 时用合成 id 补齐，真链路 _RouterLLM 透传模型原始 id。
        assistant: dict[str, Any] = {"role": "assistant", "content": getattr(resp, "text", "") or ""}
        if calls:
            assistant["tool_calls"] = [
                {"id": str(call.get("id") or f"call_{rnd}_{i}"),
                 "type": "function",
                 "function": {"name": call["name"],
                              "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)}}
                for i, call in enumerate(calls)
            ]
        messages.append(assistant)
        for i, call in enumerate(calls):
            out = await box.dispatch(call["name"], call.get("args") or {})
            on_event and on_event({"type": "tool", "name": call["name"],
                                   "args_summary": str(call.get("args"))[:120]})
            on_event and on_event({"type": "tool_result", "name": call["name"],
                                   "preview": out[:200]})   # 门尾/错误回执，冒烟与 SSE 调试用
            messages.append({"role": "tool",
                             "tool_call_id": str(call.get("id") or f"call_{rnd}_{i}"),
                             "name": call["name"],
                             "content": out})
        if not state["finished"]:
            if not calls:
                # 真模型会偶发"只回文本不调工具"的轮次——不加矫正就白烧一轮预算
                # （首冒烟 16 轮里约 9 轮这么浪费掉）。计入迭代（有界不破），但立刻纠偏。
                messages.append({"role": "user",
                                 "content": "这一轮没有工具调用，阶段零进展。立即用工具推进："
                                            "缺键就 write_draft 补，未自检就 run_quality_gate，"
                                            "门全过就 finish。"})
            continue

        # 权威门：唯一能把产物落库的门（自检只是模型的预演，不作数）。
        # 经 box.run_gate() 而非裸 _run_gate——模型可以不 run_quality_gate 直接
        # finish，组装草稿必须在这里自足完成，不能指望工作目录里已有产物。
        ok, output = await box.run_gate()
        gate_log.append(output[-GATE_TAIL:])
        on_event and on_event({"type": "gate", "ok": ok, "round": rnd})
        if ok:
            await pipeline_repository.save(db, project_id, spec["artifact"],
                                           _finalize_draft(spec, state),
                                           source_run_id=f"stage:{stage}")
            on_event and on_event({"type": "done", "ok": True})
            return StageResult(True, spec["artifact"], rnd, gate_log, [])
        findings = [{"gate": "validate", "detail": output[-FINDING_TAIL:]}]
        state["finished"] = False
        # 失败必须把报告喂回对话，模型才知道修哪里（gate_log 只留给调用方）；
        # 修复指令要具体到"覆盖哪个键、整组重发"——首冒烟里模型曾反复只改第 1 集
        # 和无关的 scenes，对报告点名的 warnings 视而不见，预算烧光也没修对。
        messages.append({"role": "user",
                         "content": f"质量门未过（第 {rnd} 轮），报告如下：\n" + output[-GATE_TAIL:]
                                    + "\n逐条修复：报告点名哪个键，就重调一次 write_draft 覆盖那个键"
                                      "的完整数据（数组键整组重发全部条目，别只发单条；点名第几集缺什么"
                                      "预警就在该集 warnings 补原词）。只动点名的键。"
                                      "报告里「第 N 集缺「X」」是机械判据：该集文本扫到了生成难点关键词，"
                                      "而 warnings 数组里没有字符串 X——把 X 原样加进该集 warnings 即可，"
                                      "不用改梗概原文。"
                                      "修完 run_quality_gate，报告全过就立刻 finish。"})

    # 迭代耗尽：带失败报告退出，绝不落产物
    if not findings:
        last = state["last_gate"]
        findings = [{"gate": "validate",
                     "detail": last["output"][-FINDING_TAIL:] if last
                     else f"{max_iterations} 轮内未 finish，无质量门结果"}]
    on_event and on_event({"type": "done", "ok": False})
    return StageResult(False, spec["artifact"], max_iterations, gate_log, findings)


# ---------------------------------------------------------------------------
# 真链路适配器与冒烟入口（手动一次；pytest 不进这里）
# ---------------------------------------------------------------------------

class _RouterLLM:
    """LLMPort 的真链路实现：把 core/llm.LLM 的 AssistantMessage 整形为
    complete() 返回的 SimpleNamespace(text, tool_calls=[{id,name,args}])。

    只做响应整形，不碰请求构造——请求细节（流式、tool 拼装、用量）全部留在
    core/llm.LLM.chat 里，凭据经 Settings 注入（TF_ROUTER_* 或 DEEPSEEK_*）。
    Task 3 起 runner 直接注入本类实例。
    """

    def __init__(self, settings: "Settings | None" = None) -> None:
        from .llm import LLM          # 延迟导入：门/测试路径不背 openai 依赖
        from .config import Settings as _Settings
        self._llm = LLM(settings or _Settings.load())

    async def complete(self, messages: list[dict], tools: list[dict]) -> object:
        msg = await self._llm.chat(messages, tools=tools)
        return SimpleNamespace(
            text=msg.content,
            tool_calls=[{"id": call.id, "name": call.name, "args": call.parsed_args()}
                        for call in msg.tool_calls],
        )


def _load_env_file(path: Path) -> None:
    """KEY=VALUE 逐行入 os.environ（setdefault，不覆盖已有环境）。只服务冒烟入口。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _pick_settings(provider: str, model: str) -> "Settings":
    """冒烟凭据选择：TF_ROUTER 优先（阶段 2 口径），没有/不可用时回退 DEEPSEEK。
    两条路都走 core/llm 既有客户端，只是 Settings 的三个字段不同。"""
    from .config import Settings
    router_key = os.environ.get("TF_ROUTER_API_KEY", "").strip()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if provider == "router" or (provider == "auto" and router_key):
        return Settings(
            api_key=router_key,
            base_url=os.environ.get("TF_ROUTER_BASE_URL", "https://api.toonflow.net/v1"),
            model=model or os.environ.get("TF_ROUTER_MODEL", "deepseek-v4.1-flash"),
        )
    if not deepseek_key:
        raise SystemExit("缺凭据：需要 TF_ROUTER_API_KEY 或 DEEPSEEK_API_KEY（.env.production）")
    return Settings(
        api_key=deepseek_key,
        base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        model=model or os.environ.get("LLM_MODEL", "deepseek-chat"),
    )


async def _bootstrap_pipeline_test(dsn: str) -> None:
    """pipeline_test 库引导（与 tests/pipeline/test_pipeline_repository.py 同法）：
    0001-0004 在全新库上不可用（会 ALTER 既有表），只幂等跑 0005/0006。"""
    import asyncpg

    admin = await asyncpg.connect("postgresql://postgres:postgres@127.0.0.1:5432/dramaflow")
    try:
        try:
            await admin.execute("CREATE DATABASE pipeline_test")
        except asyncpg.DuplicateDatabaseError:
            pass
    finally:
        await admin.close()
    connection = await asyncpg.connect(dsn)
    try:
        for name in ("0005_pipeline_artifacts.sql", "0006_pipeline_runs.sql"):
            await connection.execute((ROOT_DIR / "migrations" / name).read_text(encoding="utf-8"))
    finally:
        await connection.close()


async def _smoke(args: argparse.Namespace) -> int:
    import asyncpg

    from .postgres import PostgresDatabase

    _load_env_file(args.env_file)
    provider = args.provider
    settings = _pick_settings(provider, args.model)
    dsn = "postgresql://postgres:postgres@127.0.0.1:5432/pipeline_test"
    await _bootstrap_pipeline_test(dsn)
    database = PostgresDatabase(dsn, min_size=1, max_size=2)
    database.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=30)
    try:
        novel_text = Path(args.novel).read_text(encoding="utf-8")

        def _on_event(event: dict) -> None:
            print(f"[event] {json.dumps(event, ensure_ascii=False)}", flush=True)

        print(f"== smoke: stage={args.stage} project={args.project} "
              f"provider={provider} model={settings.model} max_iterations={args.max_iterations}",
              flush=True)
        try:
            result = await run_stage(database, args.project, args.stage,
                                     {"novel_text": novel_text}, llm=_RouterLLM(settings),
                                     max_iterations=args.max_iterations, on_event=_on_event)
        except Exception as exc:  # 配额不足等凭据级失败：auto 时回退 DEEPSEEK 重跑一次
            if provider == "auto" and ("insufficient_quota" in str(exc) or "余额不足" in str(exc)):
                settings = _pick_settings("deepseek", args.model)
                print(f"[warn] TF_ROUTER 不可用（{str(exc)[:120]}…），回退 DEEPSEEK_API_KEY "
                      f"model={settings.model}", flush=True)
                result = await run_stage(database, args.project, args.stage,
                                         {"novel_text": novel_text}, llm=_RouterLLM(settings),
                                         max_iterations=args.max_iterations, on_event=_on_event)
            else:
                raise

        print(f"== result: ok={result.ok} artifact={result.artifact_type} "
              f"iterations={result.iterations} gate_runs={len(result.gate_log)}", flush=True)
        for line in result.gate_log[-1:]:
            print(f"[gate tail]\n{line}", flush=True)
        if result.findings:
            for finding in result.findings:
                print(f"[finding] {finding['gate']}: {finding['detail']}", flush=True)
        saved = await pipeline_repository.get(database, args.project, result.artifact_type)
        if saved is None:
            print("== 未落库（门未过或未 finish）", flush=True)
            return 1
        content = saved["content"]
        print(f"== saved: {args.project}/{result.artifact_type} source_run_id=stage:{args.stage} "
              f"keys={sorted(content)}", flush=True)
        return 0 if result.ok else 1
    finally:
        await database.pool.close()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.skill_executor",
        description="真链路冒烟：跑一个创作阶段（真 LLM + 真质量门 → pipeline_test 库）",
    )
    parser.add_argument("stage", choices=sorted(STAGE_SPECS), help="阶段名")
    parser.add_argument("novel", help="小说 txt 文件路径")
    parser.add_argument("--project", default="smoke-outline", help="项目 id（默认 smoke-outline，只写 pipeline_test 库）")
    parser.add_argument("--provider", choices=("auto", "router", "deepseek"), default="auto",
                        help="凭据选择：TF_ROUTER 优先，auto 下配额不足自动回退 deepseek")
    parser.add_argument("--model", default="", help="覆盖模型名（默认跟 provider 走环境变量）")
    # 默认 20：写九节(2-3 轮) + 每轮修一类的修复环（修→门→finish 至少 3 轮/类），
    # 真模型实测 16 轮在 2-3 类违规时刚好差一口气；run_stage 的保守默认仍是 5。
    parser.add_argument("--max-iterations", type=int, default=20)
    parser.add_argument("--env-file", type=Path,
                        default=ROOT_DIR / ".env.production",
                        help="凭据文件（KEY=VALUE）；默认仓库根 .env.production")
    args = parser.parse_args(argv)
    return asyncio.run(_smoke(args))


if __name__ == "__main__":
    sys.exit(_main())

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
  填表型（policy="chain_repair"：characters/art）按权威门运行次数锁 2 次——产全文
  + 恰好一轮修复，被拒的组装轮不消耗门预算（finish 组装检查，fastfollow-2）；
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
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from . import pipeline_artifacts, pipeline_import, pipeline_repository

ROOT_DIR = Path(__file__).resolve().parents[1]
_SKILLS_ROOT = ROOT_DIR / "pipeline_skills"

MAX_GATE_TIMEOUT = 120        # 门超时（秒）：node 子进程的唯一预算
GATE_TAIL = 2000              # run_quality_gate 回给模型的输出尾部
FINDING_TAIL = 800            # findings 里带的病灶尾部（进 pipeline_runs.findings）

# 阶段规格表：新增创作阶段 = 加一份 spec，loop 与工具环不动。
# gate 列表首元素是技能目录内的脚本相对路径，其余是参数；cwd = 阶段工作目录。
# 通用规格键：gate_needs（上游产物注入，inputs[need] 按产物注册表文件名落盘供门
# 对账，并随 user 消息进对话）；gate_files（非产物输入原文落盘，{文件名: inputs 键}，
# 如 characters 的逐字引文校验要小说原文）；meta_sections（模型经 write_draft 写入
# 的元数据键，同 outline 口径——不写死进产物拼装逻辑）；merge_sections（write_draft
# 按 key 分批并入而非覆盖）；save_merge（落库按 data-contract 分批合并，缺省
# latest-wins 整行替换）；policy（"agent"=多轮 gated loop；"chain_repair"=填表型，
# 单次产全文 + 恰好一轮修复）。
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
        # 阶段专属口径（_compose_system 原样带进执行器纪律，不进通用模板——
        # outline 自己定集数，script 等下游阶段的集数以上游产物为事实）。
        "notes": ("注意：source 是书名字符串（去掉书名号）；lang 固定写 \"zh\"；"
                  "params.episodes 必须与 episodes 数组长度一致。episodes 数量按小说体量定："
                  "几千字的短篇用 6–12 集，更长的原作才逐步上探；集数自检通过后不要再改。"),
        "repair_note": ("点名第几集缺什么预警就在该集 warnings 补原词"
                        "（报告里「第 N 集缺「X」」是机械判据：该集文本扫到了生成难点关键词，"
                        "而 warnings 数组里没有字符串 X——把 X 原样加进该集 warnings 即可，"
                        "不用改梗概原文）。"),
    },
    # Task 5：novel-script 执行器。难度在时长门——每集估算秒数必须落在目标 ±15% 内
    # （台词按语速折算、动作按拍计秒），写超写欠都要回炉。
    "script": {
        "skill": "novel-script", "artifact": "script", "policy": "agent",
        "playbook": ["SKILL.md", "references/schema.md"],
        "sections": ["episodes"],           # 按 ep 逐集 write_draft(section="episodes", data=[...])
        # source 是门的硬输入（validate 必填）；params 刻意不可写——语速/容差用技能
        # 默认值，模型自调 tolerance 等于给时长门放水。
        "meta_sections": ["source", "lang"],
        # 逐集写按集号并入（playbook：一批 ≤ 3 集）：分批写、单集修复都不丢已写集。
        "merge_sections": {"episodes": "ep"},
        # 落库按 data-contract「script 分批合并」：批次外的集保留，不是 outline 那种整行替换。
        "save_merge": True,
        "gate": ["scripts/novel-script.mjs", "validate", "script.json", "--outline", "outline.json"],
        "gate_needs": ["outline"],
        "task": "按 playbook 把大纲落成分集剧本（结构化节拍流），逐集写 episodes 后跑质量门，全过才 finish。",
        # 集数/时长/钩子/爽点全部是上游大纲的既有事实（seed 的确定性搬运口径；
        # 执行器没有 seed 命令，由模型逐集搬运，门逐项对账）。
        "notes": ("注意：集数、集号与每集事实字段一律以上游大纲为准，不要自行增删集或改时长："
                  "targetSeconds = 大纲 params.minutesPerEpisode × 60；hook/cliff 从大纲对应集"
                  "的 hook/suspense 搬运；beatsClaimed 认领大纲 beats 落在该集的爽点 type。"
                  "episodes 按 ep 逐集 write_draft（data 是数组，可分批写、可单集修，按集号并入）；"
                  "source/lang 写一次即可（source 同大纲 source，lang 固定写 \"zh\"）。"),
        # 修复口径（2026-10-25 冒烟实证：deepseek-chat 20 轮不收敛——门报每集欠多少秒，
        # 模型只给接近达标的集加一两句，欠时最多的集始终没补够）：折算给死、补足定向。
        "repair_note": ("时长门按集报差距（估Xs/目标Ys）：每欠 4.5 秒 ≈ 补 20 字对白或 1 个动作行"
                        "（动作按 2.5 秒/行折算）。优先补欠时最多的集，一轮把差额一次补够，"
                        "不要把轮数花在给已接近达标的集微调；episodes 整组重发时已达标集原样带上，"
                        "不许降低它们的时长。"),
    },
    # ---------------------------------------------------------------------
    # Task 6：制片四层。填表型（characters/art）policy="chain_repair"：单次
    # complete 产全文 → 权威门 → 未过恰好一轮修复（规格 8.1，预算按权威门计、
    # 锁 2 次，fastfollow-2 起 finish 前置组装检查）；
    # 创作型（staging/storyboard）policy="agent"：与 outline/script 同一条
    # gated agent loop。gate 实参按各技能 SKILL.md 的 validate 用法抄写
    # （brief 初稿与技能文档不一致处以文档为准）：文件名取自
    # pipeline_artifacts 注册表（characters 的产物文件是 cast.json）。
    # ---------------------------------------------------------------------

    # 角色表：门 = validate cast.json book.txt（SKILL.md Step 4）——逐字引文校验
    # 要小说原文，gate_files 把 novel_text 落成 book.txt。summary 缺失门必拦。
    "characters": {
        "skill": "novel-characters", "artifact": "characters", "policy": "chain_repair",
        "playbook": ["SKILL.md", "references/schema.md"],
        "meta_sections": ["source", "lang"],
        "sections": ["summary", "characters"],
        # 契约「characters 逐角色合并」：落库按 name 并入既有卡，重跑/重试不吞卡
        # （与 script 共用 pipeline_import.prepare 的 save_merge 路径，不另起灶）。
        "save_merge": True,
        "gate": ["scripts/novel-characters.mjs", "validate", "cast.json", "book.txt"],
        "gate_needs": ["outline"],
        # 门的非产物输入：逐字引文校验要原文（唯一一个非 JSON 落盘文件）。
        "gate_files": {"book.txt": "novel_text"},
        "task": "从小说与大纲拆角色表（cast.json 形状），过质量门后 finish。",
        "notes": ("注意：lang 固定写 \"zh\"（内置界面语言；写其他语言必须再写一份 ui "
                  "翻译块，否则门直接拦）。summary 是顶层故事摘要（3–5 句、不剧透结局），"
                  "必写。角色清单照上游大纲的 characters 块，不要自行增删角色；"
                  "每张卡写全 persona/image.prompt/voice.prompt，同一批角色的提示词"
                  "不得雷同（相似度门会点名成对的雷同卡）。"),
    },
    # 美术设定：门 = validate art.json --cast cast.json（SKILL.md 用法）——--cast
    # 启用「提示词不含角色名」检查；characters 产物此时已落库（STAGE_ORDER 前序），
    # gate_needs 同时把 cast 注入对话，模型才知道要避开哪些名字。
    "art": {
        "skill": "novel-art", "artifact": "art", "policy": "chain_repair",
        "playbook": ["SKILL.md", "references/schema.md"],
        "meta_sections": ["source"],
        "sections": ["scenes", "props"],
        "gate": ["scripts/novel-art.mjs", "validate", "art.json", "--cast", "cast.json"],
        "gate_needs": ["outline", "characters"],
        "task": "产出美术设定（场景锚点/光照状态/道具），过质量门后 finish。",
        "notes": ("注意：scenes 不为空、props 可以为空数组（只收承载剧情的叙事道具）；"
                  "场景/道具的 image.prompt/negativePrompt/sheet 与 image.tags 一项都"
                  "不能缺；场景与道具清单照上游大纲，场景/道具提示词里严禁出现角色名"
                  "（--cast 门逐字对账）。"),
    },
    # 开场站位：门 = validate staging.json --script script.json --art art.json
    # （SKILL.md Step 4）——--script 启用 cast-complete 门（出场人物跟剧本对账）。
    "staging": {
        "skill": "novel-staging", "artifact": "staging", "policy": "agent",
        "playbook": ["SKILL.md", "references/schema.md"],
        "meta_sections": ["source", "lang"],
        "sections": ["locations", "scenes"],
        "gate": ["scripts/novel-staging.mjs", "validate", "staging.json",
                 "--script", "script.json", "--art", "art.json"],
        "gate_needs": ["script", "art"],
        "task": "为每场戏排站位与走位，过质量门后 finish。",
        "notes": ("注意：scenes 的集号/场号/地点/光照/出场人物全部照剧本搬运，不要"
                  "重新想；每个地点 4–20 件 items（提示词里有、图上没有的不写，按图认"
                  "的必须有 box）；每场每人恰好一条 places（后来才进来的人开场不在），"
                  "同一侧不叠人，群演人数写整数并写明在哪。"),
    },
    # 分镜：门 = validate storyboard.json --script script.json --outline
    # outline.json --cast cast.json（SKILL.md Step 3：--script 硬前提，--outline/
    # --cast 启用提示词人名检查）。staging 不进门的实参，但站位是切镜与走位的
    # 事实来源，随 gate_needs 注入对话。
    "storyboard": {
        "skill": "novel-storyboard", "artifact": "storyboard", "policy": "agent",
        "playbook": ["SKILL.md", "references/schema.md"],
        "meta_sections": ["source", "aspectRatio", "promptLang"],
        "sections": ["episodes"],
        "merge_sections": {"episodes": "ep"},   # 一批 ≤ 3 集，单集修复不丢已写集
        # params 刻意不可写：切段/切镜/同框/时长容差用技能默认值（DEFAULT_PARAMS），
        # 模型自调 tolerance 等于给时长门放水（同 script 的 params 口径）。
        "gate": ["scripts/novel-storyboard.mjs", "validate", "storyboard.json",
                 "--script", "script.json", "--outline", "outline.json", "--cast", "cast.json"],
        "gate_needs": ["script", "art", "staging", "outline", "characters"],
        "task": "把剧本+站位落成镜头表与首帧/视频提示词，过质量门后 finish。",
        "notes": ("注意：aspectRatio 只接受 \"16:9\"/\"9:16\"（竖屏短剧写 \"9:16\"）；"
                  "promptLang 默认写 \"en\"。每段 9–15 秒且不跨场、段内每切 2–5 秒、"
                  "每集总时长落在剧本目标 ±15% 内、单切同框 ≤ 3 人；h3Prompt 首行对齐"
                  "指令与 [Shot k] 切点时刻由分镜秒数推导（门逐字对账，一个字符都不许"
                  "漂），认领台词逐字进 <d> 块；分镜图提示词直呼角色名，shot/h3 正文"
                  "严禁出现角色名；episodes 按 ep 逐集 write_draft（可分批写、可单集修，"
                  "按集号并入）。"),
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
        # gate_needs 驱动的上游落盘（通用逻辑，非某个阶段的特判）：inputs[need] 是
        # runner 从 pipeline_artifacts 取来的已落库 content，按注册表的正式文件名
        # 落盘（outline.json / cast.json——注册表是产物文件名唯一映射，characters
        # 的落盘名不是 <artifact>.json 的直觉名）供门的 --outline/--cast 等实参读取。
        for need in spec.get("gate_needs") or []:
            payload = inputs.get(need)
            if payload is not None:
                (workdir / pipeline_artifacts.get(need).filename).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        # gate_files：非产物输入原文落盘（characters 的逐字引文校验要小说原文——
        # 没有原文文件门只会跳过引文对账，等于白送一道门的放水）。
        for filename, key in (spec.get("gate_files") or {}).items():
            payload = inputs.get(key)
            if isinstance(payload, str) and payload:
                (workdir / filename).write_text(payload, encoding="utf-8")

    def specs(self) -> list[dict]:
        """OpenAI 风格 tool 定义；section 枚举直接约束顶层键。"""
        meta = spec_meta_sections(self.spec)
        sections = [*meta, *self.spec["sections"]]
        merge_note = "".join(
            f"；{sec} 按 {key} 逐条并入：可整组重发，也可只发要写/要改的条目，"
            f"已写的其他条目不受影响"
            for sec, key in (self.spec.get("merge_sections") or {}).items()
        )
        return [
            {"type": "function", "function": {
                "name": "write_draft",
                "description": f"写入产物的一个顶层键；重复调用即覆盖该键{merge_note}。"
                               f"{len(sections)} 个键全部写完后才允许跑门。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section": {"type": "string", "enum": sections, "description": "顶层键名"},
                        "data": {"description": "该键的完整 JSON 数据：字符串/对象/数组以 schema.md "
                                               "对应节为准（如 source/lang/summary 是字符串，"
                                               "params/adaptation 是对象，"
                                               "characters/scenes/props/beats/episodes/locations 是数组）"},
                    },
                    "required": ["section", "data"],
                },
            }},
            {"type": "function", "function": {
                "name": "run_quality_gate",
                "description": "把当前草稿组装成产物 JSON 并跑质量门（各道全查），返回报告尾部。有违规就修对应键。",
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
                   "summary": str, "aspectRatio": str, "promptLang": str,
                   "characters": list, "scenes": list, "props": list,
                   "beats": list, "episodes": list, "locations": list}

    async def dispatch(self, name: str, args: dict) -> str:
        if name == "write_draft":
            meta = spec_meta_sections(self.spec)
            section = str(args.get("section", ""))
            if section not in meta + self.spec["sections"]:
                return (f"错误：缺 section 或不是合法键。可选键："
                        f"{'、'.join([*meta, *self.spec['sections']])}；"
                        f"重发时把 section 和 data 都带上。")
            data = args.get("data")
            kind = self._DATA_KINDS.get(section)
            if kind is not None and not isinstance(data, kind):
                return (f"错误：{section} 的 data 必须是{'字符串' if kind is str else '对象' if kind is dict else '数组'}本体，"
                        f"收到的是 {type(data).__name__}。"
                        + ("不要把 JSON 再编码成字符串，直接给数组/对象。"
                           if isinstance(data, str) else "按 schema.md 该节的类型重发。"))
            merge_key = (self.spec.get("merge_sections") or {}).get(section)
            if merge_key is not None:
                keyed = _keyed_items(data, merge_key)
                if keyed is None:
                    return (f"错误：{section} 按 {merge_key} 逐条并入，data 必须是非空数组，"
                            f"且每项都是含合法整数 {merge_key} 的对象——修正后重发。")
                existing = self.state["draft"].get(section)
                if isinstance(existing, list):
                    data = _merge_by_key(existing, keyed, merge_key)
            self.state["draft"][section] = data
            return "ok"
        if name in ("run_quality_gate", "read_gate_report"):
            ok, output = await self.run_gate()
            self.state["last_gate"] = {"ok": ok, "output": output}
            self.state["gate_log"].append(output[-GATE_TAIL:])
            return output if name == "read_gate_report" else output[-GATE_TAIL:]
        if name == "finish":
            # chain_repair 的结构保证（fastfollow-2）：finish 前置组装检查——缺键/
            # 空键直接打回（点名缺失键），半稿进不了权威门。真模型实证（2026-10-25）：
            # deepseek-chat 把 characters 产稿拆两轮，修复轮拿到半成品烧光预算未落库；
            # 旧口径 finish 无条件置 finished，半稿直接进门。组装轮不消耗门预算
            # （预算按权威门运行次数计，见 run_stage 的动态上界）。
            if self.spec.get("policy") == "chain_repair":
                required = [*spec_meta_sections(self.spec), *self.spec["sections"]]
                missing = [k for k in required
                           if k not in self.state["draft"]
                           or self.state["draft"][k] in (None, "", [], {})]
                if missing:
                    self.state["finish_rejections"] = self.state.get("finish_rejections", 0) + 1
                    return (f"finish 被拒绝：草稿还缺 {'、'.join(missing)}。"
                            "先把这些键 write_draft 写全（数组键一次整组），"
                            "同一轮立即再 finish——组装轮不消耗你的门预算。")
            self.state["finished"] = True
            return "ok"
        return f"未知工具 {name}"

    async def run_gate(self) -> tuple[bool, str]:
        """组装当前草稿 → 产物 JSON，然后跑真门。自检与 finish 共用这一条路。"""
        path = self.workdir / pipeline_artifacts.get(self.spec["artifact"]).filename
        path.write_text(
            json.dumps(_finalize_draft(self.spec, self.state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return await _run_gate(self.spec, self.workdir)


def spec_meta_sections(spec: dict) -> list[str]:
    """规格的元数据键；缺省无（新阶段规格可以只写 sections，不背 outline 的三件套）。"""
    return spec.get("meta_sections") or []


def _keyed_items(data: Any, key: str) -> dict | None:
    """data → {key: item}；不是非空数组、或有项缺合法整数 key 时返回 None（打回重发）。
    bool 是 int 子类，显式剔除——True 当集号会静默错并。"""
    if not isinstance(data, list) or not data:
        return None
    keyed: dict[int, Any] = {}
    for item in data:
        if not isinstance(item, dict):
            return None
        k = item.get(key)
        if not isinstance(k, int) or isinstance(k, bool):
            return None
        keyed[k] = item
    return keyed


def _merge_by_key(existing: list, keyed_new: dict, key: str) -> list:
    """按键分批合并（docs/pipeline/data-contract.md「script 分批合并」，与
    core/pipeline_import._merge_by_key 同一语义）：既有项原样保留（键无法识别的
    也保留），同键整对象替换且位置不变，新键追加在后。write_draft 的
    merge_sections 与落库 save_merge 共用这一口径——草稿和库不出现两套合并语义。"""

    def _key_of(item: Any) -> int | None:
        if isinstance(item, dict):
            k = item.get(key)
            if isinstance(k, int) and not isinstance(k, bool):
                return k
        return None

    merged: list = []
    replaced: set[int] = set()
    for item in existing:
        k = _key_of(item)
        if k is not None and k in keyed_new:
            merged.append(keyed_new[k])
            replaced.add(k)
        else:
            merged.append(item)
    for k, item in keyed_new.items():
        if k not in replaced:
            merged.append(item)
    return merged


def _finalize_draft(spec: dict, state: dict) -> dict:
    """把 write_draft 攒下的草稿拼成完整产物 JSON。只排序、不补内容——
    门对什么算合格有最终解释权，执行器不替模型圆场。"""
    draft = state["draft"]
    ordered = [*spec_meta_sections(spec), *spec["sections"]]
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


async def run_gate_only(artifact_type: str, content: dict, upstreams: dict) -> tuple[bool, str]:
    """模块级公开门入口（Task 8）：改稿脑 apply 的自动体检用。

    与阶段执行器同一条真门（node 子进程）、同一条组装与落盘规则
    （content 按产物注册表文件名落盘、upstreams 按 gate_needs/gate_files 落盘），
    但只判不落——落库永远是调用方（审批端点）的事，本函数对库零写入。

    artifact_type 从 STAGE_SPECS 反查阶段规格（六类创作产物各有一个 spec）；
    upstreams 键 = gate_needs 的产物名（outline/cast/...），缺了的门自会报缺文件。
    注意 gate_files（characters 的 book.txt←novel_text）依赖的小说原文不在产物库，
    这里没有小说原文可给——该类门会跳过逐字引文对账（brief 只要求 script 体检，
    characters 的体检需回填原文后另行放开）。
    """
    stage = next((s for s, spec in STAGE_SPECS.items() if spec["artifact"] == artifact_type), None)
    if stage is None:
        raise KeyError(f"未知产物类型：{artifact_type}（STAGE_SPECS 无对应阶段）")
    spec = STAGE_SPECS[stage]
    workdir = Path(tempfile.mkdtemp(prefix=f"gate-only-{artifact_type}-"))
    try:
        (workdir / pipeline_artifacts.get(artifact_type).filename).write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # _ToolBox 的构造副作用即 gate_needs/gate_files 落盘——与 run_stage 共用
        # 这一段，保证「模型看到的」与「门对账的」是同一份事实（不另起灶）。
        _ToolBox(spec, workdir, dict(upstreams), {"draft": {}})
        return await _run_gate(spec, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
    meta = spec_meta_sections(spec)
    sections = [*meta, *spec["sections"]]
    artifact_file = pipeline_artifacts.get(spec["artifact"]).filename
    if spec.get("policy") == "chain_repair":
        # 填表型纪律（规格 8.1）：一轮产全文 → finish 触发权威门 → 恰好一轮修复。
        # 不设自检轮（run_quality_gate）——预算只有 2 次权威门，自检等于白烧一半。
        steps = f"""2. 第一轮就并行发齐全部 write_draft，把 {len(sections)} 个顶层键一次写全：{'、'.join(sections)}。
   data 严格符合上面 schema.md 对应节的字段定义，一次写到位，不留空壳占位；写全后
   同一轮立即调用 finish 触发权威质量门（门就是自检，不必也不应先 run_quality_gate）。
   {spec.get("notes", "")}
   write_draft 的 data 必须是该键的 JSON 数据本体（数组/对象/字符串），
   绝不能是再编码过的 JSON 字符串。
3. 权威门未过时你会收到一份带门报告的 user 消息——这是唯一一轮修复机会：只重调
   write_draft 覆盖报告点名的键，data 给该键的完整数据（数组键必须整组重发全部条目，
   别只发单条），修完同一轮立刻再 finish。不点名的键一律不动。{spec.get("repair_note", "")}
4. 每一轮都必须发出至少一个工具调用——没有工具调用的一轮就是白白消耗一轮预算。
5. 梗概/摘要等叙述栏一律叙述体，严禁出现引号对白（「」『』""）。"""
    else:
        steps = f"""2. 按此顺序写全每一个顶层键，各调一次 write_draft：{'、'.join(sections)}。
   一轮可以并行发多个 write_draft（推荐 3-4 个一组）。data 严格符合上面 schema.md
   对应节的字段定义，一次写到位，不留空壳占位。
   {spec.get("notes", "")}
   write_draft 的 data 必须是该键的 JSON 数据本体（数组/对象/字符串），
   绝不能是再编码过的 JSON 字符串。
3. {len(sections)} 个键全部写完调 run_quality_gate 自检；门没全过禁止 finish。
   修复纪律：报告点名哪个键，就只重调 write_draft 覆盖那个键，data 给该键的
   完整数据（数组键必须整组重发全部条目，别只发单条）。不点名的键一律不动。
   {spec.get("repair_note", "")}
4. run_quality_gate 报告全过后，下一轮立刻 finish 结束，不要再打磨其他键。
5. 每一轮都必须发出至少一个工具调用——没有工具调用的一轮就是白白消耗一轮预算。
6. 梗概/hook/suspense 等叙述栏一律叙述体，严禁出现引号对白（「」『』""）。"""
    return f"""{playbook}

## 执行器纪律（覆盖 playbook 里需要真人参与的步骤）

你在 DramaFlow 制片管线的 {spec['artifact']} 阶段工具环里自主工作，没有用户可问：
playbook 里需要真人参与的步骤（Step 0 询问参数、用户拍板、报告渲染等）一律取消，
输入与口径以下文和给你的上游产物为准。

1. 只能调用四个工具：write_draft / run_quality_gate / read_gate_report / finish。
   严禁把 {artifact_file} 的 JSON 直接写在回复文本里；回复文本（如非空）只是一句话进度说明。
   没有 Bash、Read、Write、Task 等任何其他工具——playbook 里的命令行是给人类
   会话的示例，你没有 shell，也不要试图寻找或执行脚本。
{steps}"""


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
    chain_repair（填表型）的 finish 前置组装检查：缺键/空键直接打回、不置
    finished，被拒组装轮不消耗门预算（循环上界随 finish_rejections 动态抬高）；
    预算语义 = 权威门运行次数，锁 2 次。
    """
    spec = STAGE_SPECS[stage]
    # chain_repair（填表型，规格 8.1）：单次产全文 + 恰好一轮修复——预算按"权威门
    # 运行次数"计、锁 2 次（调用方给更大的 max_iterations 也不放宽）。被拒的组装
    # 轮不消耗门预算：循环上界随 finish_rejections 动态抬高（每轮重算，见下）。
    # agent 型才吃调用方预算。
    chain_repair = spec.get("policy") == "chain_repair"
    base_iterations = min(max_iterations, 2) if chain_repair else max_iterations
    workdir = Path(tempfile.mkdtemp(prefix=f"stage-{stage}-"))   # 每阶段一次，门的 cwd
    state: dict = {"draft": {}, "finished": False, "gate_log": [], "last_gate": None,
                   "finish_rejections": 0}
    box = _ToolBox(spec, workdir, inputs, state)
    gate_log: list[str] = state["gate_log"]

    # 上游产物随 user 消息进对话：执行器模型没有 Read 工具（纪律第 1 条），
    # 大纲等 gate_needs 产物必须把内容直接给它；同一份 content 也已由 _ToolBox
    # 按注册表文件名落盘供门对账——模型与门看到同一事实。
    user_content = spec["task"] + "\n\n【小说原文】\n" + inputs["novel_text"]
    for need in spec.get("gate_needs") or []:
        if need in inputs:
            user_content += (f"\n\n【上游产物 {pipeline_artifacts.get(need).filename}（门的对账依据，已落库版本）】\n"
                             + json.dumps(inputs[need], ensure_ascii=False, indent=2))
    messages: list[dict] = [
        {"role": "system", "content": _compose_system(spec)},
        {"role": "user", "content": user_content},
    ]
    findings: list[dict] = []
    # while 化（原 for range）：chain_repair 的上界 = min(max_iterations, 2) +
    # finish_rejections，拒绝发生在轮内，必须每轮重算；agent 型上界恒定。
    rnd = 0
    while rnd < (base_iterations + state["finish_rejections"]
                 if chain_repair else base_iterations):
        rnd += 1
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
            content = _finalize_draft(spec, state)
            import_findings: list[dict] = []
            if spec.get("save_merge"):
                # data-contract「script 分批合并」：按集号并入已落库产物，批次外的
                # 集保留——重跑/重试不吞既有集（outline 无 save_merge，latest-wins
                # 整行替换）。合并整形唯一出口 pipeline_import.prepare，不另起灶。
                existing = await pipeline_repository.get(db, project_id, spec["artifact"])
                content, warnings = pipeline_import.prepare(
                    pipeline_artifacts.get(spec["artifact"]),
                    content, existing["content"] if existing else None,
                )
                # 合并告警入账（fastfollow-2，恢复 Task 5 被 `content, _` 丢掉的
                # 第二返回值）：库内 existing 形状异常时的契约告警（整行替换等）
                # 不再静默，落进 StageResult.findings 留档。ok=True 的 findings
                # 也可以非空——runner 落 JSONB 已兼容。
                import_findings = [{"gate": "import", "detail": w} for w in warnings]
            await pipeline_repository.save(db, project_id, spec["artifact"],
                                           content,
                                           source_run_id=f"stage:{stage}")
            on_event and on_event({"type": "done", "ok": True})
            return StageResult(True, spec["artifact"], rnd, gate_log, import_findings)
        findings = [{"gate": "validate", "detail": output[-FINDING_TAIL:]}]
        state["finished"] = False
        # 失败必须把报告喂回对话，模型才知道修哪里（gate_log 只留给调用方）；
        # 修复指令要具体到"覆盖哪个键、整组重发"——首冒烟里模型曾反复只改第 1 集
        # 和无关的 scenes，对报告点名的 warnings 视而不见，预算烧光也没修对。
        # 尾巴按策略分支（与 _compose_system 两个分支的自检口径一致）：agent 型
        # 多轮预算下自检不亏，修完先 run_quality_gate；chain_repair 预算锁 2 次
        # 权威门，自检等于白烧一半——修完同轮立刻 finish（门即自检）。
        tail = ("修完同一轮立刻再 finish，不要先 run_quality_gate（预算只有 2 次权威门，自检等于白烧）。"
                if spec.get("policy") == "chain_repair"
                else "修完 run_quality_gate，报告全过就立刻 finish。")
        messages.append({"role": "user",
                         "content": f"质量门未过（第 {rnd} 轮），报告如下：\n" + output[-GATE_TAIL:]
                                    + "\n逐条修复：报告点名哪个键，就重调一次 write_draft 覆盖那个键"
                                      "的完整数据（数组键整组重发全部条目，别只发单条）。只动点名的键。"
                                      + (spec.get("repair_note") or "")
                                      + tail})

    # 迭代耗尽：带失败报告退出，绝不落产物（iterations = 实际轮数，含被拒组装轮）
    if not findings:
        last = state["last_gate"]
        findings = [{"gate": "validate",
                     "detail": last["output"][-FINDING_TAIL:] if last
                     else f"{rnd} 轮内未 finish，无质量门结果"}]
    on_event and on_event({"type": "done", "ok": False})
    return StageResult(False, spec["artifact"], rnd, gate_log, findings)


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

        # 上游产物按 gate_needs 注入（与 runner._upstream 同口径）：script 冒烟
        # 依赖同项目已落库的 outline。缺上游立刻失败退出（fastfollow-2 队列 10）：
        # 旧行为缺了只带 novel_text，必然失败的尝试照样烧满 20 轮 LLM 才死在门上。
        inputs: dict = {"novel_text": novel_text}
        for need in STAGE_SPECS[args.stage].get("gate_needs") or []:
            row = await pipeline_repository.get(database, args.project, need)
            if row is None:
                print(f"上游产物 {need} 不存在——先跑它的阶段冒烟，别空烧 LLM", flush=True)
                return 1
            inputs[need] = row["content"]

        print(f"== smoke: stage={args.stage} project={args.project} "
              f"provider={provider} model={settings.model} max_iterations={args.max_iterations}",
              flush=True)
        try:
            result = await run_stage(database, args.project, args.stage,
                                     inputs, llm=_RouterLLM(settings),
                                     max_iterations=args.max_iterations, on_event=_on_event)
        except Exception as exc:  # 配额不足等凭据级失败：auto 时回退 DEEPSEEK 重跑一次
            if provider == "auto" and ("insufficient_quota" in str(exc) or "余额不足" in str(exc)):
                settings = _pick_settings("deepseek", args.model)
                print(f"[warn] TF_ROUTER 不可用（{str(exc)[:120]}…），回退 DEEPSEEK_API_KEY "
                      f"model={settings.model}", flush=True)
                result = await run_stage(database, args.project, args.stage,
                                         inputs, llm=_RouterLLM(settings),
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

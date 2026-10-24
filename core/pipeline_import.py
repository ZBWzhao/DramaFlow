"""导入合并核心：把请求体 content 整形为可落库的产物 content（纯函数，不碰库）。

事实来源：docs/pipeline/data-contract.md（阶段 1 定案，Task 1 commit 4291738 锁定）。

- script 按 episodes[].ep 分批合并：新集替换同号集（位置不变），未出现的集逐字节
  保留，新集追加在后；payload 无法识别集数组 → PipelineImportError；库内 existing
  无法识别集数组 → 整行替换并记告警（契约"存储与导入约定"script 分批合并定案）。
- characters / character_refs 落库为 {"characters": [...]} 形状，逐角色合并：
  同标识整对象替换、他角色保留；单个角色对象（asset.json 单角色文件）自动包装
  （契约 character_refs 合并形状定案；characters 同形）。
- 其余类型（outline/art/staging/storyboard）content 原样落库。
- payload 顶层出现契约字段表完全不认识的键 → finding 告警、不拒绝（契约宽容条款：
  art dynasty/lang、script params/lang、outline thresholds 等均不硬要求）。
- schema_version 不由本模块设置（由请求/API 层负责，Task 3）——prepare 只做内容整形。
"""

from __future__ import annotations

from typing import Any

from .pipeline_artifacts import ArtifactType


class PipelineImportError(Exception):
    """导入结构非法。message + findings 可直接序列化为 api.js 的 422 形态。"""

    def __init__(self, message: str, findings: list[str]) -> None:
        super().__init__(message)
        self.message = message
        self.findings = list(findings)

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "findings": self.findings}


# 角色标识字段：契约 characters 节 characters[] 卡字段表只有
# name/aliases/importance/oneLiner/persona/image/voice，无显式 id 字段，故以 name
# 为角色标识；character_refs 节 asset.json 的 name 亦标注"只用于文件名、标识和报告"
# （docs/pipeline/data-contract.md characters / character_refs 两节）。
CHARACTER_IDENTITY_FIELD = "name"

# 剧本集号字段：契约 script 节 episodes[] 嵌套首列 `ep`
# （docs/pipeline/data-contract.md script 节）。
SCRIPT_EPISODE_FIELD = "ep"

# 包装/合并数组键：契约"存储与导入约定"——character_refs 落库 content 为
# {"characters": [...]}；script 的集数组键为 episodes（契约 script 节顶层字段表）。
CHARACTER_ARRAY_FIELD = "characters"
SCRIPT_EPISODES_FIELD = "episodes"

# 各产物契约顶层字段表（docs/pipeline/data-contract.md 各节"顶层字段"表逐字抄录）。
# 用途仅限"未知键告警"，不做必填校验——契约宽容条款禁止硬要求。
_CONTRACT_TOP_LEVEL: dict[str, frozenset[str]] = {
    "outline": frozenset(
        {"source", "lang", "params", "adaptation", "characters", "scenes", "props", "beats", "episodes"}
    ),
    "script": frozenset({"source", "episodes", "params", "lang"}),
    "characters": frozenset({"source", "lang", "summary", "characters", "ui"}),
    "art": frozenset({"source", "scenes", "props", "dynasty", "lang"}),
    "staging": frozenset({"source", "lang", "locations", "scenes"}),
    "storyboard": frozenset({"source", "aspectRatio", "promptLang", "params", "episodes", "lang"}),
    "character_refs": frozenset({"name", "source", "lang", "ui", "layers", "outfits"}),
}

# characters 产物角色卡字段表（契约 characters 节 `characters[]` 卡：无 id 字段）。
# 单角色对象按这张表查未知键，而非文档顶层表。
_CHARACTER_CARD_FIELDS = frozenset(
    {"name", "aliases", "importance", "oneLiner", "persona", "image", "voice"}
)


def prepare(
    art: ArtifactType,
    payload: Any,
    existing: dict | None,
) -> tuple[dict | list, list[str]]:
    """返回 (落库 content, findings)。findings 非空表示有告警但可落库；
    结构非法时 raise PipelineImportError(message, findings)。"""
    findings: list[str] = []
    if not isinstance(payload, dict):
        raise PipelineImportError(
            f"{art.name}: 请求体 content 必须是 JSON 对象，收到 {type(payload).__name__}", findings
        )
    if art.name == "script":
        _warn_unknown_keys(_CONTRACT_TOP_LEVEL["script"], payload, art.name, findings)
        return _prepare_script(payload, existing, findings), findings
    if art.name in ("characters", "character_refs"):
        return _prepare_character_set(art, payload, existing, findings), findings
    # outline / art / staging / storyboard：原样落库
    _warn_unknown_keys(_CONTRACT_TOP_LEVEL.get(art.name), payload, art.name, findings)
    return payload, findings


# ---------------------------------------------------------------- script

def _prepare_script(payload: dict, existing: dict | None, findings: list[str]) -> dict | list:
    episodes = payload.get(SCRIPT_EPISODES_FIELD)
    if not isinstance(episodes, list):
        raise PipelineImportError(
            "script: 请求体缺少可识别的集数组 episodes[]（契约 script 节），无法按集合并",
            findings,
        )
    numbered: list[tuple[int, dict]] = []
    for index, item in enumerate(episodes):
        numbered.append((_require_episode_number(item, index), item))
    existing_episodes = existing.get(SCRIPT_EPISODES_FIELD) if isinstance(existing, dict) else None
    if not isinstance(existing_episodes, list):
        if existing is not None:
            findings.append(
                "script: 库内 existing 无法识别集数组，按整行替换处理"
                "（契约存储与导入约定：非预期结构整行替换并记录告警）"
            )
        return payload
    merged = _merge_by_key(
        existing_episodes, {ep: item for ep, item in numbered}, _episode_number_of
    )
    return {
        **_without(existing, SCRIPT_EPISODES_FIELD),
        **_without(payload, SCRIPT_EPISODES_FIELD),
        SCRIPT_EPISODES_FIELD: merged,
    }


def _require_episode_number(item: Any, index: int) -> int:
    if not isinstance(item, dict):
        raise PipelineImportError(f"script: episodes[{index}] 不是对象，无法识别集号 {SCRIPT_EPISODE_FIELD!r}", [])
    ep = item.get(SCRIPT_EPISODE_FIELD)
    if not isinstance(ep, int) or isinstance(ep, bool):
        raise PipelineImportError(
            f"script: episodes[{index}] 缺少合法集号 {SCRIPT_EPISODE_FIELD!r}（契约 script 节，须为整数）", []
        )
    return ep


def _episode_number_of(item: Any) -> int | None:
    """库内已有集的集号；无法识别返回 None（该项原样保留，不丢弃既有数据）。"""
    if isinstance(item, dict):
        ep = item.get(SCRIPT_EPISODE_FIELD)
        if isinstance(ep, int) and not isinstance(ep, bool):
            return ep
    return None


# ---------------------------------------------------------------- characters / character_refs

def _prepare_character_set(art: ArtifactType, payload: dict, existing: dict | None, findings: list[str]) -> dict:
    art_name = art.name
    # 未知键查哪张表取决于 payload 形状：单角色对象是卡字段（characters 卡表 /
    # character_refs 的 asset.json 顶层表），已合并对象按文档顶层表查。
    if art_name == "characters":
        single_known = _CHARACTER_CARD_FIELDS
    else:
        single_known = _CONTRACT_TOP_LEVEL[art_name]
    cards, meta, merged_shape = _interpret_character_payload(art_name, payload, findings)
    known = (single_known if not merged_shape else _CONTRACT_TOP_LEVEL[art_name] | {CHARACTER_ARRAY_FIELD})
    _warn_unknown_keys(known, payload, art_name, findings)
    existing_cards: list | None = None
    existing_meta: dict = {}
    if isinstance(existing, dict):
        raw = existing.get(CHARACTER_ARRAY_FIELD)
        if isinstance(raw, list):
            existing_cards = raw
            existing_meta = _without(existing, CHARACTER_ARRAY_FIELD)
        else:
            findings.append(
                f"{art_name}: 库内 existing 无法识别角色数组 {CHARACTER_ARRAY_FIELD}[]，按整行替换处理"
            )
    elif existing is not None:
        findings.append(f"{art_name}: 库内 existing 不是 JSON 对象，按整行替换处理")
    merged = _merge_by_key(existing_cards or [], cards, _character_identity_of)
    return {**existing_meta, **meta, CHARACTER_ARRAY_FIELD: merged}


def _interpret_character_payload(
    art_name: str, payload: dict, findings: list[str]
) -> tuple[dict[str, dict], dict, bool]:
    """把 payload 归一为 (新角色卡 {标识: 卡}, 文档级元数据, 是否已合并形状)。

    已合并对象（characters 为数组）→ 逐卡并入库；单角色对象（含标识字段）→
    自动包装，其字段是角色卡字段而非文档字段，不并入文档元数据。
    """
    raw = payload.get(CHARACTER_ARRAY_FIELD)
    if isinstance(raw, list):
        keyed: dict[str, dict] = {}
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or not _valid_identity(item):
                raise PipelineImportError(
                    f"{art_name}: {CHARACTER_ARRAY_FIELD}[{index}] 缺少角色标识字段 "
                    f"{CHARACTER_IDENTITY_FIELD!r}（契约 characters 节卡片无 id 字段，以 name 为标识）",
                    findings,
                )
            keyed[item[CHARACTER_IDENTITY_FIELD]] = item
        return keyed, _without(payload, CHARACTER_ARRAY_FIELD), True
    if _valid_identity(payload):
        return {payload[CHARACTER_IDENTITY_FIELD]: payload}, {}, False
    raise PipelineImportError(
        f"{art_name}: 无法识别的 content——既不是含 {CHARACTER_IDENTITY_FIELD!r} 的单角色对象，"
        f"也不是含 {CHARACTER_ARRAY_FIELD}[] 的已合并对象",
        findings,
    )


def _valid_identity(item: dict) -> bool:
    identity = item.get(CHARACTER_IDENTITY_FIELD)
    return isinstance(identity, str) and bool(identity.strip())


def _character_identity_of(item: Any) -> str | None:
    if isinstance(item, dict) and _valid_identity(item):
        return item[CHARACTER_IDENTITY_FIELD]
    return None


# ---------------------------------------------------------------- 公共小件

def _merge_by_key(existing_items: list, keyed_new: dict, key_of) -> list:
    """既有项原样保留；同键者被新项整对象替换且位置不变；新键追加在后。

    既有项无法识别键（key_of → None）时原样保留，绝不丢弃库内数据。
    """
    merged: list = []
    replaced: set = set()
    for item in existing_items:
        key = key_of(item)
        if key is not None and key in keyed_new:
            merged.append(keyed_new[key])
            replaced.add(key)
        else:
            merged.append(item)
    for key, item in keyed_new.items():
        if key not in replaced:
            merged.append(item)
    return merged


def _warn_unknown_keys(known: frozenset[str] | None, payload: dict, art_name: str, findings: list[str]) -> None:
    if known is None:
        return
    for key in payload:
        if key not in known:
            findings.append(
                f"{art_name}: 顶层未知键 {key!r} 不在契约字段表，按宽容条款保留不拒绝"
            )


def _without(payload: dict, key: str) -> dict:
    return {k: v for k, v in payload.items() if k != key}

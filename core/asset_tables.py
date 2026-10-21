"""资产表（草稿区）schema 与幂等同步合并。

对照设计 docs/superpowers/specs/2026-10-21-script-asset-pipeline-design.md §4。
原则：表是草稿、库是资产。同步**只增不改不删**——已有形象、非空字段、
图片一律不碰；表中删掉的行不删库（删减由用户在资产库操作）。
merge_asset_tables 是纯函数：existing 是资产库投影，输出 plan 由调用方执行。
"""
from __future__ import annotations

import asyncio
import json
import unicodedata
from typing import Any, Callable

from pydantic import BaseModel, Field

from .story_analysis import extract_json


class LookRow(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class CharacterRow(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    bio: str = Field(default="", max_length=1500)
    looks: list[LookRow] = Field(default_factory=list)


class AssetTables(BaseModel):
    """meta.asset_tables 的 schema；角色=一条记录+形象子数组（存储归一，UI 负责平铺）。"""

    skill: dict = Field(default_factory=dict)
    updated_at: int = 0
    synced_at: int = 0
    characters: list[CharacterRow] = Field(default_factory=list)
    scenes: list[dict] = Field(default_factory=list)
    props: list[dict] = Field(default_factory=list)


def normalize_name(name: str) -> str:
    """归一化键：trim + 全角转半角 + 去内部空白 + 小写。同名即同一资产（别名归一入口）。"""
    half = unicodedata.normalize("NFKC", str(name or ""))
    return "".join(half.split()).lower()


def merge_asset_tables(existing: list[dict[str, Any]], tables: AssetTables) -> dict[str, Any]:
    """existing 投影形状：{node_id, kind, name, description, bio, looks: [{name, prompt}]}。

    返回 plan：create_assets / add_looks / fill_fields（只补空字段）/ merged（别名归并报告）/ untouched。
    """
    plan: dict[str, Any] = {
        "create_assets": [], "add_looks": [], "fill_fields": [], "merged": [], "untouched": [],
    }
    by_key = {normalize_name(item["name"]): item for item in existing}

    wanted: list[tuple[str, dict]] = []
    for row in tables.characters:
        wanted.append(("character", row.model_dump()))
    for row in tables.scenes:
        wanted.append(("scene", {"name": row.get("name", ""), "description": row.get("description", "")}))
    for row in tables.props:
        wanted.append(("prop", {"name": row.get("name", ""), "description": row.get("description", "")}))

    for kind, row in wanted:
        if not str(row.get("name") or "").strip():
            continue
        node = by_key.get(normalize_name(row["name"]))
        node = node if node and node.get("kind") == kind else None
        if node is None:
            looks = [{"name": lk["name"], "prompt": ""} for lk in row.get("looks", [])] if kind == "character" else []
            plan["create_assets"].append({
                "kind": kind, "name": row["name"].strip(),
                "description": row.get("description", ""), "bio": row.get("bio", ""), "looks": looks,
            })
            continue
        for field in ("description", "bio"):
            value = str(row.get(field) or "").strip()
            if value and not str(node.get(field) or "").strip():
                plan["fill_fields"].append({"node_id": node["node_id"], "field": field, "value": row[field]})
        have = {normalize_name(lk["name"]) for lk in node.get("looks", [])}
        for lk in row.get("looks", []):
            if normalize_name(lk["name"]) in have:
                plan["untouched"].append(lk["name"])
            else:
                plan["add_looks"].append({"node_id": node["node_id"], "kind": kind,
                                          "look": {"name": lk["name"].strip(), "prompt": ""}})

    # 归并报告：同一规范化名下的不同字面写法被表里的写法收敛（只报告，不改库）
    seen: dict[str, str] = {}
    for kind, row in wanted:
        canon = normalize_name(row.get("name", ""))
        if not canon:
            continue
        if canon in seen and seen[canon] != row["name"]:
            plan["merged"].append({"from": seen[canon], "into": row["name"]})
        seen.setdefault(canon, row["name"])
    for item in existing:
        canon = normalize_name(item["name"])
        if canon in seen and item["name"] != seen[canon]:
            plan["merged"].append({"from": item["name"], "into": seen[canon]})
    return plan


def build_parse_prompt(episode_text: str, known_names: list[str]) -> list[dict[str, str]]:
    """按集抽取的消息组装；known_names 让跨集同名沿用同一写法。"""
    known = "、".join(known_names[:80]) if known_names else "（无）"
    system = (
        "你是短剧资产抽取助手。只输出一个 JSON 对象。从剧本抽取："
        '{"characters": [{"name", "description"(一句身份), "bio"(外形小传:身高体材/脸型发型), '
        '"looks": [{"name"(服装/状态形象名)}]}], '
        '"scenes": [{"name", "description"(一句功能)}], "props": [{"name", "description"}]}。'
        "规则：同一角色的多个形象拆成多条 looks，只写差异（服装/发型）；不编造剧本没有的条目；"
        "禁止输出图片、URL，或声称已生成任何图片。"
    )
    user = f"【已见过的资产名（同名请沿用同一写法）】{known}\n\n【本集剧本】\n{episode_text}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def parse_asset_tables(
    episodes: list[dict[str, Any]],
    llm: Any,
    *,
    known: list[str] | None = None,
    on_event: Callable[[dict], None] | None = None,
    cancel: asyncio.Event | None = None,
) -> dict[str, Any]:
    """按集分块抽取；跨集同名（归一化后）合并——characters 并 looks 去重，文本字段取首个非空。"""
    emit = on_event or (lambda event: None)
    known_norm = {normalize_name(n) for n in (known or [])}
    characters: dict[str, dict] = {}
    scenes: dict[str, dict] = {}
    props: dict[str, dict] = {}
    for episode in episodes:
        if cancel is not None and cancel.is_set():
            break
        emit({"type": "parse_progress", "episode_no": episode["no"]})
        messages = build_parse_prompt(episode["text"], sorted(known_norm))
        try:
            data = extract_json(await llm.simple(messages))
        except (ValueError, json.JSONDecodeError):
            continue  # 单集失败不拖垮整表（结果里缺这集，UI 有进度可感知）
        for row in data.get("characters", []):
            key = normalize_name(row.get("name", ""))
            if not key:
                continue
            slot = characters.setdefault(key, {"name": row["name"], "description": "", "bio": "", "looks": []})
            for field in ("description", "bio"):
                if not slot[field] and row.get(field):
                    slot[field] = row[field]
            have = {normalize_name(lk.get("name", "")) for lk in slot["looks"]}
            for lk in row.get("looks", []):
                if lk.get("name") and normalize_name(lk["name"]) not in have:
                    slot["looks"].append({"name": lk["name"]})
                    have.add(normalize_name(lk["name"]))
            known_norm.add(key)
        for group, target in (("scenes", scenes), ("props", props)):
            for row in data.get(group, []):
                key = normalize_name(row.get("name", ""))
                if key and key not in target:
                    target[key] = {"name": row["name"], "description": row.get("description", "")}
                if key:
                    known_norm.add(key)
    tables = AssetTables(
        characters=list(characters.values()),
        scenes=list(scenes.values()),
        props=list(props.values()),
    )
    result = tables.model_dump()
    emit({"type": "parse_done", "tables": result})
    return result

"""prepare() 导入合并核心测试（Task 2 brief Step 1）。

字段名断言严格对齐 docs/pipeline/data-contract.md：
- script 集号 = episodes[].ep（契约 script 节 episodes[] 嵌套首列）
- characters 角色标识 = name（契约 characters 节 characters[] 卡字段表无显式 id）
- character_refs 落库形状 = {"characters": [...]}（契约"存储与导入约定"定案）
"""
from __future__ import annotations

import json

import pytest

from core.pipeline_artifacts import ARTIFACTS, get
from core.pipeline_import import PipelineImportError, prepare


# ---------------------------------------------------------------- outline

def test_outline_dict_passthrough_with_empty_findings():
    payload = {"source": "书名", "params": {"episodes": 12}, "beats": []}
    content, findings = prepare(get("outline"), payload, None)
    assert content == payload
    assert findings == []


# ---------------------------------------------------------------- characters

def test_characters_single_card_is_wrapped_into_merged_shape():
    card = {"name": "张三", "importance": "protagonist", "oneLiner": "主角"}
    content, findings = prepare(get("characters"), card, None)
    assert content == {"characters": [card]}
    assert findings == []


def test_characters_merged_object_merges_into_existing_by_identity():
    card_a = {"name": "阿禾", "v": 1}
    card_b = {"name": "老周", "v": 1}
    existing = {"characters": [card_a, card_b]}
    card_b2 = {"name": "老周", "v": 2, "importance": "major"}
    card_c = {"name": "小满", "v": 1}
    payload = {"characters": [card_b2, card_c]}
    content, findings = prepare(get("characters"), payload, existing)
    assert findings == []
    # 他角色保留（逐字节）、同标识整对象替换、新角色追加
    assert content["characters"][0] is card_a
    assert content["characters"][1] == card_b2
    assert content["characters"][2] == card_c


def test_characters_single_card_merges_into_existing():
    existing = {"source": "书名", "characters": [{"name": "阿禾", "importance": "protagonist"}]}
    card = {"name": "老周", "importance": "major"}
    content, findings = prepare(get("characters"), card, existing)
    assert findings == []
    assert content["source"] == "书名"
    assert content["characters"] == [
        {"name": "阿禾", "importance": "protagonist"},
        card,
    ]


def test_characters_uninterpretable_payload_raises():
    with pytest.raises(PipelineImportError):
        prepare(get("characters"), {"summary": "既无 name 也无 characters 数组"}, None)


# ---------------------------------------------------------------- script

def test_script_without_existing_is_stored_as_is():
    payload = {"source": "剧名", "episodes": [{"ep": 1, "hook": "开场钩子"}]}
    content, findings = prepare(get("script"), payload, None)
    assert content == payload
    assert findings == []


def test_script_merges_by_episode_number():
    ep_a = {"ep": 1, "hook": "A"}
    ep_b = {"ep": 2, "hook": "B"}
    existing = {"source": "剧名", "episodes": [ep_a, ep_b]}
    ep_b2 = {"ep": 2, "hook": "B'", "cliff": "新悬念"}
    ep_c = {"ep": 3, "hook": "C"}
    payload = {"source": "剧名", "episodes": [ep_b2, ep_c]}
    content, findings = prepare(get("script"), payload, existing)
    assert findings == []
    # 未出现的集逐字节保留、同集号整集替换、新集追加
    assert content["episodes"][0] is ep_a
    assert content["episodes"][1] == ep_b2
    assert content["episodes"][2] == ep_c
    assert content["source"] == "剧名"


def test_script_payload_missing_episodes_array_raises():
    with pytest.raises(PipelineImportError) as excinfo:
        prepare(get("script"), {"source": "剧名"}, None)
    assert excinfo.value.message
    assert isinstance(excinfo.value.findings, list)


def test_script_episode_item_without_ep_raises():
    with pytest.raises(PipelineImportError):
        prepare(get("script"), {"source": "剧名", "episodes": [{"hook": "缺集号"}]}, None)


def test_script_existing_unrecognizable_replaces_whole_row_with_warning():
    # 契约"存储与导入约定"：非预期结构（无法识别集数组）时整行替换并记录告警
    existing = {"episodes": "不是数组"}
    payload = {"source": "剧名", "episodes": [{"ep": 1, "hook": "A"}]}
    content, findings = prepare(get("script"), payload, existing)
    assert content == payload
    assert findings


# ---------------------------------------------------------------- staging

def test_staging_non_dict_payload_raises():
    with pytest.raises(PipelineImportError):
        prepare(get("staging"), ["not", "a", "dict"], None)


# ---------------------------------------------------------------- 宽容条款

_UNKNOWN_KEY_PAYLOADS = {
    "outline": {"source": "书名"},
    "script": {"source": "剧名", "episodes": [{"ep": 1}]},
    "characters": {"name": "张三"},
    "art": {"source": "剧名", "scenes": []},
    "staging": {"source": "剧名", "lang": "zh", "locations": [], "scenes": []},
    "storyboard": {"source": "剧名", "episodes": []},
    "character_refs": {"name": "张三", "layers": {}, "outfits": {}},
}


@pytest.mark.parametrize("art", ARTIFACTS, ids=lambda a: a.name)
def test_unknown_top_level_key_warns_but_still_stores(art):
    payload = {**_UNKNOWN_KEY_PAYLOADS[art.name], "totally_unknown": 1}
    content, findings = prepare(art, payload, None)
    assert len(findings) == 1
    assert "totally_unknown" in findings[0]
    # 不拒绝：未知键仍进入落库 content
    assert "totally_unknown" in json.dumps(content, ensure_ascii=False)


# ---------------------------------------------------------------- character_refs

def test_character_refs_single_asset_wraps_per_contract_shape():
    asset = {"name": "阿禾", "layers": {"identity": {}}, "outfits": {}}
    content, findings = prepare(get("character_refs"), asset, None)
    assert content == {"characters": [asset]}
    assert findings == []


def test_character_refs_merge_keeps_other_characters():
    existing = {"characters": [{"name": "阿禾", "layers": {"identity": {}}}]}
    asset = {"name": "老周", "layers": {"identity": {}}}
    content, findings = prepare(get("character_refs"), asset, existing)
    assert findings == []
    assert content["characters"] == [{"name": "阿禾", "layers": {"identity": {}}}, asset]


# ---------------------------------------------------------------- 异常序列化

def test_import_error_serializes_to_api_422_shape():
    error = PipelineImportError("剧本缺集数组", ["finding-1"])
    assert error.to_dict() == {"message": "剧本缺集数组", "findings": ["finding-1"]}
    assert error.message == "剧本缺集数组"
    assert error.findings == ["finding-1"]
    assert str(error) == "剧本缺集数组"

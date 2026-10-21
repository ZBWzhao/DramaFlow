"""资产表：名称归一化 + 同步合并纯函数（只增不改不删）。"""
from core.asset_tables import AssetTables, merge_asset_tables, normalize_name


def _tables(**kw):
    base = {"characters": [], "scenes": [], "props": []}
    base.update(kw)
    return AssetTables(**base)


def test_normalize_name_fullwidth_and_spaces():
    assert normalize_name("苏 晚") == normalize_name("苏晚")
    assert normalize_name("Ｌｉｌｙ") == "lily"
    assert normalize_name("  林　生 ") == "林生"


def test_sync_creates_missing_assets_with_looks():
    plan = merge_asset_tables([], _tables(
        characters=[{"name": "林生", "description": "丈夫", "bio": "约175cm", "looks": [{"name": "宴会便装"}]}],
        scenes=[{"name": "陈家客厅", "description": "主场景"}],
        props=[{"name": "茶杯", "description": "道具"}],
    ))
    assert [a["name"] for a in plan["create_assets"]] == ["林生", "陈家客厅", "茶杯"]
    assert plan["create_assets"][0]["looks"] == [{"name": "宴会便装", "prompt": ""}]
    assert plan["add_looks"] == [] and plan["fill_fields"] == []


def test_sync_adds_missing_look_and_fills_only_empty_fields():
    existing = [{"node_id": "n_1", "kind": "character", "name": "林生",
                 "description": "", "bio": "已有小传", "looks": [{"name": "宴会便装", "prompt": "p"}]}]
    plan = merge_asset_tables(existing, _tables(
        characters=[{"name": "林 生", "description": "新描述", "bio": "新小传",
                     "looks": [{"name": "宴会便装"}, {"name": "家居围裙"}]}],
    ))
    assert plan["create_assets"] == []
    assert plan["add_looks"] == [{"node_id": "n_1", "kind": "character", "look": {"name": "家居围裙", "prompt": ""}}]
    # 空字段补全、非空字段不动（description 空才补，bio 非空保留）
    assert {"node_id": "n_1", "field": "description", "value": "新描述"} in plan["fill_fields"]
    assert all(f["field"] != "bio" for f in plan["fill_fields"])
    assert plan["untouched"] == ["宴会便装"]


def test_sync_reports_alias_merge_and_never_deletes():
    existing = [
        {"node_id": "n_1", "kind": "character", "name": "苏 晚", "description": "a", "bio": "", "looks": []},
        {"node_id": "n_2", "kind": "character", "name": "路人甲", "description": "b", "bio": "", "looks": []},
    ]
    plan = merge_asset_tables(existing, _tables(
        characters=[{"name": "苏晚", "description": "a", "bio": "", "looks": []}],
    ))
    assert plan["merged"] == [{"from": "苏 晚", "into": "苏晚"}]
    # 表里删掉的行不删库：路人甲既不建也不删
    assert plan["create_assets"] == []
    assert all(f["node_id"] != "n_2" for f in plan["fill_fields"])


def test_kind_mismatch_creates_new_asset():
    """库里的同名节点 kind 不同（角色 vs 道具）不算同一资产，照常创建。"""
    existing = [{"node_id": "n_9", "kind": "prop", "name": "林生", "description": "x", "bio": "", "looks": []}]
    plan = merge_asset_tables(existing, _tables(
        characters=[{"name": "林生", "description": "丈夫", "bio": "", "looks": []}],
    ))
    assert [a["kind"] for a in plan["create_assets"]] == ["character"]


def test_parse_merges_same_character_across_episodes():
    import asyncio

    from core.asset_tables import parse_asset_tables

    class FakeLLM:
        def __init__(self, replies):
            self.replies = list(replies)

        async def simple(self, messages):
            return self.replies.pop(0)

    r1 = '{"characters": [{"name": "林生", "description": "丈夫", "bio": "b1", "looks": [{"name": "宴会便装"}]}], "scenes": [{"name": "陈家客厅", "description": "s"}], "props": []}'
    r2 = '{"characters": [{"name": "林 生", "description": "", "bio": "b2", "looks": [{"name": "宴会便装"}, {"name": "家居围裙"}]}], "scenes": [], "props": [{"name": "茶杯", "description": "p"}]}'
    events = []
    tables = asyncio.run(parse_asset_tables(
        [{"no": 1, "text": "第一集"}, {"no": 2, "text": "第二集"}],
        FakeLLM([r1, r2]), on_event=events.append,
    ))
    assert [e["type"] for e in events] == ["parse_progress", "parse_progress", "parse_done"]
    assert len(tables["characters"]) == 1
    row = tables["characters"][0]
    assert row["description"] == "丈夫" and row["bio"] == "b1"  # 首个非空
    assert [lk["name"] for lk in row["looks"]] == ["宴会便装", "家居围裙"]  # 跨集去重合并
    assert len(tables["props"]) == 1


def test_parse_skips_bad_json_episode_but_keeps_rest():
    import asyncio

    from core.asset_tables import parse_asset_tables

    class FakeLLM:
        def __init__(self, replies):
            self.replies = list(replies)

        async def simple(self, messages):
            return self.replies.pop(0)

    good = '{"characters": [{"name": "林生", "description": "丈夫", "bio": "", "looks": []}], "scenes": [], "props": []}'
    tables = asyncio.run(parse_asset_tables(
        [{"no": 1, "text": "第一集"}, {"no": 2, "text": "第二集"}],
        FakeLLM(["完全不是JSON", good]),
    ))
    assert len(tables["characters"]) == 1  # 坏的那集被跳过，好集照常入库


def test_parse_cancel_stops_between_episodes():
    """取消语义 = 卡间检查：第一集后置位，第二集不再发起调用。"""
    import asyncio

    from core.asset_tables import parse_asset_tables

    cancel = asyncio.Event()
    good = '{"characters": [{"name": "林生", "description": "丈夫", "bio": "", "looks": []}], "scenes": [], "props": []}'

    class CancelAfterFirst:
        def __init__(self):
            self.calls = 0

        async def simple(self, messages):
            self.calls += 1
            if self.calls == 1:
                cancel.set()
            return good

    events = []
    llm = CancelAfterFirst()
    tables = asyncio.run(parse_asset_tables(
        [{"no": 1, "text": "第一集"}, {"no": 2, "text": "第二集"}],
        llm, on_event=events.append, cancel=cancel,
    ))
    assert llm.calls == 1  # 第二集没调用
    assert [e["type"] for e in events] == ["parse_progress", "parse_done"]
    assert len(tables["characters"]) == 1  # 第一集结果保留

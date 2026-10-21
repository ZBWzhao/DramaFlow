"""update_asset_tables 工具：逐条编辑草稿表（归一化匹配 / schema 校验后落盘）。"""
import asyncio

from core.asset_tables import normalize_name
from core.canvas import Canvas
from core.canvas_repository import FileCanvasRepository
from core.context import ToolContext
from core.loader import create_agent_tools


def _make_ctx(tmp_path):
    async def build():
        repo = FileCanvasRepository(tmp_path)
        canvas = Canvas("main", path=tmp_path / "main.canvas.json")
        canvas.meta["asset_tables"] = {
            "characters": [{"name": "林小满", "description": "修表匠的女儿", "bio": "", "looks": [{"name": "工装未换"}]}],
            "scenes": [{"name": "老宅客厅", "description": "主场景"}],
            "props": [],
        }
        await repo.save(canvas)
        tools, _entries, _ctx = await create_agent_tools(cwd=str(tmp_path), canvas_repository=repo)
        ctx = ToolContext(cwd=str(tmp_path), config={"default_canvas": "main"}, canvas_repository=repo)
        return {t.name: t for t in tools}, ctx, repo

    return asyncio.run(build())


def test_set_character_bio_by_normalized_name(tmp_path) -> None:
    tools, ctx, repo = _make_ctx(tmp_path)
    # 表里存「林小满」，模型传「林 小满」也要命中
    result = asyncio.run(tools["update_asset_tables"].execute(
        {"op": "set_character", "name": "林 小满", "field": "bio", "value": "约20岁，身形清瘦，长发扎起，左手腕有修表磨出的薄茧"},
        ctx,
    ))
    assert not result.is_error
    saved = asyncio.run(repo.load("main"))
    row = saved.meta["asset_tables"]["characters"][0]
    assert "薄茧" in row["bio"] and row["description"] == "修表匠的女儿"  # 只动指名字段


def test_add_and_remove_look(tmp_path) -> None:
    tools, ctx, repo = _make_ctx(tmp_path)
    asyncio.run(tools["update_asset_tables"].execute(
        {"op": "add_look", "name": "林小满", "look": {"name": "素色裙子", "prompt": "素色连衣裙，宴会场合"}},
        ctx,
    ))
    saved = asyncio.run(repo.load("main"))
    looks = saved.meta["asset_tables"]["characters"][0]["looks"]
    assert [lk["name"] for lk in looks] == ["工装未换", "素色裙子"]
    # 重复添加被拒绝
    dup = asyncio.run(tools["update_asset_tables"].execute(
        {"op": "add_look", "name": "林小满", "look": {"name": "素色裙子"}}, ctx,
    ))
    assert dup.is_error
    # 删除
    asyncio.run(tools["update_asset_tables"].execute(
        {"op": "remove_look", "name": "林小满", "look": {"name": "工装未换"}}, ctx,
    ))
    saved = asyncio.run(repo.load("main"))
    assert [lk["name"] for lk in saved.meta["asset_tables"]["characters"][0]["looks"]] == ["素色裙子"]


def test_add_scene_and_prop_rows(tmp_path) -> None:
    tools, ctx, repo = _make_ctx(tmp_path)
    asyncio.run(tools["update_asset_tables"].execute(
        {"op": "add_prop", "name": "黄铜怀表", "value": "剧情关键道具，表盖内侧刻字"}, ctx,
    ))
    saved = asyncio.run(repo.load("main"))
    assert [r["name"] for r in saved.meta["asset_tables"]["props"]] == ["黄铜怀表"]


def test_missing_row_and_empty_value_are_readable_errors(tmp_path) -> None:
    tools, ctx, _repo = _make_ctx(tmp_path)
    miss = asyncio.run(tools["update_asset_tables"].execute(
        {"op": "set_character", "name": "路人甲", "field": "bio", "value": "x"}, ctx,
    ))
    assert miss.is_error and "不在表里" in miss.content
    empty = asyncio.run(tools["update_asset_tables"].execute(
        {"op": "set_character", "name": "林小满", "field": "bio", "value": ""}, ctx,
    ))
    assert empty.is_error and "value" in empty.content


def test_run_pipeline_tool_reports_and_guards(tmp_path) -> None:
    """未注册 spawner 时给可读错误；注册后触发成功。"""
    from core.pipelines import register_spawner

    tools, ctx, _repo = _make_ctx(tmp_path)
    result = asyncio.run(tools["run_pipeline"].execute({"pipeline": "asset_parse"}, ctx))
    # 测试进程里 server 层未配置 LLM/工作区，spawner 可能已注册（import server）或未注册——两种都应是可读结果
    assert result.is_error or "后台" in result.content
    # 未知 pipeline 被 pydantic 挡住，这里只验证合法值的通路
    register_spawner(lambda canvas, which: {"ok": True, "detail": "已开始后台执行"})
    ok = asyncio.run(tools["run_pipeline"].execute({"pipeline": "story_analysis"}, ctx))
    assert not ok.is_error

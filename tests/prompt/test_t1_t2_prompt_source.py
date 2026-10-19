"""T1/T2：生成正文来源（人改优先）与引用闭包断言。

对应 ``docs/提示词侧下一步工作单.md`` 的 T1、T2：
- T1：``GenerateRequest.prompt`` / ``data.prompt`` / ``rendered_by=human`` 必须真的
  被使用；首帧分支以前完全忽略人改正文。
- T2：正文里的 ``@名字`` / ``{{ref N}}`` 必须能在**引用入边集合**里找到，
  否则 422 拒绝发送，不能静默忽略。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from conftest import ASSET_NAME

HUMAN_TEXT = "人改正文：他抬手，停半秒，再放下。"


# --- T1：首帧模式不再忽略人改正文 -----------------------------------------


def test_first_frame_uses_saved_human_text(env) -> None:
    env.set_keyframe()
    env.set_human_text(HUMAN_TEXT)

    job = env.generate()["job"]

    assert job["mode"] == "first_frame"
    assert job["prompt"] == HUMAN_TEXT, "首帧模式必须使用已保存的人改正文"


def test_first_frame_body_prompt_beats_saved_human_text(env) -> None:
    env.set_keyframe()
    env.set_human_text("旧的人改正文")

    job = env.generate(prompt="这次临时传入的正文")["job"]

    assert job["prompt"] == "这次临时传入的正文"


def test_first_frame_server_render_does_not_claim_references(env) -> None:
    env.set_keyframe()

    job = env.generate()["job"]

    prompt = job["prompt"]
    assert "按参考素材" not in prompt, "首帧模式只发首帧，正文不该宣称有参考图"
    assert "@" + ASSET_NAME in prompt, "主体仍应带 @名字，方便对位"
    assert job["mode"] == "first_frame"
    assert job["reference_count"] == 0


def test_references_mode_uses_human_text_and_materializes(env) -> None:
    env.set_human_text("人改：@林直人 抬手，然后放下。")

    job = env.generate()["job"]

    assert job["mode"] == "references"
    assert "人改：" in job["prompt"]
    assert "图片1林直人" in job["prompt"], "参考图模式仍应把 @名字 换成图片序号"


def test_references_mode_human_text_without_mentions(env) -> None:
    env.set_human_text(HUMAN_TEXT)

    job = env.generate()["job"]

    assert job["mode"] == "references"
    assert HUMAN_TEXT in job["prompt"]


# --- T2：引用闭包硬断言 ---------------------------------------------------


def test_dangling_mention_is_rejected(env) -> None:
    env.set_human_text("参考 @不存在的人 站好。")

    # 传 duration 会跳过「未通过硬校验」的 422（§2.3）：闭包断言必须自己挡住。
    with pytest.raises(HTTPException) as caught:
        env.generate(duration=5)

    detail = caught.value.detail
    assert caught.value.status_code == 422
    assert "@不存在的人" in detail["dangling"]


def test_dangling_mention_also_caught_by_lint_without_duration(env) -> None:
    env.set_human_text("参考 @不存在的人 站好。")

    with pytest.raises(HTTPException) as caught:
        env.generate()

    # 不传 duration 时先被体检拦下（refs.dangling_mention），同样不发请求。
    assert caught.value.status_code == 422
    assert caught.value.detail["message"] == "片段未通过硬校验，先修好再生成"


def test_mention_without_any_reference_edge_is_rejected(env) -> None:
    env.drop_ref_edges()

    with pytest.raises(HTTPException) as caught:
        env.generate(prompt="@林直人 抬手。", duration=5)

    assert caught.value.status_code == 422
    assert "@林直人" in caught.value.detail["dangling"]


def test_out_of_range_ref_slot_is_rejected(env) -> None:
    env.set_human_text("用 {{ref 9}} 这张图。")

    with pytest.raises(HTTPException) as caught:
        env.generate()

    assert caught.value.status_code == 422
    assert "{{ref 9}}" in caught.value.detail["dangling"]


def test_valid_ref_slot_passes_and_is_materialized(env) -> None:
    env.set_human_text("用 {{ref 1}} 这张图。")

    job = env.generate()["job"]

    assert "{{ref" not in job["prompt"]
    assert "图片1林直人" in job["prompt"]


def test_asset_name_from_edge_is_not_dangling(env) -> None:
    env.set_human_text("@林直人 抬手。")

    job = env.generate()["job"]

    assert "@林直人" not in job["prompt"]

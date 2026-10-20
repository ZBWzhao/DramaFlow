"""回归：一致性链——特写以全身为参考、locked 真锁、based_on 凭据落库。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException

from core.canvas import Canvas, Node
from server.studio import AssetGenerateRequest, _look_request


def _canvas_with_lin() -> Canvas:
    c = Canvas("t", Path(__file__).parent / "_tmp-lookchain.canvas.json")
    asset = Node(
        id="a_lin",
        type="asset",
        label="林越",
        data={
            "kind": "character",
            "name": "林越",
            "looks": [
                {"id": "L1", "name": "正面全身", "prompt": "", "image_path": "assets/generated/l1.png",
                 "locked": False, "appearances": []},
                {"id": "L2", "name": "正脸特写", "prompt": "", "image_path": "", "locked": False,
                 "appearances": []},
            ],
            "active_look_id": "L1",
        },
        status="ready",
    )
    c.nodes[asset.id] = asset
    return c


def main() -> None:
    c = _canvas_with_lin()
    target = c.require("a_lin")
    looks = [dict(look) for look in target.data["looks"]]

    # 1) 生成特写（L2）：默认 based_on=L1 → 请求带全身参考图
    request, based_on_id, ref_path = _look_request(
        c, target, [dict(x) for x in looks], 1, AssetGenerateRequest(look_id="L2")
    )
    assert based_on_id == "L1" and ref_path == "assets/generated/l1.png", (based_on_id, ref_path)
    assert request.get("references") == ["assets/generated/l1.png"], request

    # 2) 生成首项自身（L1）：无参考
    request1, _, ref1 = _look_request(
        c, target, [dict(x) for x in looks], 0, AssetGenerateRequest(look_id="L1")
    )
    assert ref1 == "" and "references" not in request1, "生成锚点自身不应带参考"

    # 3) locked 真锁：锁 L2 后再生成 → 409
    locked_looks = [dict(x) for x in looks]
    locked_looks[1]["locked"] = True
    try:
        _look_request(c, target, locked_looks, 1, AssetGenerateRequest(look_id="L2"))
        raise AssertionError("locked 形象生成应被拒绝")
    except HTTPException as exc:
        assert exc.status_code == 409, exc.status_code

    # 4) 显式 based_on 优先于默认；参考对象无图则退化为无参考
    req4, _, ref4 = _look_request(
        c, target, [dict(x) for x in looks], 0, AssetGenerateRequest(look_id="L1", based_on="L2")
    )
    assert ref4 == "" and "references" not in req4, "L2 没有图，不能当参考"

    Path(__file__).parent.joinpath("_tmp-lookchain.canvas.json").unlink(missing_ok=True)
    print("look_chain_invariants PASS")


if __name__ == "__main__":
    main()

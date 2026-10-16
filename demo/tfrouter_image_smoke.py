"""真实 TF-router 图片生成冒烟测试。

默认不会发起请求。只有显式传入 ``--live`` 且设置 TF_ROUTER_API_KEY 时，
才会产生一次真实调用和费用：

    python demo/tfrouter_image_smoke.py --live

结果会下载到 workspace/assets/generated/，并检查不是 Mock、文件非空且为图片。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.media import TFRouterProvider  # noqa: E402


def _load_local_env() -> None:
    """让独立冒烟脚本复用启动脚本的本地密钥配置，不打印密钥。"""
    path = ROOT / ".env.production"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([^#=\s]+)\s*=\s*(.*?)\s*$", line)
        if match and match.group(1) not in os.environ:
            value = match.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[match.group(1)] = value


class _TracingTFRouter(TFRouterProvider):
    def _json(self, path: str, body: dict) -> dict:
        value = super()._json(path, body)
        if "generate" in path:
            print("create_response=" + json.dumps({"keys": sorted(value), "data": value.get("data")}, ensure_ascii=False))
        elif "Status" in path:
            print("status_response=" + json.dumps({"keys": sorted(value), "status": value.get("status"), "data_type": type(value.get("data")).__name__}, ensure_ascii=False))
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description="TF-router 真实图片生成冒烟测试")
    parser.add_argument("--live", action="store_true", help="确认允许发起一次真实付费请求")
    parser.add_argument("--prompt", default="雨夜便利店门口，一把透明雨伞放在暖黄色灯光下，写实电影感")
    parser.add_argument("--model", default="doubao-seedream-5.0-Pro")
    parser.add_argument("--size", default="2K")
    parser.add_argument("--debug", action="store_true", help="只输出 Provider 响应结构，不输出密钥")
    parser.add_argument("--resume-operation-id", default="", help="只恢复查询已有任务，不创建新任务")
    args = parser.parse_args()

    _load_local_env()
    key = os.environ.get("TF_ROUTER_API_KEY", "").strip()
    if not args.live:
        print("未执行真实请求：请显式加 --live；该命令可能产生 Provider 费用。")
        return 2
    if not key:
        print("未执行真实请求：TF_ROUTER_API_KEY 未配置。")
        return 2

    provider = _TracingTFRouter(key) if args.debug else TFRouterProvider(key)
    request = {
        "model": args.model,
        "prompt": args.prompt,
        "size": args.size,
        "ratio": "16:9",
    }
    result = (
        provider.resume("image", args.resume_operation_id, request, ROOT / "workspace")
        if args.resume_operation_id
        else provider.generate("image", request, ROOT / "workspace")
    )
    target = ROOT / "workspace" / result.path
    if result.mock or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(f"真实图片校验失败：{result}")
    magic = target.read_bytes()[:16]
    valid = magic.startswith(b"\x89PNG\r\n\x1a\n") or magic.startswith(b"\xff\xd8\xff") or magic.startswith(b"RIFF")
    if not valid:
        raise RuntimeError(f"下载文件不是受支持的图片格式：{target}")
    print(f"PASS TF-router image provider={result.provider} mock={result.mock}")
    print(f"path={result.path} bytes={target.stat().st_size} mime={result.mime}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

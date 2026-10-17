"""启动 Web 服务（前后端一体）。

    python run.py                        # http://127.0.0.1:8000（Mock 媒体，不花钱）
    python run.py --port 9000
    python run.py --env-file .env.production   # 载入真实模型凭据

媒体默认走 Mock：没显式给凭据时整个系统不会产生任何费用。给了凭据之后也还是
Mock 生效，要么在制片台里点「真模型」，要么再传 --media-provider tfrouter 起步。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402


def load_env_file(path: Path) -> int:
    """把 KEY=VALUE 读进环境变量，返回载入条数。

    已存在的环境变量优先（命令行/系统环境可以覆盖文件），这样排查线上问题时
    临时 export 一个变量就能压过文件里的值。
    """
    if not path.is_file():
        raise SystemExit(f"env 文件不存在：{path}")
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = value
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    parser.add_argument("--env-file", default="", help="载入凭据文件，如 .env.production")
    parser.add_argument(
        "--media-provider",
        default="",
        choices=["", "mock", "tfrouter"],
        help="起步时用哪个媒体 Provider（默认 mock，界面上还能再切）",
    )
    args = parser.parse_args()

    # 必须赶在建 registry 之前完成，因为 MediaRegistry 在 import 时读环境变量。
    if args.env_file:
        loaded = load_env_file((ROOT / args.env_file).resolve())
        print(f"已载入 {loaded} 个环境变量（{args.env_file}）")
    if args.media_provider:
        os.environ["MEDIA_PROVIDER"] = args.media_provider

    print(f"toonflow-py-lab  →  http://{args.host}:{args.port}")
    uvicorn.run("server.app:app", host=args.host, port=args.port, log_level="info", reload=args.reload)


if __name__ == "__main__":
    main()

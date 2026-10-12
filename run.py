"""启动 Web 服务（前后端一体）。

    python run.py            # http://127.0.0.1:8000
    python run.py --port 9000

服务起来后浏览器打开对应地址即可；前端在 web/，静态文件由 FastAPI 直接托管。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    args = parser.parse_args()

    print(f"toonflow-py-lab  →  http://{args.host}:{args.port}")
    uvicorn.run("server.app:app", host=args.host, port=args.port, log_level="info", reload=args.reload)


if __name__ == "__main__":
    main()

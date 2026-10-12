"""以 Redis + PostgreSQL 完整后端启动 DramaFlow。

先启动基础服务：
    docker compose -f compose.postgres.yml up -d

再启动应用：
    python run_full.py

这里使用 setdefault，便于部署时通过外部环境变量覆盖连接地址和连接池配置。
"""

from __future__ import annotations

import os


FULL_BACKEND_DEFAULTS = {
    # Redis：实时事件、运行态、Follow-up 与结构化问答。
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "EVENT_STORE_BACKEND": "redis",
    "RUN_STATE_STORE_BACKEND": "redis",
    "QUESTION_STORE_BACKEND": "redis",
    # PostgreSQL：副作用账本、审批、媒体任务与画布业务事实。
    "DATABASE_URL": "postgresql://postgres:postgres@127.0.0.1:5432/dramaflow",
    "INVOCATION_STORE_BACKEND": "postgres",
    "MEDIA_JOB_STORE_BACKEND": "postgres",
    "CANVAS_STORE_BACKEND": "postgres",
    # 本地完整演示仍使用内嵌 Worker；多进程部署可覆盖为 false。
    "MEDIA_WORKER_EMBEDDED": "true",
}


def configure_full_backends() -> None:
    for name, value in FULL_BACKEND_DEFAULTS.items():
        os.environ.setdefault(name, value)


def main() -> None:
    configure_full_backends()
    from run import main as run_main

    run_main()


if __name__ == "__main__":
    main()

"""PostgreSQL adapter contract checks that do not require a live database."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.invocation_store import (
    ApprovalStore,
    PostgresApprovalStore,
    PostgresToolInvocationStore,
    ToolInvocationStore,
    create_invocation_stores,
)
from core.media_job_store import MediaJobStore, PostgresMediaJobStore, create_media_job_store
from core.canvas_repository import CanvasRepository, PostgresCanvasRepository, create_canvas_repository


def main() -> None:
    keys = ("DATABASE_URL", "INVOCATION_STORE_BACKEND", "MEDIA_JOB_STORE_BACKEND", "CANVAS_STORE_BACKEND")
    old = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["DATABASE_URL"] = "postgresql://user:pass@127.0.0.1:5432/dramaflow"
        os.environ["INVOCATION_STORE_BACKEND"] = "postgres"
        os.environ["MEDIA_JOB_STORE_BACKEND"] = "postgres"
        os.environ["CANVAS_STORE_BACKEND"] = "postgres"
        invocations, approvals = create_invocation_stores()
        jobs = create_media_job_store()
        canvases = create_canvas_repository(ROOT / "workspace")
        assert isinstance(invocations, PostgresToolInvocationStore)
        assert isinstance(invocations, ToolInvocationStore)
        assert isinstance(approvals, PostgresApprovalStore)
        assert isinstance(approvals, ApprovalStore)
        assert isinstance(jobs, PostgresMediaJobStore)
        assert isinstance(jobs, MediaJobStore)
        assert isinstance(canvases, PostgresCanvasRepository)
        assert isinstance(canvases, CanvasRepository)

        schema = (ROOT / "migrations" / "0001_durable_state.sql").read_text("utf-8")
        required = {
            "tool_invocations", "approval_requests", "media_jobs", "projects", "canvases",
            "sessions", "session_entries", "media_assets", "outbox_events",
        }
        assert all(f"CREATE TABLE IF NOT EXISTS {table}" in schema for table in required)
        assert "idempotency_key TEXT NOT NULL UNIQUE" in schema
        assert "revision BIGINT NOT NULL DEFAULT 0" in schema
        print("PASS: PostgreSQL stores implement their protocols")
        print("PASS: durable schema contains idempotency, canvas revision and outbox boundaries")
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    main()

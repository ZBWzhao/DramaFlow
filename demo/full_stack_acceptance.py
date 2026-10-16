"""Smoke-test a running full-backend DramaFlow deployment.

Usage:
    python demo/full_stack_acceptance.py
    python demo/full_stack_acceptance.py http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import sys
import urllib.request


BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")


def request(path: str, *, method: str = "GET", body: dict | None = None) -> dict:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    health = request("/api/health")
    expected = {
        "event_store": "RedisStreamEventStore",
        "run_state_store": "RedisRunStateStore",
        "question_store": "RedisQuestionStateStore",
        "invocation_store": "PostgresToolInvocationStore",
        "approval_store": "PostgresApprovalStore",
        "canvas_repository": "PostgresCanvasRepository",
        "session_repository": "PostgresSessionRepository",
        "production_run_repository": "PostgresProductionRunRepository",
        "media_job_store": "PostgresMediaJobStore",
        "media_asset_repository": "PostgresMediaAssetRepository",
    }
    mismatches = {
        key: {"expected": value, "actual": health.get(key)}
        for key, value in expected.items()
        if health.get(key) != value
    }
    assert not mismatches, f"Full backends are not active: {mismatches}"
    assert health.get("outbox_publisher"), "Outbox publisher is not active"
    assert health.get("ffmpeg", {}).get("available"), "FFmpeg is unavailable"

    created = request(
        "/api/sessions",
        method="POST",
        body={"canvas": "deployment-smoke", "title": "full-stack-smoke"},
    )
    session_id = created["id"]
    restored = request(f"/api/sessions/{session_id}")
    assert restored["id"] == session_id
    assert restored.get("meta", {}).get("canvas") == "deployment-smoke"

    print("PASS full backend repositories + outbox + FFmpeg + session round-trip")
    print(f"session_id={session_id}")


if __name__ == "__main__":
    main()

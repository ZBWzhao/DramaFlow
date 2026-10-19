#!/usr/bin/env bash
# DramaFlow 上线自检：/api/health 后端实现 + FFmpeg + 会话往返
#
# 用法：
#   bash deploy/smoke.sh
#   bash deploy/smoke.sh http://127.0.0.1:8000
#   BASE_URL=https://dramaflow.example.com bash deploy/smoke.sh
#
# 断言口径与 demo/full_stack_acceptance.py 一致；只读 + 建一个临时会话（随后删除）。
# 全部通过退出 0，任一失败退出 1，缺少 curl/python 退出 2。
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://127.0.0.1:8000}}"
BASE_URL="${BASE_URL%/}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
FAILED=0

pass() { printf 'PASS %s\n' "$*"; }
mark_fail() { printf 'FAIL %s\n' "$*" >&2; FAILED=1; }

command -v curl >/dev/null 2>&1 || { echo "缺少 curl" >&2; exit 2; }
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if command -v python3 >/dev/null 2>&1; then PYTHON=python3
  elif command -v python >/dev/null 2>&1; then PYTHON=python
  else echo "缺少 python3" >&2; exit 2
  fi
fi

# —— 1. /api/health ——
HEALTH_JSON="$TMP_DIR/health.json"
if ! curl -fsS --max-time 20 "$BASE_URL/api/health" -o "$HEALTH_JSON"; then
  echo "FAIL 无法访问 $BASE_URL/api/health" >&2
  echo "  排查：docker compose -p dramaflow --env-file .env.production -f compose.full.yml logs --tail=200 app" >&2
  exit 1
fi

check_health() {
  "$PYTHON" - "$1" <<'PY'
import json
import sys

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

with open(sys.argv[1], encoding="utf-8") as fh:
    health = json.load(fh)

problems = []
for key, want in expected.items():
    got = health.get(key)
    if got != want:
        problems.append(f"{key}: 期望 {want}，实际 {got!r}")

if health.get("ok") is not True:
    problems.append(f"ok 不为 true：{health.get('ok')!r}")
if not health.get("outbox_publisher"):
    problems.append("outbox_publisher 未启用（检查 OUTBOX_PUBLISHER_ENABLED=true）")

ffmpeg = health.get("ffmpeg") or {}
if ffmpeg.get("available") is not True:
    problems.append(f"ffmpeg 不可用：{ffmpeg.get('reason') or '未知原因'}")

if problems:
    print("FAIL /api/health 断言失败：", file=sys.stderr)
    for item in problems:
        print(f"  - {item}", file=sys.stderr)
    sys.exit(1)
PY
}

if check_health "$HEALTH_JSON"; then
  pass "/api/health：Redis/PostgreSQL 仓储 + Outbox + FFmpeg"
else
  mark_fail "/api/health"
  echo "FAIL 自检未通过（健康检查是前置条件，后面的会话往返已跳过）：$BASE_URL" >&2
  exit 1
fi

# —— 2. 创建会话并读回 ——
SESSION_JSON="$TMP_DIR/session.json"
if curl -fsS --max-time 30 -X POST "$BASE_URL/api/sessions" \
     -H 'Content-Type: application/json' \
     -d '{"canvas":"deployment-smoke","title":"deployment-smoke"}' \
     -o "$SESSION_JSON"; then
  SESSION_ID="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("id") or "")' "$SESSION_JSON")"
  if [[ -z "$SESSION_ID" ]]; then
    mark_fail "POST /api/sessions 未返回 id"
  else
    pass "创建会话 ${SESSION_ID}"

    RESTORED_JSON="$TMP_DIR/restored.json"
    check_session() {
      "$PYTHON" - "$1" "$2" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    restored = json.load(fh)

problems = []
if restored.get("id") != sys.argv[2]:
    problems.append(f"id 不一致：期望 {sys.argv[2]}，实际 {restored.get('id')!r}")
meta = restored.get("meta") or {}
if meta.get("canvas") != "deployment-smoke":
    problems.append(f"meta.canvas 不一致：{meta.get('canvas')!r}")

if problems:
    print("FAIL 会话读回不一致：", file=sys.stderr)
    for item in problems:
        print(f"  - {item}", file=sys.stderr)
    sys.exit(1)
PY
    }

    if curl -fsS --max-time 20 "$BASE_URL/api/sessions/$SESSION_ID" -o "$RESTORED_JSON" \
       && check_session "$RESTORED_JSON" "$SESSION_ID"; then
      pass "会话读回一致（PostgreSQL 持久化）"
    else
      mark_fail "会话读回"
    fi
    # 无论读回是否成功都清理测试会话，避免在生产库留下垃圾数据
    curl -fsS --max-time 10 -X POST "$BASE_URL/api/sessions/$SESSION_ID/delete" -o /dev/null || true
  fi
else
  mark_fail "POST /api/sessions"
fi

# —— 3. 结果 ——
if [[ "$FAILED" -ne 0 ]]; then
  echo "FAIL 自检未通过：$BASE_URL" >&2
  exit 1
fi
echo "PASS 上线自检全部通过：$BASE_URL"

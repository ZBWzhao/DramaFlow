#!/usr/bin/env bash
# DramaFlow 恢复：PostgreSQL + workspace / agent 命名卷
#
# 用法：
#   bash deploy/restore.sh --yes --stamp 20261018-030000
#   bash deploy/restore.sh --yes --postgres /var/backups/dramaflow/postgres-....dump \
#                          --workspace /var/backups/dramaflow/workspace-....tar.gz \
#                          --agent /var/backups/dramaflow/agent-....tar.gz
#
# 会覆盖当前数据，必须显式 --yes。建议先停掉正在使用系统的用户。
# 流程：停 app/worker -> DROP 并重建数据库 -> pg_restore -> 清空并解包卷 -> 启动 app/worker
# 注意：不会 down -v；PostgreSQL 的迁移会在 app 启动时自动重跑（幂等）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PROJECT="${DRAMAFLOW_PROJECT:-dramaflow}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/compose.full.yml}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env.production}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dramaflow}"
PG_USER="${POSTGRES_USER:-dramaflow}"
PG_DB="${POSTGRES_DB:-dramaflow}"
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine:3.20}"

ASSUME_YES=0
FLUSH_REDIS=0
STAMP=""
PG_DUMP=""
WS_TAR=""
AGENT_TAR=""

log() { printf '[restore] %s %s\n' "$(date '+%F %T')" "$*"; }
die() { echo "[restore] 错误：$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：
  bash deploy/restore.sh --yes --stamp <时间戳>
  bash deploy/restore.sh --yes --postgres <dump> [--workspace <tar.gz>] [--agent <tar.gz>]

选项：
  --yes              必须显式给出，确认会覆盖当前数据
  --stamp <时间戳>   从备份目录按 postgres-/workspace-/agent-<时间戳> 取文件（如 20261018-030000）
  --postgres <文件>  pg_dump -Fc 产物
  --workspace <文件> workspace 卷 tar.gz
  --agent <文件>     agent 卷 tar.gz
  --dir <目录>       备份目录，默认 /var/backups/dramaflow
  --flush-redis      恢复后清空 Redis（数据库回滚后旧事件流/运行态可能不一致）
  -h, --help         显示本帮助

示例：
  bash deploy/restore.sh --yes --stamp 20261018-030000
  bash deploy/restore.sh --yes --postgres /var/backups/dramaflow/postgres-20261018-030000.dump
EOF
}

need_value() {
  [[ $# -ge 2 && -n "$2" ]] || { echo "[restore] 参数 $1 缺少值" >&2; exit 2; }
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)      ASSUME_YES=1; shift ;;
    --flush-redis) FLUSH_REDIS=1; shift ;;
    --stamp)       need_value "$@"; STAMP="$2"; shift 2 ;;
    --dir)         need_value "$@"; BACKUP_DIR="$2"; shift 2 ;;
    --postgres)    need_value "$@"; PG_DUMP="$2"; shift 2 ;;
    --workspace)   need_value "$@"; WS_TAR="$2"; shift 2 ;;
    --agent)       need_value "$@"; AGENT_TAR="$2"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *)             echo "[restore] 未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$STAMP" ]]; then
  [[ -n "$PG_DUMP" ]]   || PG_DUMP="$BACKUP_DIR/postgres-$STAMP.dump"
  [[ -n "$WS_TAR" ]]    || WS_TAR="$BACKUP_DIR/workspace-$STAMP.tar.gz"
  [[ -n "$AGENT_TAR" ]] || AGENT_TAR="$BACKUP_DIR/agent-$STAMP.tar.gz"
fi

if [[ -z "$PG_DUMP" && -z "$WS_TAR" && -z "$AGENT_TAR" ]]; then
  echo "[restore] 没有指定要恢复的内容" >&2
  usage >&2
  exit 2
fi

# —— 覆盖前必须显式确认 ——
if [[ "$ASSUME_YES" -ne 1 ]]; then
  cat >&2 <<'EOF'
[restore] 已拒绝执行：必须显式加 --yes。
  这会覆盖：
    - PostgreSQL 数据库 dramaflow（先 DROP 再重建）
    - 卷 dramaflow_workspace_data（/app/workspace，含素材）
    - 卷 dramaflow_agent_data（/app/.agent，含会话与生产检查点）
  确认备份文件可用后再执行，例如：bash deploy/restore.sh --yes --stamp 20261018-030000
EOF
  exit 1
fi

command -v docker >/dev/null 2>&1 || die "未找到 docker"
docker compose version >/dev/null 2>&1 || die "需要 docker compose v2"
[[ -f "$COMPOSE_FILE" ]] || die "compose 文件不存在：$COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || die "环境文件不存在：$ENV_FILE"

# 校验输入文件，避免恢复中途才发现缺文件
check_file() {
  [[ -n "$1" ]] || return 0
  [[ -f "$1" ]] || die "备份文件不存在：$1"
  [[ -s "$1" ]] || die "备份文件为空：$1"
}
check_file "$PG_DUMP"
check_file "$WS_TAR"
check_file "$AGENT_TAR"

for vol in "${PROJECT}_workspace_data" "${PROJECT}_agent_data"; do
  docker volume inspect "$vol" >/dev/null 2>&1 || die "卷不存在：$vol（先启动过一次完整服务？）"
done

compose() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

log "即将恢复："
[[ -n "$PG_DUMP" ]]   && log "  PostgreSQL <- $PG_DUMP"   || true
[[ -n "$WS_TAR" ]]    && log "  workspace  <- $WS_TAR"    || true
[[ -n "$AGENT_TAR" ]] && log "  agent      <- $AGENT_TAR" || true
log "  目标项目：$PROJECT（数据库 $PG_DB，用户 $PG_USER）"

# —— 1. 确保基础服务在跑，再停应用进程 ——
log "确保 postgres / redis 运行"
compose up -d postgres redis >/dev/null

for _ in {1..30}; do
  compose exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 && break
  sleep 1
done
compose exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null || die "PostgreSQL 未就绪"

log "停止 app / worker"
compose stop app worker >/dev/null 2>&1 || log "  （没有运行中的 app/worker，继续）"

# —— 2. PostgreSQL：删库重建后整体导入 ——
if [[ -n "$PG_DUMP" ]]; then
  log "删除并重建数据库 $PG_DB"
  compose exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" dropdb -U "$1" --if-exists --force "$2" && createdb -U "$1" -O "$1" "$2"' \
    sh "$PG_USER" "$PG_DB"

  log "pg_restore"
  compose exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U "$1" -d "$2" --no-owner --no-privileges --clean --if-exists --single-transaction' \
    sh "$PG_USER" "$PG_DB" < "$PG_DUMP"
  log "PostgreSQL 恢复完成"
fi

# —— 3. 命名卷：清空后解包（避免旧文件残留）——
restore_volume() {
  local vol="$1" tar_path="$2" dir base
  dir="$(cd "$(dirname "$tar_path")" && pwd)"
  base="$(basename "$tar_path")"
  log "清空并解包 $vol <- $base"
  docker run --rm \
    -v "$vol:/dst" \
    -v "$dir:/backup:ro" \
    "$ALPINE_IMAGE" \
    sh -c 'set -e; find /dst -mindepth 1 -maxdepth 1 -exec rm -rf {} +; tar xzf "/backup/$1" -C /dst' \
    sh "$base"
  log "卷 $vol 恢复完成"
}

if [[ -n "$WS_TAR" ]]; then restore_volume "${PROJECT}_workspace_data" "$WS_TAR"; fi
if [[ -n "$AGENT_TAR" ]]; then restore_volume "${PROJECT}_agent_data" "$AGENT_TAR"; fi

# —— 4. 可选：清 Redis ——
if [[ "$FLUSH_REDIS" -eq 1 ]]; then
  log "清空 Redis（FLUSHALL）"
  compose exec -T redis redis-cli FLUSHALL >/dev/null
fi

# —— 5. 重新启动（不 --build，用现有镜像）——
log "启动 app / worker"
compose up -d app worker >/dev/null

# —— 6. 健康检查提示 ——
if command -v curl >/dev/null 2>&1; then
  sleep 5
  if curl -fsS --max-time 10 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    log "健康检查通过。建议再跑：bash deploy/smoke.sh http://127.0.0.1:8000"
  else
    log "健康检查暂未通过，查看日志：docker compose -p $PROJECT --env-file $ENV_FILE -f $COMPOSE_FILE logs --tail=200 app"
  fi
fi

log "恢复流程结束"

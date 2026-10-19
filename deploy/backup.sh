#!/usr/bin/env bash
# DramaFlow 备份：PostgreSQL（pg_dump -Fc）+ workspace / agent 命名卷（tar.gz）
#
# 用法：
#   bash deploy/backup.sh
#   BACKUP_DIR=/data/backup KEEP_DAYS=30 bash deploy/backup.sh
#
# cron（每天 03:30，日志追加）：
#   30 3 * * * cd /opt/dramaflow && /usr/bin/env bash deploy/backup.sh >> /var/log/dramaflow-backup.log 2>&1
#
# 只读备份：不停服务、不 down、不 down -v。Redis 不备份——
# 事件流可从 PostgreSQL/Outbox 回放，运行态是短期数据，恢复后由应用重建。
# 产物（含数据库）仅 root 可读（umask 077），请自行异地同步。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

umask 077

PROJECT="${DRAMAFLOW_PROJECT:-dramaflow}"          # compose -p，卷名即 ${PROJECT}_workspace_data
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/compose.full.yml}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env.production}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/dramaflow}"
KEEP_DAYS="${KEEP_DAYS:-14}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
PG_USER="${POSTGRES_USER:-dramaflow}"
PG_DB="${POSTGRES_DB:-dramaflow}"
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine:3.20}"
STAMP="$(date +%Y%m%d-%H%M%S)"

log() { printf '[backup] %s %s\n' "$(date '+%F %T')" "$*"; }
die() { echo "[backup] 错误：$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "未找到 docker"
docker compose version >/dev/null 2>&1 || die "需要 docker compose v2（docker compose version）"
[[ -f "$COMPOSE_FILE" ]] || die "compose 文件不存在：$COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || die "环境文件不存在：$ENV_FILE（先按 .env.production.example 准备）"

mkdir -p "$BACKUP_DIR"
[[ -w "$BACKUP_DIR" ]] || die "备份目录不可写：$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"            # docker -v 需要绝对路径

trap 'rc=$?; echo "[backup] 失败：第 $LINENO 行（$BASH_COMMAND），退出码 $rc" >&2; rm -f "$BACKUP_DIR"/*.partial 2>/dev/null || true' ERR

compose() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

compose ps --status running --services 2>/dev/null | grep -qx "$POSTGRES_SERVICE" \
  || die "服务 $POSTGRES_SERVICE 未运行（先 docker compose ... up -d）"

# 卷不存在时 docker run -v 会创建空卷，导致"备份成功但内容为空"，先检查。
for vol in "${PROJECT}_workspace_data" "${PROJECT}_agent_data"; do
  docker volume inspect "$vol" >/dev/null 2>&1 || die "卷不存在：$vol（完整服务至少启动过一次？）"
done

log "开始：project=$PROJECT stamp=$STAMP dir=$BACKUP_DIR keep=${KEEP_DAYS}d"

# —— 1. PostgreSQL 逻辑备份 ——
# 在 postgres 容器内执行，走本地 socket；密码用容器自带的 POSTGRES_PASSWORD。
PG_DUMP="$BACKUP_DIR/postgres-$STAMP.dump"
PG_PARTIAL="$PG_DUMP.partial"
log "pg_dump -Fc ..."
compose exec -T "$POSTGRES_SERVICE" sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$1" -d "$2" -Fc --no-owner' \
  sh "$PG_USER" "$PG_DB" > "$PG_PARTIAL"
[[ -s "$PG_PARTIAL" ]] || die "pg_dump 产物为空"
mv -f "$PG_PARTIAL" "$PG_DUMP"
log "PostgreSQL -> $(basename "$PG_DUMP") ($(du -h "$PG_DUMP" | cut -f1))"

# —— 2. 命名卷备份（一次性 alpine 容器，直接打命名卷，不依赖宿主机路径）——
backup_volume() {
  local vol="$1" name="$2"
  local out="$BACKUP_DIR/$name-$STAMP.tar.gz"
  local partial="$out.partial"
  log "打包卷 $vol -> $(basename "$out")"
  docker run --rm \
    -v "$vol:/src:ro" \
    -v "$BACKUP_DIR:/backup" \
    "$ALPINE_IMAGE" \
    sh -c 'set -e; tar czf "/backup/$1" -C /src .' sh "$(basename "$partial")"
  [[ -s "$partial" ]] || die "卷 $vol 备份为空"
  mv -f "$partial" "$out"
  log "卷 $vol -> $(basename "$out") ($(du -h "$out" | cut -f1))"
}

backup_volume "${PROJECT}_workspace_data" workspace
backup_volume "${PROJECT}_agent_data" agent

# —— 3. 清理过期备份（只删本脚本生成的三种文件）——
deleted="$(find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'postgres-*.dump' -o -name 'workspace-*.tar.gz' -o -name 'agent-*.tar.gz' \) \
  -mtime +"$KEEP_DAYS" -print -delete | wc -l | tr -d ' ')"
log "清理超过 ${KEEP_DAYS} 天的备份：${deleted} 个文件"

log "完成。当前备份目录："
ls -lh "$BACKUP_DIR" | sed 's/^/[backup] /'

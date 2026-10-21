# DramaFlow 上线清单

> ⚠️ 免责声明：当前版本**没有登录鉴权、配额、限流和对象存储**，`/api/agent` 也不需要令牌。
> 只适合作品展示与受控试用。要公开给不受信任的用户，必须先加身份认证、按用户/按 IP 限流，
> 并把素材迁到对象存储；否则任何人拿到域名就能消耗你的模型额度、写入素材卷。

目标形态：单台 Linux VPS（≥2 核 4 GB），Docker + Compose v2，代码放在 `/opt/dramaflow`。
只有 Caddy/Nginx 监听 80/443，应用只监听 `127.0.0.1:8000`。

| 文件 | 用途 |
|---|---|
| `deploy/Caddyfile` | Caddy 反代 + 自动 HTTPS（推荐） |
| `deploy/nginx.conf` | 等价 Nginx 方案（certbot 证书） |
| `deploy/backup.sh` | 备份 PostgreSQL + workspace/agent 卷，默认保留 14 天 |
| `deploy/restore.sh` | 从备份恢复，必须显式 `--yes` |
| `deploy/smoke.sh` | 上线后自检（健康检查 + 会话往返），PASS/FAIL 用退出码表达 |

---

## 1. 域名与 DNS

- A 记录指向 VPS 公网 IP（有 IPv6 再加 AAAA）；等生效：`dig +short dramaflow.example.com`。
- 云安全组/防火墙只放行 **22（限制来源 IP）、80、443**。不要放行 8000、5432、6379。
- 80 端口必须能从公网访问，否则 HTTP-01 签证书会失败。

## 2. 配置与权限

```bash
cd /opt/dramaflow
cp .env.production.example .env.production
vim .env.production           # 至少填 DEEPSEEK_API_KEY、30+ 位随机 POSTGRES_PASSWORD
chmod 600 .env.production
```

- `APP_BIND_ADDRESS=127.0.0.1`、`APP_PORT=8000` 保持默认，只让反代访问。
- 8000 被别的进程占用时可用 `APP_PORT=8010` 覆盖（compose 已支持 `${APP_PORT:-8000}`），
  但 `Caddyfile` / `nginx.conf` 里的上游 `127.0.0.1:8000` 要同步改。
- `POSTGRES_PASSWORD` 一旦初始化过 `postgres_data` 卷就**不能直接改**；改了会认证失败。
  必须改时：先备份，再删除该卷重建（数据会丢）。
- `.env.production` 已在 `.gitignore` 中，禁止提交。

## 3. 反向代理与证书

**Caddy（推荐，自动签发/续期）**

```bash
sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/dramaflow.example.com/你的域名/g' /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

**Nginx + certbot**

```bash
sudo apt install -y nginx certbot
# 首次签证书：standalone 需要 80 端口空闲
sudo systemctl stop nginx
sudo certbot certonly --standalone -d 你的域名 --email you@example.com --agree-tos --non-interactive
sudo cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak
sudo cp deploy/nginx.conf /etc/nginx/nginx.conf
sudo sed -i 's/dramaflow.example.com/你的域名/g' /etc/nginx/nginx.conf
sudo nginx -t && sudo systemctl start nginx
```

续期用钩子短暂停 nginx：`sudo certbot renew --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"`。
（嫌麻烦就用 Caddy。）

## 4. 首次启动

```bash
docker compose -p dramaflow --env-file .env.production -f compose.full.yml up -d --build
docker compose -p dramaflow --env-file .env.production -f compose.full.yml ps
docker compose -p dramaflow --env-file .env.production -f compose.full.yml logs --tail=100 app worker
```

- 启动时自动执行 `migrations/*.sql`，无需手工建表。
- 卷名固定为 `dramaflow_postgres_data`、`dramaflow_redis_data`、
  `dramaflow_workspace_data`、`dramaflow_agent_data`。
- 四个服务都配了 `restart: unless-stopped`；再执行一次
  `sudo systemctl enable docker`，机器重启后容器会自动拉起，不需要另写 systemd 单元。

## 5. 验收

```bash
curl -fsS http://127.0.0.1:8000/api/health | python3 -m json.tool
bash deploy/smoke.sh http://127.0.0.1:8000
python3 demo/full_stack_acceptance.py http://127.0.0.1:8000
curl -fsS https://你的域名/api/health          # 确认反代与证书
curl -N -X POST https://你的域名/api/agent -H 'Content-Type: application/json' \
  -d '{"session_id":"...","message":"你好","canvas":"main"}'   # 事件应逐行出现，不是一次性吐出
```

## 6. 备份与恢复

```bash
sudo mkdir -p /var/backups/dramaflow
sudo bash deploy/backup.sh
ls -lh /var/backups/dramaflow

# 每天 03:30
crontab -e
30 3 * * * cd /opt/dramaflow && /usr/bin/env bash deploy/backup.sh >> /var/log/dramaflow-backup.log 2>&1
```

- 保留份数用 `KEEP_DAYS`（默认 14 天）；备份目录用 `BACKUP_DIR` 覆盖。
- 只删自己生成的 `postgres-*.dump` / `workspace-*.tar.gz` / `agent-*.tar.gz`。
- Redis 不备份：事件可回放，运行态是短期数据。
- **上线后至少做一次恢复演练**（在测试机或维护窗口）：

```bash
bash deploy/restore.sh --yes --stamp 20261018-030000    # 恢复 DB + 两个卷
bash deploy/restore.sh --yes --stamp ... --flush-redis  # 同时清 Redis（回滚后旧事件流可能不一致）
```

恢复前务必确认备份可用；脚本无 `--yes` 会直接拒绝执行。

## 7. 日志与巡检

```bash
C="docker compose -p dramaflow --env-file .env.production -f compose.full.yml"
$C logs -f app worker
$C ps
docker stats --no-stream
df -h /var/lib/docker /var/backups
```

建议再配 `/api/health`、磁盘占用、容器重启次数、Provider 失败率的外部监控。

## 8. 常见故障

| 现象 | 排查 / 处理 |
|---|---|
| `curl /api/health` 失败、app unhealthy | `$C logs --tail=200 app`；迁移失败、`DATABASE_URL` 连不上，或启动时环境变量缺失 |
| `password authentication failed for user "dramaflow"` | `.env.production` 的 `POSTGRES_PASSWORD` 与卷初始化时不一致；改回旧密码，或备份后重建 `postgres_data` 卷 |
| `bind: address already in use` | `ss -ltnp \| grep :8000`；改 `APP_PORT`，并同步 Caddyfile/nginx 里的上游端口与 `docker compose` 端口映射 |
| media Worker 不消费 | `$C logs --tail=200 worker`；确认 `MEDIA_WORKER_EMBEDDED=false`、worker 容器 running、任务已通过人工审批；`/api/health` 里 `media_worker` 为 `null` 是正常的（独立进程不注册到 API） |
| `ffmpeg.available=false` | 用仓库 Dockerfile 构建（镜像内已装 ffmpeg）；自定义镜像别删这层 |
| 前端发送后"卡住不出字" | 反代漏了 `flush_interval -1` / `proxy_buffering off` / `gzip off`，或云 LB 有缓冲 |
| 上传大文件 413 | 调大 `client_max_body_size`（nginx）或 `request_body max_size`（Caddy） |
| 502 Bad Gateway | app 没起或端口不匹配；`$C ps` + `ss -ltnp` |

## 9. 回滚

```bash
# 升级前先备份并给当前镜像打 tag
sudo bash deploy/backup.sh
docker tag dramaflow:latest dramaflow:rollback-$(date +%Y%m%d)
```

- **代码回滚**：`git fetch --all && git checkout <上一个 tag/commit>`，然后
  `$C up -d --build`。
- **镜像回滚**：`.env.production` 改成 `DRAMAFLOW_IMAGE_TAG=rollback-20261018`，再
  `$C up -d`（不要加 `--build`，否则会重新构建）。
- **数据回滚**：迁移只增不删，旧代码可能不认新表/新列；必要时用 `restore.sh` 回到升级前备份。
- 任何情况下都不要 `docker compose down -v`，那会删除四个数据卷。

## 10. 公开前的硬门槛（当前未实现）

- 登录鉴权与用户隔离（现在任何访问者共用同一套会话/画布/素材）。
- 按用户/IP 的限流、并发上限与费用配额（`/api/agent` 会直接产生模型费用）。
- 对象存储（素材现在落在单机卷 `workspace_data`，没有多副本、没有 CDN）。
- 审计日志与异常告警。

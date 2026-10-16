# DramaFlow 完整模式与上线指南

## 1. 两种运行模式

- `python run.py`：零依赖演示模式。会话、画布和生产进度使用本地文件，短期运行状态使用内存。
- `python run_full.py`：完整后端模式。Redis 保存事件流、运行占用、Follow-up 和结构化问答；PostgreSQL 保存会话、画布、工具调用账本、审批、媒体任务、资产元数据和生产检查点。

正式部署使用 `compose.full.yml`，API 和媒体 Worker 分为两个进程。媒体文件和 JSONL WAL 放在持久卷，Redis 与 PostgreSQL 也分别使用持久卷。

## 2. 本机完整模式

### 准备 Redis 和 PostgreSQL

可使用 Docker，也可以在 WSL/本机安装服务。连接地址应分别为：

```text
redis://127.0.0.1:6379/0
postgresql://postgres:postgres@127.0.0.1:5432/dramaflow
```

应用启动时会在 PostgreSQL 中自动执行 `migrations/*.sql`，无需手工逐个建表。

Windows 上不想依赖 Docker Desktop 时，在一个可见的 PowerShell 终端运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup-wsl-full-backend.ps1
```

首次安装会要求输入 Ubuntu 的 sudo 密码。密码只交给 WSL，不要写入项目配置或聊天窗口。

### 启动

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-full-wsl.ps1
```

该脚本会在每次启动时读取 WSL 的当前私有 IP，并临时设置 `DATABASE_URL`，不会把动态地址写死在项目中。
它还会在应用运行期间保持一个轻量 WSL 会话，避免 WSL 空闲回收导致 Redis/PostgreSQL 突然断开；应用退出时保活进程会一并清理。

另开一个终端验收：

```powershell
python demo/full_stack_acceptance.py
```

验收脚本不只检查 HTTP 200，还会确认 Redis/PostgreSQL 的实际实现已经启用、Outbox Publisher 与 FFmpeg 可用，并创建、恢复一次会话。

## 3. Docker Compose 完整部署

复制生产配置：

```bash
cp .env.production.example .env.production
```

至少填写 `DEEPSEEK_API_KEY` 和一个足够长的随机 `POSTGRES_PASSWORD`。然后执行：

```bash
docker compose --env-file .env.production -f compose.full.yml up -d --build
docker compose --env-file .env.production -f compose.full.yml ps
python demo/full_stack_acceptance.py http://127.0.0.1:8000
```

查看日志：

```bash
docker compose --env-file .env.production -f compose.full.yml logs -f app worker
```

停止服务不会删除数据：

```bash
docker compose --env-file .env.production -f compose.full.yml down
```

只有明确需要清空所有数据库、事件与素材时才使用 `down -v`。

## 4. 上线到云服务器

建议先选择一台至少 2 核 4 GB、带持久磁盘的 Linux VPS，以 Docker Compose 部署。将代码拉到服务器后执行上一节命令，再使用 Caddy 或 Nginx 将域名的 HTTPS 请求反向代理到 `127.0.0.1:8000`。

生产环境不要把 PostgreSQL 的 5432 或 Redis 的 6379 暴露到公网。`compose.full.yml` 默认只暴露应用端口；应用端口也默认绑定到 `127.0.0.1`，由反向代理负责公网入口。

上线前还需要完成：

1. 域名解析与 HTTPS。
2. 云防火墙只开放 22、80、443。
3. `.env.production` 权限设为仅部署用户可读，且永不提交 Git。
4. 定时备份 PostgreSQL 与持久素材卷，并做一次恢复演练。
5. 为 `/api/health`、磁盘、容器重启次数和 Provider 失败率配置监控。
6. 如果公开给不受信任的用户使用，必须再增加登录鉴权、配额、限流和对象存储；当前版本更适合作品展示和受控试用。

## 5. 面试演示建议

演示前运行完整验收脚本，并保存输出。现场可以从 `/api/health` 展示各仓储的真实实现，再展示：

1. 一句话创意进入短剧生产团队。
2. 阶段验收与定向返工。
3. Redis Streams 断线补发。
4. PostgreSQL 会话、画布与任务恢复。
5. 人工审批后进入媒体 Worker。
6. FFmpeg 合成并回填最终画布。

真实媒体 Provider 没有配置时使用 `mock`，可以验证审批、幂等、队列、Worker、画布回填和合成，但要明确说明它没有验证真实模型的画面质量。

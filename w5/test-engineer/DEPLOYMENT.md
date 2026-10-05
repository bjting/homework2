# pg-mcp 部署方案（测试岗交付 · ③）

> 目标：一条命令拉起 **PostgreSQL（多库 + 测试数据）+ pg-mcp 服务 + Prometheus 指标** 的完整可验收环境，
> 并给出生产部署的加固清单与回滚预案。

## 1. 环境分层

| 环境 | 形态 | 用途 | 数据 |
|---|---|---|---|
| local | `docker compose up`（本仓库 `docker-compose.yml`） | 开发/测试/演示 | fixtures 三套示例库 |
| staging | 同 compose，`ENVIRONMENT=staging`，接只读账号 | 验收测试 | 脱敏快照 |
| production | 容器化部署 + 托管 PostgreSQL，`ENVIRONMENT=production` | 对外服务 | 最小权限账号 |

## 2. 本地/测试环境：docker compose 一键部署

前提：Docker Desktop；`.env` 由 `.env.example` 复制并填入 `OPENAI_API_KEY`。

```bash
cd w5/pg-mcp
cp .env.example .env            # 填入 OPENAI_API_KEY；可按需追加：
# 多库（本方案新增能力）：
# EXTRA_DATABASES=[{"name":"ecommerce_medium"},{"name":"saas_crm_large"}]
# 安全策略（本方案新增能力）：
# SECURITY_BLOCKED_TABLES=audit_logs
# SECURITY_BLOCKED_COLUMNS=users.password
# SECURITY_ALLOW_EXPLAIN=true
# 限流（本方案新增能力）：
# RESILIENCE_QUERY_RATE_LIMIT=10
# RESILIENCE_LLM_RATE_LIMIT=5

docker compose up -d            # 拉起 postgres + pg-mcp
docker compose logs -f pg-mcp   # 观察 "Server ready to accept requests"
```

启动后可用性验证：

```bash
# 1) 进程与配置
docker compose exec pg-mcp python -c \
  "from pg_mcp.config.settings import Settings; print(list(Settings.model_validate({}).databases))"

# 2) 指标端点（OBSERVABILITY_METRICS_ENABLED=true 时）
curl -s http://localhost:9090/metrics | grep pg_mcp_query_requests

# 3) MCP 协议层冒烟：见 §4 验收清单
```

## 3. 生产部署加固清单

- [ ] **最小权限**：专用只读角色 `CREATE ROLE app_ro NOINHERIT; GRANT SELECT ON ALL TABLES …`，
      配合 `SECURITY_READONLY_ROLE=app_ro` 双保险（应用层只读事务 + 数据库层权限）；
- [ ] **敏感对象**：`SECURITY_BLOCKED_TABLES` / `SECURITY_BLOCKED_COLUMNS` 覆盖凭据、审计类表；
- [ ] **写入关闭**：`SECURITY_ALLOW_WRITE_OPERATIONS=false`（生产开启会在启动日志出现 WARNING 告警）；
- [ ] **限流参数**：按容量压测设定 `RESILIENCE_QUERY_RATE_LIMIT` / `RESILIENCE_LLM_RATE_LIMIT`；
- [ ] **密钥管理**：`OPENAI_API_KEY` 走 secret manager 注入，不落 `.env`；
- [ ] **可观测**：`OBSERVABILITY_METRICS_ENABLED=true`、`OBSERVABILITY_LOG_FORMAT=json`，
      Prometheus 抓取 `:9090/metrics`；关注 `pg_mcp_query_requests_total`、
      `pg_mcp_query_duration_seconds`、`pg_mcp_sql_rejected_total`、`pg_mcp_llm_tokens_used`；
- [ ] **告警建议**：`rate(rate_limit_exceeded)` 持续 > 0（容量不足）、熔断器状态非 CLOSED、
      `sql_rejected` 突增（疑似探测/攻击）。

## 4. 部署验收（冒烟）清单

| # | 检查项 | 通过标准 |
|---|---|---|
| D-01 | 容器健康 | `docker compose ps` 全部 healthy/running |
| D-02 | 多库就绪 | 启动日志列出全部配置库 "Created connection pool …" + "Schema loaded …" |
| D-03 | 只读保障 | 发送"删除所有用户"类问题 → 返回 `security_violation`，数据行数不变 |
| D-04 | 封锁对象 | 问题命中 blocked_tables/columns → 拒绝；其余表正常返回 |
| D-05 | 限流生效 | `RESILIENCE_QUERY_RATE_LIMIT=1` 时并发 2 请求 → 其一 `rate_limit_exceeded` |
| D-06 | 指标输出 | 触发一次成功查询后 `pg_mcp_query_requests_total{status="success"}` 增长 |
| D-07 | 追踪贯穿 | 任一响应的 `request_id` 能在日志中检索到同一 ID 的全链路日志 |
| D-08 | 优雅停机 | `docker compose stop` 3 秒内退出，日志出现 "connection pools closed" |

## 5. 升级与回滚

```bash
# 升级（镜像化部署）
docker compose build pg-mcp && docker compose up -d --no-deps pg-mcp

# 回滚：保留上一版镜像 tag，指向旧 tag 重建容器
docker compose up -d --no-deps pg-mcp
```

- 数据库 schema 由 fixtures 管理，服务本身无状态（除进程内 schema 缓存），
  回滚无需数据迁移；
- 配置变更（新增 blocked_tables 等）仅需改 `.env` 后 `docker compose up -d --force-recreate pg-mcp`。

## 6. 已知限制

- 本仓库 `docker-compose.yml` 面向本地/测试环境；生产需替换为带资源限额、
  健康检查、日志收集的编排（k8s/云容器服务）；
- `fixtures/*.sql` 为演示数据，生产接入需替换为脱敏快照或只读副本。

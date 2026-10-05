# Codex Review 缺失功能实现说明（研发岗交付）

> 针对《第 5 章第 4 节 codex review》指出的三类问题，逐项落地。
> 验证方式：`uv sync --extra dev && uv run pytest tests/unit -q` —— **297 个单元测试全部通过**，
> `ruff check src tests` 全绿，单元测试覆盖率 **66% → 76%**（剩余缺口为需真实
> PostgreSQL/LLM 的集成模块，属存量状态，见 §4）。

---

## Review 问题 1：多数据库与安全控制未启用

**Review 原文要点**：服务器始终使用单一执行器，无法强制实施表/列访问限制或 EXPLAIN
策略，可能导致请求访问错误数据库，且敏感对象无法得到保护。

### 修复前的问题定位

| 位置 | 问题 |
|---|---|
| `server.py` lifespan | 仅为 `settings.database`（主库）创建连接池和执行器 |
| `server.py` | `SQLValidator(blocked_tables=None, blocked_columns=None, allow_explain=False)` 硬编码，安全策略形同虚设 |
| `orchestrator.py` | `execute_query()` 虽然解析了目标数据库名，却始终调用 `self.sql_executor.execute()`（主库执行器）→ **请求访问错误数据库** |
| `settings.py` | 无多数据库配置、无 blocked_tables/blocked_columns/allow_explain 配置字段 |

### 修复内容

1. **多数据库配置**（`config/settings.py`）
   - 新增 `Settings.extra_databases`：通过 `EXTRA_DATABASES` 环境变量传 JSON 数组声明额外数据库，
     缺省字段继承 `DATABASE_*` 环境变量/默认值；
   - 新增 `Settings.databases` 属性：返回 `{名称: DatabaseConfig}` 全集（主库在前）；
   - 新增重复数据库名校验（`model_validator`）。

2. **每库独立执行器 + 按请求路由**（`server.py`、`services/orchestrator.py`）
   - lifespan 用 `create_pools(list(settings.databases.values()))` 为**每个配置的数据库**建池，
     并各建一个 `SQLExecutor`；
   - `QueryOrchestrator.__init__` 签名由 `sql_executor: SQLExecutor` 改为
     `sql_executors: dict[str, SQLExecutor]`；
   - `execute_query()` 解析出目标库后**必须**取到同名执行器，取不到则返回
     `database_error`（"No SQL executor configured for database …"）。
     查询永远落在所请求的数据库上，不可能再打到主库。

3. **表/列访问限制与 EXPLAIN 策略真正生效**（`config/settings.py`、`server.py`）
   - `SecurityConfig` 新增 `blocked_tables`、`blocked_columns`、`allow_explain` 三个配置字段
     （支持逗号分隔字符串，兼容环境变量）；
   - lifespan 构造 `SQLValidator` 时从安全配置注入这三个值，
     敏感表/列（如 `audit_logs`、`users.password`）从此无法被查询触达；
   - `.env.example` 补充 `SECURITY_BLOCKED_TABLES / SECURITY_BLOCKED_COLUMNS / SECURITY_ALLOW_EXPLAIN` 文档。

4. **附带激活 `allow_write_operations`**（此前同样是"有配置、无实现"）
   - `sql_validator.py`：`allow_write_operations=True` 时放行 DML（INSERT/UPDATE/DELETE/MERGE），
     DDL/管理语句（DROP/CREATE/ALTER/GRANT…）与危险函数在任何配置下始终禁止；
   - `sql_executor.py`：事务 `readonly` 标志跟随该配置，校验层与执行层策略一致；
   - `server.py`：生产环境开启写操作时输出告警日志（同时激活了 `is_production` 字段）。

### 关键测试（`tests/unit/test_orchestrator.py`、`test_sql_validator.py`、`test_config.py`）

- `TestMultiDatabaseRouting::test_request_routes_to_named_database_executor`
  —— 请求 db2 时只有 db2 的执行器被执行（**错误数据库回归测试**）；
- `TestMultiDatabaseRouting::test_missing_executor_returns_database_error`；
- `TestSecurityPolicyWiring`（12 条）—— 封锁表在子查询/CTE 中同样被拦截、
  `table.column` 精确封锁、大小写不敏感、EXPLAIN 策略、写入开关与 DDL 恒禁用；
- `TestMultiDatabaseSettings`（8 条）—— JSON 解析、字段继承、重名拒绝。

---

## Review 问题 2：弹性与可观测性未接入请求流程

**Review 原文要点**：速率限制、重试/退避机制、指标/追踪系统仅停留在设计层面，
尚未整合到实际请求处理流程。

### 修复前的问题定位

- `server.py` 创建了全局 `_circuit_breaker`/`_rate_limiter` 后**从未使用**（死代码）；
- 限流参数硬编码 `query_limit=10, llm_limit=5  # Can be made configurable`；
- `MetricsCollector` 已实例化但没有任何埋点调用；
- `ResilienceConfig.retry_delay/backoff_factor` 从未被读取（重试无退避）；
- `tracing.py` 的 `request_context` 从未被请求流程使用。

### 修复内容（全部收敛在 `services/orchestrator.py` 的请求主链路）

每个 `execute_query()` 请求现在依次经过：

```
request_context(request_id)            ← 追踪上下文（contextvar 贯穿全链路，响应带 request_id）
  → query_limiter.acquire(timeout)     ← 请求级并发限流（超时返回 rate_limit_exceeded）
    → 解析目标库 → 路由到该库执行器
    → _generate_sql_with_retry()
        ├ llm_limiter.for_llm(timeout) ← LLM 并发限流（超时返回 rate_limit_exceeded）
        ├ metrics: llm_calls / llm_latency（每次尝试）
        ├ 校验失败 → 带错误反馈立即重试
        └ LLM 超时（瞬态故障）→ retry_delay × backoff_factor^attempt 指数退避重试
    → executor.execute()  → metrics: db_query_duration
    → 结果校验           → metrics: validate_result 调用计数/延迟
  → metrics: query_duration（整体延迟直方图）
  → metrics: query_requests{status, database}（成功/每类错误都会计数，finally 保证）
  → metrics: sql_rejected{reason}（安全拦截/解析失败）
```

- **限流可配置**：`ResilienceConfig` 新增 `query_rate_limit`、`llm_rate_limit`、
  `rate_limit_timeout`，orchestrator 缺省据此自建 `MultiRateLimiter`（也可注入共享实例）；
- **重试语义**：校验失败 = 立即重试 + 错误反馈（供 LLM 修正）；LLM 超时 = 瞬态故障，
  指数退避后重试；认证类 LLM 错误 = 快速失败不重试；熔断器在最终失败时计数；
- **server.py 清理**：删除死变量 `_circuit_breaker`/`_rate_limiter`，熔断器/限流器
  归 orchestrator 所有并从配置构建；`_metrics` 注入 orchestrator；
- **`.env.example`** 补充三个限流参数文档。

### 关键测试

- `TestRateLimiting` —— 限流打满返回 `rate_limit_exceeded`、成功后槽位正确归还（无泄漏）、
  缺省限流器按配置创建；
- `TestBackoffRetry` —— LLM 超时按退避重试至成功 / 重试耗尽记熔断 / 非瞬态错误快速失败；
- `TestObservabilityIntegration` —— 成功路径与安全拦截路径的指标调用逐项断言；
  `request_id` 经 `request_context` 传播且与响应一致、请求结束后上下文清理；
- `tests/unit/test_server_tool.py::TestQueryTool` —— server 工具层短路路径
  （未初始化 / 非法 return_type / 空问题 / 异常转 INTERNAL_ERROR）。

---

## Review 问题 3：响应/模型缺陷与测试覆盖不足

### 修复前的问题定位

- `models/query.py` 的 `QueryResponse` 定义了**两个 `to_dict`**，后者静默覆盖前者
  （前者保证 `tokens_used` 恒存在，后者 `exclude_none=True` 会丢弃它），
  迫使 `server.py` 在调用处再打一次 `tokens_used` 补丁 —— 序列化行为取决于定义顺序，属于典型缺陷；
- `ValidationConfig.min_confidence_score` 与 `confidence_threshold` 语义重复且前者**零引用**；
- `SecurityConfig.allow_write_operations`、`ResilienceConfig.retry_delay/backoff_factor`、
  `Settings.is_production/is_development` 均未使用；
- 单元测试未覆盖上述任何安全/弹性路径。

### 修复内容

1. **唯一 `to_dict`**（`models/query.py`）：删除重复定义，保留唯一规范化实现
   （`exclude_none=True` + `tokens_used` 缺省补 0）；`server.py` 移除手工补丁；
   新增回归测试 `test_to_dict_is_the_only_serialization`（反射断言只有一个实现），
   以及 tokens_used 恒存在 / None 字段省略 / request_id 序列化等 5 条测试；
2. **删除 `min_confidence_score`**（重复字段），`.env.example` 同步改为
   `VALIDATION_CONFIDENCE_THRESHOLD`；
3. **激活其余闲置字段**：`retry_delay/backoff_factor` → 退避重试（§2）；
   `allow_write_operations` → 校验+执行层（§1）；`is_production` → 生产环境写操作告警（§1）；
4. **`QueryResponse` 新增 `request_id` 字段**，响应自带追踪 ID；
5. **测试覆盖**：单元测试 224 → **297**（+73），新增文件 `test_server_tool.py`，
   新增类 `TestMultiDatabaseRouting` / `TestBackoffRetry` / `TestRateLimiting` /
   `TestObservabilityIntegration` / `TestSecurityPolicyWiring` / `TestMultiDatabaseSettings` /
   `TestQueryResponseToDict` / `TestTracingContext` / `TestQueryTool`；
   修复存量错误断言 `TestObservabilityConfig::test_default_values`（默认 log_format 断言为 json，
   实际为 text）。

---

## 4. 已知边界（如实说明）

- 覆盖率门槛（`fail_under=80`）在"仅单元测试"运行下为 **76%**：缺口集中在
  `server.py` lifespan（需完整 MCP/DB 环境）、`db/introspection.py`、`observability/logging.py`、
  `services/result_validator.py` —— 这些由 `tests/integration`、`tests/e2e` 覆盖，
  需要 PostgreSQL 实例与 OPENAI_API_KEY（`docker-compose up -d` 后运行）。
  基线对比：改造前单元测试覆盖率 66%，且同样的模块为零覆盖。
- `tokens_used` 仍恒为 0：`SQLGenerator.generate()` 只返回 SQL 字符串，
  OpenAI usage 元数据需改接口透传，属后续增强（与 review 所述行为一致）。
- 集成/E2E 测试需要真实服务，本次验证范围为全部单元测试（297 通过）+ ruff 全绿。

## 验证命令

```bash
cd w5/pg-mcp
uv sync --extra dev
uv run pytest tests/unit -q          # 297 passed
uv run pytest tests/unit --cov=src   # 总覆盖率 76%（基线 66%）
uv run ruff check src tests          # All checks passed
```

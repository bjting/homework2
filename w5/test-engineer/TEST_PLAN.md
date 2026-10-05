# pg-mcp 测试用例设计与自动化测试方案（测试岗交付）

> 案例选择：以 `w5/pg-mcp`（自然语言查库 MCP 服务）为被测系统，用 AI 完成三件事：
> **① 测试用例设计**（本文档 §2-§4 用例矩阵）、**② 自动化脚本**（§5，已落地为 pytest 套件 + GitHub Actions CI）、
> **③ 部署方案**（`DEPLOYMENT.md`，docker-compose 分层部署 + 冒烟验收）。
>
> 当前自动化状态：**297 个单元测试全部通过，`ruff check` 全绿，覆盖率 76%（基线 66%）**。

---

## 1. 被测系统与测试策略

pg-mcp 的请求主链路：`自然语言问题 → LLM 生成 SQL → 安全校验 → 只读执行 → 结果校验 → 响应`。
风险排序决定了测试投入分配：

| 风险 | 后果 | 策略 |
|---|---|---|
| 安全校验被绕过（写操作/危险函数/敏感表） | 数据泄露或损坏 | **最高**：穷举式单元测试 + CI 独立安全 job |
| 请求路由到错误数据库 | 数据口径错误 | 回归测试锁定"请求库=执行库" |
| LLM 不可用拖垮服务 | 服务雪崩 | 退避/熔断/限流行为测试 |
| 响应序列化不稳定 | 客户端解析失败 | to_dict 契约测试 |

分层：**单元**（mock 全部外部依赖，秒级，每次提交跑）→ **集成**（真实 PostgreSQL，跑数据流）→
**E2E**（MCP 协议级，验收）。本方案新增/修订的全部用例落在单元层（可离线重复执行），
集成层沿用仓库既有 `tests/integration`、`tests/e2e`。

---

## 2. 安全测试用例（核心，对应 SQLValidator / SQLExecutor / 多库路由）

| # | 用例 | 输入 | 期望 |
|---|---|---|---|
| S-01 | 基本 SELECT 放行 | `SELECT * FROM users` | 通过 |
| S-02 | INSERT/UPDATE/DELETE 拒绝 | `DELETE FROM users WHERE id=1` | `security_violation` |
| S-03 | DDL 恒拒绝（即使允许写入） | `DROP TABLE users` / `GRANT ALL …` | 拒绝 |
| S-04 | 危险函数拒绝 | `SELECT pg_sleep(100)` | 拒绝并报函数名 |
| S-05 | 多语句拒绝 | `SELECT 1; DROP TABLE users;--` | 拒绝 |
| S-06 | 子查询/CTE 夹带写操作 | `WITH x AS (DELETE …) SELECT …` | 拒绝 |
| S-07 | **封锁表**直查 | `SELECT * FROM audit_logs`（blocked_tables 含该表） | 拒绝 |
| S-08 | **封锁表**经子查询/CTE 触达 | `…WHERE id IN (SELECT user_id FROM secrets)` | 拒绝 |
| S-09 | **封锁列**（裸列名） | `SELECT password FROM users` | 拒绝 |
| S-10 | **封锁列**（table.column 精确） | `SELECT users.email`；`SELECT contacts.email`（未封锁） | 前者拒绝、后者放行 |
| S-11 | 大小写/引号变体 | `SELECT * FROM "AUDIT_LOGS"` | 拒绝 |
| S-12 | EXPLAIN 策略 | 默认 `EXPLAIN SELECT`；`allow_explain=true` 时 | 前拒后放 |
| S-13 | 写入开关一致性 | `allow_write_operations=true` 时 DML 校验放行 | 执行层事务同步为可写 |
| S-14 | 空SQL/仅注释 | `""`、`-- comment` | `sql_parse_error` |
| S-15 | **多库路由正确性** | 请求 `database=db2` | 仅 db2 执行器被调用 |
| S-16 | 未知数据库 | `database=ghost` | `database_error`，响应含可用库列表 |
| S-17 | search_path 注入防护 | 配置含非法字符的 search_path | 拒绝执行 |

> 状态：S-01~S-16 已自动化（`tests/unit/test_sql_validator.py`、`test_sql_executor.py`、
> `test_orchestrator.py::TestMultiDatabaseRouting`、`::TestSecurityPolicyWiring`）。

## 3. 弹性测试用例（对应 RateLimiter / CircuitBreaker / 退避重试）

| # | 用例 | 场景 | 期望 |
|---|---|---|---|
| R-01 | 请求级限流打满 | 并发 > `query_rate_limit`，等待超过 `rate_limit_timeout` | `rate_limit_exceeded`，不进入流水线 |
| R-02 | LLM 限流打满 | 并发生成 > `llm_rate_limit` | `rate_limit_exceeded` |
| R-03 | 槽位归还 | 请求成功/失败后 | `active_count` 归零（无泄漏） |
| R-04 | 瞬态故障退避重试 | 前 N 次 `LLMTimeoutError`，第 N+1 次成功 | 共调用 N+1 次，间隔按 `retry_delay×factor^k` |
| R-05 | 重试耗尽记熔断 | 持续超时 | 抛 `LLMTimeoutError`，熔断器失败数 +1 |
| R-06 | 非瞬态错误快速失败 | 认证失败类 `LLMError` | 只调用 1 次，不重试 |
| R-07 | 校验失败反馈重试 | 第 1 次生成危险 SQL，第 2 次合法 | 第 2 次携带 `previous_attempt`+`error_feedback` |
| R-08 | 熔断开启 | 失败数 ≥ threshold | 直接拒绝，不再调用 LLM |
| R-09 | 熔断恢复 | 超时后半开→成功 | 回到 CLOSED |
| R-10 | 限流器可配置 | 不同 `ResilienceConfig` | limiter 参数与配置一致 |

> 状态：R-01~R-10 已自动化（`tests/unit/test_orchestrator.py::TestBackoffRetry/::TestRateLimiting`、
> `test_resilience.py`）。

## 4. 契约与可观测性测试用例

| # | 用例 | 期望 |
|---|---|---|
| C-01 | `QueryResponse.to_dict` 唯一性 | 反射检查仅 1 个 `to_dict` 定义（防"后定义覆盖前定义"回归） |
| C-02 | tokens_used 恒存在 | 任何响应序列化后必有 `tokens_used`（无 token 时为 0） |
| C-03 | None 字段省略 | 失败响应不输出 `data:null` 等噪音字段 |
| C-04 | request_id 追踪 | 响应中的 `request_id` == 生成/执行全程 contextvar 中的值，请求后清理 |
| C-05 | 指标：成功路径 | `query_requests{status=success,database}`、`query_duration`、`llm_calls`、`db_query_duration` 全部被记录 |
| C-06 | 指标：安全拦截 | `sql_rejected{reason=security_violation}` + `query_requests{status=security_violation}` |
| C-07 | 工具层短路 | 未初始化→`SERVER_NOT_INITIALIZED`；非法 `return_type`→`INVALID_PARAMETER`；空问题→`INVALID_REQUEST`；编排器崩溃→`INTERNAL_ERROR` |
| C-08 | 配置校验 | `EXTRA_DATABASES` 非法 JSON/非数组/重名均拒绝；封锁表/列支持 CSV 格式 |
| C-09 | 响应执行时间 | `data.execution_time_ms ≥ 0` 且为 float |

> 状态：C-01~C-09 已自动化（`test_models.py::TestQueryResponseToDict`、
> `test_orchestrator.py::TestObservabilityIntegration`、`test_server_tool.py`、`test_config.py`）。

---

## 5. 自动化脚本（已落地）

### 5.1 测试代码结构

```
tests/
├── unit/                        # ← 本方案新增/扩充的主力（297 条，离线可跑）
│   ├── test_sql_validator.py    #   S-01~S-14（含新增 TestSecurityPolicyWiring）
│   ├── test_orchestrator.py     #   S-15/16、R-01~R-10、C-04~C-06
│   ├── test_server_tool.py      #   C-04、C-07（新增文件）
│   ├── test_models.py           #   C-01~C-03（新增 TestQueryResponseToDict）
│   ├── test_config.py           #   C-08（新增 TestMultiDatabaseSettings 等）
│   ├── test_resilience.py       #   R-09（熔断状态机）
│   └── test_sql_executor.py     #   S-17、序列化、超时
├── integration/                 # 真实 PostgreSQL 数据流（需 docker 服务）
└── e2e/                         # MCP 协议级验收（需 DB + LLM key）
```

### 5.2 本地一键执行

```bash
cd w5/pg-mcp
uv sync --extra dev
uv run pytest tests/unit -q --cov=src          # 单元 + 覆盖率
uv run pytest tests/unit/test_sql_validator.py # 只跑安全组
uv run ruff check src tests                    # 静态检查
```

### 5.3 CI 流水线（新增 `.github/workflows/ci.yml`）

四个并行 job，按风险分级把关：

| Job | 内容 | 把关点 |
|---|---|---|
| `lint` | ruff 全仓检查 | 风格/明显缺陷 |
| `unit-tests` | pytest + coverage，**Python 3.12/3.13/3.14 三版本矩阵** | 全量单元用例 |
| `security-tests` | 独立跑 SQL 校验器 + 执行器 + 安全策略用例 | 安全用例单独红灯即拦截合并 |
| `integration-tests` | GitHub Actions **service 容器起 PostgreSQL 16**，加载 `fixtures/01_small_db.sql` 后跑集成测试 | 真实数据流；无 `OPENAI_API_KEY` 的 fork PR 自动跳过 LLM 用例 |

---

## 6. 缺口与后续计划（如实说明）

- 集成/E2E 用例依赖真实服务，本地未执行（CI 的 `integration-tests` job 可覆盖）；
- 覆盖率缺口集中在 `server.py` lifespan、`db/introspection.py`、`observability/logging.py`、
  `services/result_validator.py`，需要 PostgreSQL fixture + mock LLM 的集成层补充；
- 建议后续引入：mutation testing（mutmut）验证安全用例的"杀死突变体"能力、
  以 `hypothesis` 对 SQL 校验器做 property-based 模糊测试。

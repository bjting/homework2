# W5 作业交付总览

> 交付日期：2026-10-05。三个岗位围绕同一产品载体 `w5/pg-mcp`（自然语言查库 MCP 服务）展开。

## 交付清单

| 岗位 | 案例选择 | 交付物 |
|---|---|---|
| **研发岗** | 实现第 5 章第 4 节 codex review 指出的全部缺失功能 | `w5/pg-mcp/` 代码改造 + `CODEX_REVIEW_FIXES.md` 逐项说明 |
| **测试岗** | 测试用例设计 + 自动化脚本 + 部署方案 | `w5/test-engineer/TEST_PLAN.md`（36 条用例矩阵）、`DEPLOYMENT.md`（部署+验收）、`w5/pg-mcp/.github/workflows/ci.yml`（4-job CI） |
| **产品岗** | 产品文档 + 交互原型 + 介绍 PPT | `w5/product-manager/PRD.md`、`prototype.html`（浏览器直接打开）、`产品介绍PPT.html`（方向键翻页） |

## 研发岗：三个 review 问题的落地结果

1. **多数据库与安全控制** —— `EXTRA_DATABASES` 多库配置；每库独立连接池+执行器，请求按库名精确路由（修复"请求访问错误数据库"）；`SECURITY_BLOCKED_TABLES/_COLUMNS/ALLOW_EXPLAIN` 三个安全策略真正接入校验器，敏感对象不可触达；附带激活 `allow_write_operations`（校验层+执行层联动）。
2. **弹性与可观测性接入请求流程** —— 请求级/LLM 级双层限流（超时返回 `rate_limit_exceeded`）、LLM 瞬态故障指数退避重试、熔断计数；Prometheus 指标全链路埋点（请求计数/延迟/拦截/Token）；`request_context` 追踪贯穿，响应携带 `request_id`。删除了 server.py 中从未使用的死代码限流/熔断器。
3. **响应/模型缺陷与测试** —— 删除 `QueryResponse` 重复 `to_dict`（含反射回归测试）；删除零引用的 `min_confidence_score`；其余闲置配置字段全部激活；单元测试 **224 → 297**（+73），覆盖率 **66% → 76%**。

## 验证方式（已全部执行通过）

```bash
cd w5/pg-mcp
uv sync --extra dev
uv run pytest tests/unit -q          # 297 passed
uv run ruff check src tests          # All checks passed
```

- 原型/PPT：直接双击 `w5/product-manager/prototype.html` 与 `产品介绍PPT.html`；
- 部署验收：见 `w5/test-engineer/DEPLOYMENT.md` §4（8 条冒烟清单）。

## 如实说明（未覆盖部分）

- 集成/E2E 测试需真实 PostgreSQL 与 `OPENAI_API_KEY`，本次未在本地执行（CI 的 integration job 可覆盖）；覆盖率 76% 未达 80% 门槛的缺口即在此类模块，属存量状态（基线同样不达标，且为 66%）。
- `tokens_used` 恒为 0：需 `SQLGenerator` 接口透传 OpenAI usage 元数据，属后续增强。
- 原型中查询结果为前端模拟数据（原型演示用途）；后端能力（多库路由/安全策略/限流熔断/追踪）均为 `w5/pg-mcp` 中真实实现并有测试覆盖。

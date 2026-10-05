"""End-to-end verification script for the w5 research deliverables.

Runs the real server pipeline (lifespan -> orchestrator -> executors ->
PostgreSQL) against the dockerized test databases, and demonstrates:

    [1] Normal NL query            -> success + request_id + confidence
    [2] SQL-only mode              -> generated SQL without execution
    [3] Multi-database routing     -> query ecommerce_medium via EXTRA_DATABASES
    [4] Unknown database rejection -> database_error (no wrong-DB execution)
    [5] Blocked-table security     -> security_violation (SECURITY_BLOCKED_TABLES)
    [6] Concurrent rate limiting   -> set RESILIENCE_QUERY_RATE_LIMIT=1 in .env
                                      to see one request get rate_limit_exceeded

Usage:
    uv run python verify_e2e.py

Prerequisites:
    - docker compose up -d postgres   (fixtures loaded)
    - .env with OPENAI_API_KEY etc.   (see .env.example)
"""

import asyncio

from dotenv import load_dotenv

from pg_mcp.server import lifespan, mcp, query


async def show(label: str, r: dict) -> None:
    """Print a compact summary of a query-tool response."""
    err = r.get("error") or {}
    print(f"    {label}: success={r.get('success')} code={err.get('code', '-')} "
          f"confidence={r.get('confidence', '-')}")
    if r.get("request_id"):
        print(f"    request_id: {r['request_id']}")
    if r.get("generated_sql"):
        print(f"    SQL: {r['generated_sql']}")
    if r.get("data") is not None:
        d = r["data"]
        print(f"    rows({d.get('row_count')}): {str(d.get('rows')[:2])[:120]}")


async def main() -> None:
    print("=" * 72)
    print("pg-mcp 研发效果端到端验证")
    print("=" * 72)
    load_dotenv()  # .env -> os.environ (nested configs read os.environ only)

    async with lifespan(mcp):
        # [1] 正常查询（走 LLM -> 安全校验 -> 只读执行 -> 结果校验）
        print("\n[1] 正常查询 blog_small ...")
        await show("result", await query(
            question="数据库里一共有多少个用户？",
            database="blog_small",
            return_type="result",
        ))

        # [2] 仅生成 SQL 模式
        print("\n[2] return_type=sql（只生成不执行）...")
        await show("sql-only", await query(
            question="最新的 5 篇已发布文章的标题",
            database="blog_small",
            return_type="sql",
        ))

        # [3] 多库路由：切到 EXTRA_DATABASES 里声明的电商库
        print("\n[3] 多库路由 -> ecommerce_medium ...")
        await show("route", await query(
            question="一共有多少个用户？",
            database="ecommerce_medium",
            return_type="result",
        ))

        # [4] 请求不存在的库 -> 应得到 database_error，而不是跑去主库执行
        print("\n[4] 请求不存在的数据库 ...")
        await show("wrong-db", await query(
            question="有多少个用户？",
            database="no_such_db",
            return_type="result",
        ))

        # [5] 封锁表：SECURITY_BLOCKED_TABLES 命中 -> security_violation
        print("\n[5] 试探封锁表 audit_logs（需在 .env 配置 SECURITY_BLOCKED_TABLES）...")
        await show("security", await query(
            question="查看 audit_logs 表的全部记录",
            database="blog_small",
            return_type="result",
        ))

        # [6] 并发限流：把 .env 里 RESILIENCE_QUERY_RATE_LIMIT 设为 1 后重跑本脚本，
        #     两个并发请求之一应得到 rate_limit_exceeded
        print("\n[6] 并发 2 个请求（默认限流=10，两个都成功；设为 1 可观察限流）...")
        results = await asyncio.gather(
            query(question="有多少篇文章？", database="blog_small", return_type="sql"),
            query(question="有多少个标签？", database="blog_small", return_type="sql"),
        )
        for i, r in enumerate(results):
            code = (r.get("error") or {}).get("code", "success")
            print(f"    req{i}: {code}")

        # 保持进程存活，便于另开终端查看 Prometheus 指标
        print("\n按回车键退出（期间可另开终端执行: "
              "curl -s http://localhost:9090/metrics | findstr pg_mcp ）")
        await asyncio.get_event_loop().run_in_executor(None, input)


if __name__ == "__main__":
    asyncio.run(main())

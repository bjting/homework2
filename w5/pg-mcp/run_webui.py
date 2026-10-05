"""Launch pg-mcp with a browser-friendly HTTP transport.

Starts the MCP server with the streamable-HTTP transport on
http://127.0.0.1:8000/mcp, with CORS enabled so the bundled webui.html
(opened directly from disk) can talk to it. The orchestrator lifespan is
chained in front of the SDK's session-manager lifespan, so pools and the
orchestrator are initialized exactly as in stdio mode.

Usage:
    uv run python run_webui.py
Then open webui.html in a browser.
"""

import contextlib

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()  # .env -> os.environ (nested configs read os.environ only)

    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    from pg_mcp.server import lifespan, mcp

    app = mcp.streamable_http_app()

    # The SDK wires only session_manager.run() as the HTTP app lifespan
    # (server.py:1038); chain our orchestrator lifespan in front of it.
    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def combined_lifespan(a):
        async with lifespan(mcp), original_lifespan(a):
            yield

    app.router.lifespan_context = combined_lifespan

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # webui.html is opened from file://
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )

    print("MCP server: http://127.0.0.1:8000/mcp  (open webui.html in browser)")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()

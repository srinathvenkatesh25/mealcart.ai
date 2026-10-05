import logging
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.api import runs, ws
from app.config import get_settings
from app.db import init_db, recover_runs_after_restart
from app.graph.graph import build_graph, checkpoint_serde
from app.graph.registry import RunRegistry
from app.graph.runner import Deps, Runner, default_deps

log = logging.getLogger(__name__)


def create_app(deps: Deps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db()
        resumable = await recover_runs_after_restart()
        if resumable:
            log.info("runs waiting for plan approval after restart: %s", resumable)
        conn = await aiosqlite.connect(get_settings().checkpoint_path)
        checkpointer = AsyncSqliteSaver(conn, serde=checkpoint_serde())
        app.state.deps = deps or default_deps()
        app.state.registry = RunRegistry()
        app.state.runner = Runner(build_graph(checkpointer), app.state.registry, app.state.deps)
        try:
            yield
        finally:
            for task in app.state.runner.tasks.values():
                task.cancel()
            await conn.close()

    app = FastAPI(title="MealCart Agent", lifespan=lifespan)
    app.include_router(runs.router)
    app.include_router(ws.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()

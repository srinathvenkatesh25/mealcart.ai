"""Runs the graph in the background, one asyncio task per run.

The graph pauses in two ways:
- plan_approval: a LangGraph interrupt. The runner turns it into a question
  on the registry and resumes the graph with the answer. Because the state is
  checkpointed, an approval also resumes a run after a server restart.
- inside `shop`: the shopper calls registry.ask directly, so the browser
  stays open while you answer.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

from app import db
from app.graph.registry import RunRegistry
from app.hitl import events
from app.models import MealSpec
from app.nutrition.usda import MacroLookup
from app.planner.planner import StructuredLLM
from app.shopping.session import Ask
from app.shopping.shopper import Site

log = logging.getLogger(__name__)

OpenSite = Callable[[str, Ask], AbstractAsyncContextManager[Site]]


@dataclass
class Deps:
    make_llm: Callable[[], StructuredLLM]  # one per run: it remembers which fallback model works
    lookup: MacroLookup
    open_site: OpenSite


def default_deps() -> Deps:
    from app.nutrition.usda import USDAClient
    from app.planner.llm import LLM
    from app.shopping.instacart import open_instacart
    return Deps(make_llm=LLM, lookup=USDAClient().get_macros_per_100g, open_site=open_instacart)


class Runner:
    def __init__(self, graph, registry: RunRegistry, deps: Deps):
        self.graph = graph
        self.registry = registry
        self.deps = deps
        self.tasks: dict[str, asyncio.Task] = {}

    def _config(self, run_id: str) -> dict:
        return {
            "configurable": {
                "thread_id": run_id,
                "llm": self.deps.make_llm(),
                "lookup": self.deps.lookup,
                "emit": self.registry.get(run_id).bus.publish,
                "ask": self.registry.asker(run_id),
                "open_site": self.deps.open_site,
            },
            "recursion_limit": 50,
        }

    def start(self, run_id: str, spec: MealSpec) -> asyncio.Task:
        return self._launch(run_id, {"run_id": run_id, "spec": spec})

    def resume(self, run_id: str, answer: Any) -> asyncio.Task:
        """Continue a run paused at plan_approval whose task no longer exists (server restarted)."""
        return self._launch(run_id, Command(resume=answer))

    def is_active(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        return task is not None and not task.done()

    def _launch(self, run_id: str, graph_input: Any) -> asyncio.Task:
        task = asyncio.create_task(self._drive(run_id, graph_input))
        self.tasks[run_id] = task
        return task

    async def _drive(self, run_id: str, graph_input: Any) -> None:
        emit = self.registry.get(run_id).bus.publish
        config = self._config(run_id)
        try:
            await db.update_run(run_id, status="running")
            result = await self.graph.ainvoke(graph_input, config)
            while result.get("__interrupt__"):
                question = result["__interrupt__"][0].value
                answer = await self.registry.ask(run_id, question["kind"], question["prompt"])
                result = await self.graph.ainvoke(Command(resume=answer), config)
            if result.get("error") in ("cancelled_by_user", "invalid_edit"):
                await emit(events.failed(run_id, result["error"], []))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # anything unexpected ends the run visibly, never silently
            log.exception("run %s failed", run_id)
            await db.update_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")
            await emit(events.failed(run_id, type(e).__name__, [str(e)]))


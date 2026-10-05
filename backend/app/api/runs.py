"""REST: start a run, read a run."""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import ValidationError

from app import db
from app.intake.intake import parse_request
from app.models import MealSpec, NLRunRequest

router = APIRouter(prefix="/api/runs")


@router.post("")
async def create_run(request: Request, body: dict[str, Any] = Body(...)) -> dict:
    """Body is a MealSpec, or {"text": "..."} to describe it in words (one LLM call)."""
    state = request.app.state
    try:
        if "text" in body:
            spec = await parse_request(NLRunRequest.model_validate(body).text, state.deps.make_llm())
        else:
            spec = MealSpec.model_validate(body)
    except ValidationError as e:
        raise HTTPException(422, detail=e.errors(include_url=False, include_context=False)) from e
    run_id = await db.create_run(spec.user_id, spec.model_dump())
    state.runner.start(run_id, spec)
    return {"run_id": run_id, "status": "running", "ws_url": f"/api/runs/{run_id}/events"}


@router.get("/{run_id}")
async def get_run(request: Request, run_id: str) -> dict:
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    run["open_hitl_event"] = await db.get_open_hitl_event(run_id)
    run["last_seq"] = request.app.state.registry.get(run_id).bus.seq
    return run

"""Whole runs through the real API and websocket, with a fake LLM and a fake store.

No live LLM, USDA or Instacart calls.
"""

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.graph.runner import Deps
from app.main import create_app
from app.models import MealSpec
from app.shopping import shopper
from fakes import FakeSite, ScriptedLLM, cand, fake_lookup, rough_day, week

ITEMS = ["basmati_rice", "chicken_breast", "eggs", "masoor_dal", "onion", "turmeric_ground", "vegetable_oil"]
SECRET_CODE = "918273"


def catalog():
    return {("quicklly-grocery", i.replace("_", " ")): [cand(0, f"Store {i}", "2 lb", 4.0, 907.2)] for i in ITEMS}


def scripted_llm():
    return ScriptedLLM({
        "plan": [week(rough_day("Monday"))],
        "store": [shopper.StoreShortlist(stores=["quicklly-grocery"], hard_items=["masoor_dal"], reason="")],
        "probe": [shopper.Picks(picks=[shopper.Pick(item="quicklly-grocery: masoor_dal", index=0)])],
        "pick": [shopper.Picks(picks=[shopper.Pick(item=i, index=0) for i in ITEMS])],
    })


class World:
    """Fakes shared by the app under test; counts browser launches."""

    def __init__(self, login_code: bool = True):
        self.site = FakeSite(catalog())
        self.launches = 0
        self.login_code = login_code
        self.codes_received: list[str] = []
        self.llms: list[ScriptedLLM] = []

    def make_llm(self):
        llm = scripted_llm()
        self.llms.append(llm)
        return llm

    @asynccontextmanager
    async def open_site(self, user_id, ask):
        self.launches += 1
        if self.login_code:  # like a logged-out profile: Instacart asks for a code mid-shop
            self.codes_received.append(await ask("email_code", "Instacart sent you a code. What is it?", None))
        yield self.site

    def deps(self) -> Deps:
        return Deps(make_llm=self.make_llm, lookup=fake_lookup, open_site=self.open_site)


SPEC = MealSpec(zip_code="12345", days=1, include_snacks=False,
                macros={"calories": 2000, "protein_g": 150}).model_dump()


def receive_until(ws, wanted: str, limit: int = 200) -> tuple[dict, list[dict]]:
    seen = []
    for _ in range(limit):
        msg = ws.receive_json()
        seen.append(msg)
        if msg["type"] == wanted:
            return msg, seen
        if msg["type"] == "run.failed":
            raise AssertionError(f"run failed: {msg}")
    raise AssertionError(f"no {wanted} in {[m['type'] for m in seen]}")


def start(client, spec=SPEC) -> str:
    resp = client.post("/api/runs", json=spec)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running" and body["ws_url"] == f"/api/runs/{body['run_id']}/events"
    return body["run_id"]


def test_full_run_with_approval_and_login_pause():
    world = World()
    with TestClient(create_app(world.deps())) as client:
        run_id = start(client)
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            plan, seen = receive_until(ws, "plan.ready")
            assert plan["per_day"]["Monday"]["calories"] > 0 and len(plan["grocery_list"]["items"]) == 7

            approval, _ = receive_until(ws, "hitl.required")
            assert approval["kind"] == "plan_approval" and "7 items" in approval["prompt"]
            assert client.get(f"/api/runs/{run_id}").json()["status"] == "awaiting_hitl"
            assert world.launches == 0  # nothing touches Instacart before approval
            ws.send_json({"type": "hitl.response", "kind": "plan_approval", "value": "approved"})

            code_q, _ = receive_until(ws, "hitl.required")
            assert code_q["kind"] == "email_code"
            ws.send_json({"type": "hitl.response", "kind": "plan_approval", "value": "approved"})  # stale
            rejected, _ = receive_until(ws, "hitl.rejected")
            assert rejected["kind"] == "plan_approval"
            ws.send_json({"type": "hitl.response", "kind": "email_code", "value": SECRET_CODE,
                          "event_id": code_q["event_id"]})

            done, seen = receive_until(ws, "cart.ready")
            assert all(c["covered"] for c in done["report"]["coverage"])
            seqs = [m["seq"] for m in seen]
            assert seqs == sorted(seqs)

        assert world.launches == 1  # the login pause did not restart the browser
        assert world.codes_received == [SECRET_CODE]
        run = client.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "cart_ready" and run["open_hitl_event"] is None
        assert len(run["cart_report_json"]["lines"]) == 7
        assert run["llm_usage"] == {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                    "models": []}  # the fake LLM here makes no logged calls

    # The code went to the waiting shopper only: it is nowhere in the database.
    assert SECRET_CODE.encode() not in open(get_settings().database_path, "rb").read()
    assert SECRET_CODE.encode() not in open(get_settings().checkpoint_path, "rb").read()


def test_edit_grocery_list_at_approval():
    world = World(login_code=False)
    with TestClient(create_app(world.deps())) as client:
        run_id = start(client)
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            plan, _ = receive_until(ws, "plan.ready")
            receive_until(ws, "hitl.required")
            edited = plan["grocery_list"]
            edited["items"] = [i for i in edited["items"] if i["name"] != "turmeric_ground"]  # have it already
            ws.send_json({"type": "hitl.response", "kind": "plan_approval", "value": {"edit": edited}})
            done, _ = receive_until(ws, "cart.ready")
        assert "Store turmeric_ground" not in {l["product_name"] for l in done["report"]["lines"]}
        assert len(client.get(f"/api/runs/{run_id}").json()["grocery_list_json"]["items"]) == 6


def test_reject_cancels_without_shopping():
    world = World()
    with TestClient(create_app(world.deps())) as client:
        run_id = start(client)
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            receive_until(ws, "hitl.required")
            ws.send_json({"type": "hitl.response", "kind": "plan_approval", "value": "rejected"})
            msg = ws.receive_json()
            while msg["type"] != "run.failed":
                msg = ws.receive_json()
        assert msg["error"] == "cancelled_by_user" and world.launches == 0
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "failed"


def test_reconnect_replays_missed_events():
    world = World()
    with TestClient(create_app(world.deps())) as client:
        run_id = start(client)
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            _, first = receive_until(ws, "hitl.required")
        cut = first[1]["seq"]  # pretend we only saw the first two events
        with client.websocket_connect(f"/api/runs/{run_id}/events?since={cut}") as ws:
            approval, replayed = receive_until(ws, "hitl.required")
        assert [m["seq"] for m in replayed] == [m["seq"] for m in first if m["seq"] > cut]
        assert client.get(f"/api/runs/{run_id}").json()["last_seq"] == first[-1]["seq"]


def test_approval_survives_server_restart():
    world = World(login_code=False)
    with TestClient(create_app(world.deps())) as client:
        run_id = start(client)
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            receive_until(ws, "hitl.required")
    # server stopped while waiting for approval; a new process starts
    with TestClient(create_app(world.deps())) as client:
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "awaiting_hitl"
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            again, _ = receive_until(ws, "hitl.required")  # re-asked from the database
            assert again["kind"] == "plan_approval"
            ws.send_json({"type": "hitl.response", "kind": "plan_approval", "value": "approved"})
            receive_until(ws, "cart.ready")
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "cart_ready"
    assert [p for p, _ in world.llms[0].calls] == ["plan"]  # the plan was not regenerated


def test_shop_failure_is_reported_not_silent():
    world = World(login_code=False)

    @asynccontextmanager
    async def broken(user_id, ask):
        raise RuntimeError("Chrome profile is in use by another run")
        yield

    world.open_site = broken
    with TestClient(create_app(world.deps())) as client:
        run_id = start(client)
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            receive_until(ws, "hitl.required")
            ws.send_json({"type": "hitl.response", "kind": "plan_approval", "value": "approved"})
            msg = ws.receive_json()
            while msg["type"] != "run.failed":
                msg = ws.receive_json()
        assert "profile is in use" in msg["detail"][0]
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "failed"


@pytest.mark.parametrize("body", [{"zip_code": "12345"}, {"text": ""}])
def test_bad_request_is_422(body):
    with TestClient(create_app(World().deps())) as client:
        assert client.post("/api/runs", json=body).status_code == 422


def test_unknown_run():
    with TestClient(create_app(World().deps())) as client:
        assert client.get("/api/runs/run_nope").status_code == 404


def test_swap_a_meal_before_approving():
    from fakes import meal
    world = World(login_code=False)
    with TestClient(create_app(world.deps())) as client:
        run_id = start(client)
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            first_plan, _ = receive_until(ws, "plan.ready")
            assert "masoor_dal" in str(first_plan["meal_plan"]["days"][0]["meals"][2])
            receive_until(ws, "hitl.required")
            world.llms[0].replies["swap"] = [
                meal("dinner", [("chicken_breast", 150), ("basmati_rice", 80), ("onion", 50), ("vegetable_oil", 10)],
                     title="Chicken pulao"),
            ]
            ws.send_json({"type": "hitl.response", "kind": "plan_approval", "value": {"swap": {
                "day": "monday", "slot": "dinner", "reason": "not a fan of dal", "avoid": ["masoor dal"]}}})

            second_plan, seen = receive_until(ws, "plan.ready")
            progress = [m["message"] for m in seen if m["type"] == "run.progress"]
            assert "New dinner: Chicken pulao" in progress
            dinner = second_plan["meal_plan"]["days"][0]["meals"][2]
            assert dinner["title"] == "Chicken pulao" and dinner["slot"] == "dinner"
            # Same-day meals keep their dishes; only their grams are re-balanced to the day's targets.
            before, after = first_plan["meal_plan"]["days"][0]["meals"], second_plan["meal_plan"]["days"][0]["meals"]
            assert [m["title"] for m in after[:2]] == [m["title"] for m in before[:2]]
            assert [[i["name"] for i in m["ingredients"]] for m in after[:2]] == \
                   [[i["name"] for i in m["ingredients"]] for m in before[:2]]
            assert "masoor_dal" not in {i["name"] for i in second_plan["grocery_list"]["items"]}
            assert abs(second_plan["per_day"]["Monday"]["calories_delta"]) <= 100  # re-sized to target

            again, _ = receive_until(ws, "hitl.required")
            assert again["kind"] == "plan_approval" and world.launches == 0  # still nothing bought
            ws.send_json({"type": "hitl.response", "kind": "plan_approval", "value": "approved"})
            done, _ = receive_until(ws, "cart.ready")

        assert world.launches == 1
        assert "masoor dal" in client.get(f"/api/runs/{run_id}").json()["meal_spec_json"]["dislikes"]
        planning = [p for p, _ in world.llms[0].calls if p not in ("store", "probe", "pick")]
        assert planning == ["plan", "swap"]  # one call for the swap, no repairs needed
        swap_prompt = world.llms[0].calls[1][1]
        assert "not a fan of dal" in swap_prompt and "Monday's dinner" in swap_prompt


def test_swap_of_missing_meal_asks_again_without_llm():
    world = World(login_code=False)
    with TestClient(create_app(world.deps())) as client:
        run_id = start(client)
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            receive_until(ws, "hitl.required")
            ws.send_json({"type": "hitl.response", "kind": "plan_approval",
                          "value": {"swap": {"day": "Friday", "slot": "dinner"}}})  # 1-day plan
            again, seen = receive_until(ws, "hitl.required")
            assert again["kind"] == "plan_approval"
            assert "There's no dinner on Friday to swap" in [m.get("message") for m in seen]
        assert [p for p, _ in world.llms[0].calls] == ["plan"]

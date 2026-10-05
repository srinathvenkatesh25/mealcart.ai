"""Which Chrome windows open, and when. A recording stand-in replaces the real browser."""

import pytest

from app.config import get_settings
from app.shopping import instacart
from app.shopping.instacart import HumanCheckError, InstacartSite, LoginRequiredError, open_instacart


class Launches:
    """Records every BrowserSession opened: its headless flag, and whether it was closed."""

    def __init__(self, signed_in: list[bool]):
        self.signed_in = list(signed_in)      # answers for successive ensure_logged_in calls
        self.sessions: list[dict] = []
        self.logged_in_via: list[bool] = []   # headless flag of the session login() ran in

    def session_class(self):
        outer = self

        class FakeSession:
            def __init__(self, user_id, *, headless=False, profile_root=None):
                self.record = {"headless": headless, "closed": False}
                outer.sessions.append(self.record)
                self.page = self

            async def start(self):
                return self

            async def close(self):
                self.record["closed"] = True

        return FakeSession


@pytest.fixture
def world(monkeypatch):
    def make(mode: str, signed_in: list[bool]) -> Launches:
        monkeypatch.setenv("BROWSER_MODE", mode)
        get_settings.cache_clear()
        w = Launches(signed_in)
        monkeypatch.setattr(instacart, "BrowserSession", w.session_class())

        async def fake_ensure(page, url=None):
            return w.signed_in.pop(0)

        async def fake_login(page, ask):
            w.logged_in_via.append(page.record["headless"])

        monkeypatch.setattr(instacart, "ensure_logged_in", fake_ensure)
        monkeypatch.setattr(instacart, "login", fake_login)
        return w
    return make


async def use(ask=None):
    async with open_instacart("user_default", ask) as site:
        assert isinstance(site, InstacartSite)


async def test_auto_signed_in_runs_hidden_and_opens_one_window(world):
    w = world("auto", [True])
    await use()
    assert [s["headless"] for s in w.sessions] == [True] and w.sessions[0]["closed"]


async def test_auto_not_signed_in_shows_a_window_only_for_sign_in(world):
    w = world("auto", [False, True])
    await use()
    assert [s["headless"] for s in w.sessions] == [True, False, True]   # hidden check, visible login, hidden shop
    assert w.logged_in_via == [False]                                    # sign-in happened in the visible one
    assert all(s["closed"] for s in w.sessions)


async def test_auto_sign_in_that_does_not_stick_is_an_error(world):
    w = world("auto", [False, False])
    with pytest.raises(LoginRequiredError, match="didn't keep the session"):
        await use()
    assert all(s["closed"] for s in w.sessions)


async def test_hidden_never_opens_a_window_and_says_how_to_sign_in(world):
    w = world("hidden", [False])
    with pytest.raises(LoginRequiredError, match="scripts/login.py"):
        await use()
    assert [s["headless"] for s in w.sessions] == [True] and w.sessions[0]["closed"]


async def test_visible_is_the_old_behaviour(world):
    w = world("visible", [False])
    await use()
    assert [s["headless"] for s in w.sessions] == [False] and w.logged_in_via == [False]


async def test_window_is_closed_even_if_the_shop_step_fails(world):
    w = world("auto", [True])
    with pytest.raises(RuntimeError):
        async with open_instacart("user_default", None):
            raise RuntimeError("boom")
    assert w.sessions[0]["closed"]


def test_default_mode_is_auto():
    get_settings.cache_clear()
    assert get_settings().browser_mode == "auto"

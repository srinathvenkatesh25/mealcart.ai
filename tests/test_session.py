"""Real headless Chrome against a local fake Instacart."""

import shutil

import pytest
from playwright.async_api import Error as PlaywrightError

from app.shopping.session import BrowserSession, ProfileBusyError, ensure_logged_in, login
from fake_site import FakeInstacart

pytestmark = pytest.mark.skipif(
    not shutil.which("google-chrome") and not __import__("os").path.exists("/Applications/Google Chrome.app"),
    reason="needs Google Chrome",
)


@pytest.fixture
def site():
    s = FakeInstacart()
    yield s
    s.close()


def session(tmp_path) -> BrowserSession:
    return BrowserSession("user_test", headless=True, profile_root=str(tmp_path / "profiles"))


class ScriptedAnswers:
    def __init__(self, answers: dict[str, str]):
        self.answers, self.asked = answers, []

    async def __call__(self, kind, prompt, screenshot):
        self.asked.append(kind)
        return self.answers[kind]


async def test_login_with_code_then_second_launch_needs_no_login(site, tmp_path):
    ask = ScriptedAnswers({"login_email": "me@example.com", "email_code": "123456"})
    async with session(tmp_path) as s:
        assert not await ensure_logged_in(s.page, site.url)
        await login(s.page, ask, site.url, timeout_s=30)
        assert await ensure_logged_in(s.page, site.url)
    assert ask.asked == ["login_email", "email_code"]

    # Fresh browser, same profile: the session survived and nothing is asked.
    async with session(tmp_path) as s:
        assert await ensure_logged_in(s.page, site.url)


async def test_login_with_password(tmp_path):
    site = FakeInstacart(mode="password")
    try:
        ask = ScriptedAnswers({"login_email": "me@example.com", "login_password": "hunter22"})
        async with session(tmp_path) as s:
            await login(s.page, ask, site.url, timeout_s=30)
            assert await ensure_logged_in(s.page, site.url)
        assert ask.asked == ["login_email", "login_password"]
    finally:
        site.close()


async def test_code_that_submits_itself(tmp_path):
    # Seen on real Instacart: the code step has no Continue button.
    site = FakeInstacart(mode="autosubmit")
    try:
        ask = ScriptedAnswers({"login_email": "me@example.com", "email_code": "123456"})
        async with session(tmp_path) as s:
            await login(s.page, ask, site.url, timeout_s=30)
            assert await ensure_logged_in(s.page, site.url)
        assert ask.asked == ["login_email", "email_code"]
    finally:
        site.close()


async def test_guard_blocks_checkout_in_real_browser(site, tmp_path):
    async with session(tmp_path) as s:
        await s.page.goto(site.url)
        with pytest.raises(PlaywrightError, match="ERR_BLOCKED_BY_CLIENT"):
            await s.page.goto(site.url + "store/checkout")
        page = await s.context.new_page()  # the first tab is left on Chrome's error page
        await page.goto(site.url)
        statuses = await page.evaluate("""async (base) => {
            const post = async (op) => {
              try { return (await fetch(base + 'graphql', {method: 'POST',
                     body: JSON.stringify({operationName: op})})).status; }
              catch (e) { return 'blocked'; }
            };
            // Instacart sends some binary POST bodies (seen live: byte 0x8e); they must not crash the guard.
            const binary = (await fetch(base + 'beacon', {method: 'POST',
                            body: new Uint8Array([0x1f, 0x8b, 0x8e, 0xff, 0x00])})).status;
            return [await post('UpdateCartItemsMutation'), await post('PlaceOrderMutation'), binary];
        }""", site.url)
    assert statuses == [200, "blocked", 200]
    assert not any("checkout" in r or "PlaceOrder" in r for r in site.requests)  # never reached the server
    assert any("checkout" in b for b in s.blocked) and any("PlaceOrder" in b for b in s.blocked)


async def test_profile_can_only_be_opened_once(site, tmp_path):
    async with session(tmp_path):
        with pytest.raises(ProfileBusyError):
            await session(tmp_path).start()
    async with session(tmp_path):  # released after close
        pass


async def test_profile_dir_is_private(tmp_path):
    async with session(tmp_path) as s:
        assert oct(s.profile_dir.stat().st_mode & 0o777) == "0o700"

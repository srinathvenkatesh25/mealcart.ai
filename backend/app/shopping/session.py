"""Persistent Instacart browser session. Playwright only, no LLM.

Your real Chrome runs with a profile under profiles/<user_id>/, so a login
survives between runs. Only one run may open a profile at a time. Every
context gets the checkout guard before any page loads.
"""

import asyncio
import fcntl
import os
import random
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.config import get_settings
from app.models import HitlKind
from app.shopping import guard

HOME_URL = "https://www.instacart.com/"
LOGIN_TIMEOUT_S = 600

# ask(kind, prompt, screenshot_png) -> the user's answer. The CLI asks in the
# terminal; Phase 6 routes the same call through the chat UI.
Ask = Callable[[HitlKind, str, bytes | None], Awaitable[str]]


class ProfileBusyError(Exception):
    """Another run already has this user's browser profile open."""


class LoginFailedError(Exception):
    pass


async def human_pause(lo: float = 0.3, hi: float = 0.9) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


class ProfileLock:
    """Exclusive, non-blocking lock on a profile directory (released if the process dies)."""

    def __init__(self, profile_dir: Path):
        self.path = profile_dir / ".mealcart.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ProfileBusyError(f"{self.path.parent} is in use by another run") from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class BrowserSession:
    def __init__(self, user_id: str, *, headless: bool = False, profile_root: str | None = None):
        self.profile_dir = Path(profile_root or get_settings().profile_root) / user_id
        self.headless = headless
        self.blocked: list[str] = []  # everything the guard stopped, for the run report
        self._lock = ProfileLock(self._ensure_profile_dir())
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def _ensure_profile_dir(self) -> Path:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile_dir.parent, 0o700)
        os.chmod(self.profile_dir, 0o700)  # session cookies live here
        return self.profile_dir

    async def start(self) -> "BrowserSession":
        self._lock.acquire()
        try:
            self._pw = await async_playwright().start()
            # Headful by default: CAPTCHAs need a person, and you can watch the run.
            self.context = await self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), channel="chrome", headless=self.headless,
                viewport={"width": 1366, "height": 900},
            )
            await guard.install(self.context, self.blocked)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
        self._lock.release()

    async def __aenter__(self) -> "BrowserSession":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.close()


async def _login_button_visible(page: Page) -> bool:
    button = page.get_by_role("button", name="Log in", exact=True)
    return await button.count() > 0 and await button.first.is_visible()


async def ensure_logged_in(page: Page, url: str = HOME_URL) -> bool:
    """True if the saved profile is still signed in (the homepage offers no 'Log in')."""
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)  # header renders client-side
    return not await _login_button_visible(page)


async def _visible(locator) -> bool:
    return await locator.count() > 0 and await locator.first.is_visible()


async def captcha_present(page: Page) -> bool:
    frames = page.locator("iframe[src*='captcha'], iframe[src*='challenge'], iframe[title*='challenge' i]")
    text = page.get_by_text(re.compile(r"verify (that )?you('re| are) (a )?human|press (and|&) hold", re.I))
    return await _visible(frames) or await _visible(text)


async def _type_slowly(locator, text: str) -> None:
    await locator.click()
    await human_pause(0.2, 0.5)
    await locator.press_sequentially(text, delay=random.randint(60, 120))


async def _continue(page: Page) -> None:
    """Click the dialog's submit button if there is one.

    The page header has its own 'Log in' button, so look inside the dialog.
    Instacart submits a verification code by itself once all digits are typed
    (seen live), so a missing button is normal, not an error.
    """
    await human_pause()
    dialog = page.get_by_role("dialog")
    scope = dialog.first if await _visible(dialog) else page
    button = scope.get_by_role("button", name=re.compile(r"^(continue|log in|submit|verify)$", re.I)).first
    try:
        await button.click(timeout=4000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(2500)


async def login(page: Page, ask: Ask, url: str = HOME_URL, timeout_s: int = LOGIN_TIMEOUT_S) -> None:
    """Sign in by asking you for each piece (email, code or password, CAPTCHA).

    Codes and passwords pass straight from ask() into the page; they are never
    stored or logged.
    """
    if await ensure_logged_in(page, url):
        return
    await page.get_by_role("button", name="Log in", exact=True).first.click()
    await page.wait_for_timeout(1500)

    email_box = page.locator("input[type=email]")
    if not await _visible(email_box):
        raise LoginFailedError("login dialog did not show an email field")
    await _type_slowly(email_box.first, await ask("login_email", "Your Instacart email address?", None))
    await _continue(page)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await captcha_present(page):
            await ask("captcha", "Instacart is showing a CAPTCHA. Solve it in the browser window, "
                                 "then reply 'done'.", await page.screenshot())
            await page.wait_for_timeout(2000)
            continue

        code_box = page.locator("input[autocomplete='one-time-code'], input[inputmode='numeric']")
        if await _visible(code_box):
            code = await ask("email_code", "Instacart sent you a verification code. What is it?", None)
            await _type_slowly(code_box.first, code.strip())
            await _continue(page)
            continue

        password_box = page.locator("input[type=password]")
        if await _visible(password_box):
            await _type_slowly(password_box.first, await ask("login_password", "Your Instacart password?", None))
            await _continue(page)
            continue

        if not await _visible(page.get_by_role("dialog")) and await ensure_logged_in(page, url):
            return
        await page.wait_for_timeout(1500)

    raise LoginFailedError(f"not logged in after {timeout_s}s")

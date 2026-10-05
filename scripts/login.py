"""Sign in to Instacart once, in the app's own Chrome profile.

    python scripts/login.py            # the script asks you for email, code/password here
    python scripts/login.py --manual   # log in yourself in the Chrome window
    python scripts/login.py --check    # is the saved session still signed in?

Codes and passwords are read with getpass (not echoed) and typed straight
into the page; nothing is saved except Chrome's own cookies in profiles/.
"""

import argparse
import asyncio
import getpass
import sys

sys.path.insert(0, "backend")

from app.config import get_settings  # noqa: E402
from app.shopping.session import BrowserSession, ensure_logged_in, login  # noqa: E402

SECRET_KINDS = {"email_code", "login_password"}


async def ask_in_terminal(kind: str, prompt: str, screenshot: bytes | None) -> str:
    reader = getpass.getpass if kind in SECRET_KINDS else input
    return await asyncio.to_thread(reader, f"\n{prompt}\n> ")


async def main(mode: str) -> int:
    user_id = get_settings().single_user_id
    async with BrowserSession(user_id) as s:
        if await ensure_logged_in(s.page):
            print("Already signed in. The saved session works.")
            return 0
        if mode == "check":
            print("Not signed in. Run: python scripts/login.py")
            return 1
        if mode == "manual":
            print("Log in to Instacart in the Chrome window. Waiting (up to 10 min)…")
            for _ in range(120):
                await asyncio.sleep(5)
                # Only check once the login dialog is closed, so we don't navigate away mid-login.
                if not await s.page.get_by_role("dialog").count() and await ensure_logged_in(s.page):
                    break
            else:
                print("Timed out.")
                return 1
        else:
            await login(s.page, ask_in_terminal)
        print("Signed in. Closing Chrome; run with --check to confirm the session was saved.")
        if s.blocked:
            print("Guard blocked:", s.blocked)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--manual", action="store_const", dest="mode", const="manual")
    g.add_argument("--check", action="store_const", dest="mode", const="check")
    sys.exit(asyncio.run(main(p.parse_args().mode or "assisted")))

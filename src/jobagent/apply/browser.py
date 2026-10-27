"""Browser driver for filling application forms.

`FormDriver` is the small interface the engine uses; `PlaywrightDriver` implements it with a real Chrome
window (visible by default: sites block headless browsers more often, and Riya can watch). Tests use a fake
driver, so the engine logic is tested without a browser.

The tool never solves or bypasses CAPTCHAs. Invisible ones (reCAPTCHA Enterprise on Greenhouse/Ashby,
hCaptcha on Lever, seen live) are left alone; if a visible challenge appears, the job goes to Manual.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from pathlib import Path
from typing import Any, Protocol

from .mapper import choose_option

log = logging.getLogger("jobagent.browser")
EXTRACT_JS = (Path(__file__).with_name("extract_fields.js")).read_text(encoding="utf-8")

CONFIRM = re.compile(r"thank(s| you)( so much)? for (applying|your (application|interest))|"
                     r"application (was |has been )?(successfully )?(submitted|received)|"
                     r"we('ve| have) received your application|successfully (submitted|applied)|"
                     r"your application has been sent", re.I)
CONFIRM_URL = re.compile(r"/(confirmation|thanks|thank-you|thank_you|success|submitted)\b", re.I)

# Runs in the page after submit: is a CAPTCHA challenge now visible, or are there field errors?
POST_SUBMIT_JS = r"""(() => {
  const big = (f) => { const r = f.getBoundingClientRect(); const st = getComputedStyle(f);
    return r.width > 120 && r.height > 120 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const challenge = Array.from(document.querySelectorAll('iframe')).some(f =>
    /recaptcha\/.*bframe|hcaptcha.*(challenge|frame=challenge)|challenges\.cloudflare|arkoselabs/i.test(f.src || '') && big(f));
  const errs = Array.from(document.querySelectorAll('[role=alert], .error, .field-error, .error-message, [class*="error" i]'))
    .map(e => (e.innerText || '').trim()).filter(t => t && t.length < 200 && /required|invalid|error|please/i.test(t));
  return { challenge, errors: [...new Set(errs)].slice(0, 5), url: location.href, text: (document.body.innerText || '').slice(0, 4000) };
})()"""


class FormDriver(Protocol):
    async def goto(self, url: str) -> None: ...
    async def extract(self) -> dict: ...
    async def fill(self, selector: str, value: str) -> None: ...
    async def select(self, selector: str, value: str) -> None: ...
    async def check(self, selector: str) -> None: ...
    async def upload(self, selector: str, path: str) -> None: ...
    async def combobox(self, selector: str, text: str) -> str | None: ...
    async def click(self, selector: str) -> None: ...
    async def screenshot(self, path: Path) -> None: ...
    async def post_submit_state(self) -> dict: ...
    async def pause(self, seconds: float) -> None: ...


class PlaywrightDriver:
    """Real browser. Use `async with PlaywrightDriver(...) as d:`."""

    def __init__(self, profile_dir: Path, headless: bool = False, slow_mo_ms: int = 40,
                 executable_path: str | None = None):
        self.profile_dir = profile_dir
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.executable_path = executable_path
        self._pw = None
        self._ctx = None
        self.page = None

    async def __aenter__(self) -> "PlaywrightDriver":
        from playwright.async_api import async_playwright   # imported lazily: only needed when applying
        self._pw = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"headless": self.headless, "slow_mo": self.slow_mo_ms,
                                  "viewport": {"width": 1280, "height": 900}, "locale": "en-US",
                                  "timezone_id": "America/New_York"}
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        self._ctx = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self.page.set_default_timeout(15000)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass                                    # some pages keep long-polling; the form is there anyway
        await self.page.wait_for_timeout(800)

    async def extract(self) -> dict:
        return await self.page.evaluate(EXTRACT_JS)

    async def fill(self, selector: str, value: str) -> None:
        loc = self.page.locator(selector).first
        await loc.scroll_into_view_if_needed()
        await loc.fill(value)

    async def select(self, selector: str, value: str) -> None:
        loc = self.page.locator(selector).first
        try:
            await loc.select_option(value=value)
        except Exception:
            await loc.select_option(label=value)

    async def check(self, selector: str) -> None:
        loc = self.page.locator(selector).first
        try:
            await loc.check(force=True)
        except Exception:
            el_id = await loc.get_attribute("id")
            if el_id:
                await self.page.locator(f'label[for="{el_id}"]').first.click()
            else:
                raise

    async def upload(self, selector: str, path: str) -> None:
        await self.page.locator(selector).first.set_input_files(path)
        await self.page.wait_for_timeout(1500)       # uploads parse the file before the form accepts it

    async def combobox(self, selector: str, text: str) -> str | None:
        """Type into a searchable dropdown and pick the option that best matches `text`."""
        loc = self.page.locator(selector).first
        await loc.scroll_into_view_if_needed()
        await loc.click()
        # Typed text filters the list; when Riya's wording differs from the options ("Decline to self-identify"
        # vs "I don't wish to answer"), the last attempt clears the box so every option shows, and the best
        # match is picked by meaning. Verified live: Greenhouse options are role="option" elements.
        queries = list(dict.fromkeys([text, text.split(",")[0], text.split(" ")[0], ""]))
        for query in queries:
            await loc.fill("")
            if query:
                await loc.type(query, delay=35)
            else:
                await loc.press("ArrowDown")
            await self.page.wait_for_timeout(1200)
            opts = self.page.locator("[role=option]:visible")
            n = await opts.count()
            texts = [((await opts.nth(i).inner_text()) or "").strip() for i in range(min(n, 30))]
            pick = choose_option([{"text": t, "value": str(i)} for i, t in enumerate(texts)], text)
            if pick is not None:
                await opts.nth(int(pick["value"])).click()
                return pick["text"]
        await loc.press("Escape")
        return None

    async def click(self, selector: str) -> None:
        loc = self.page.locator(selector).first
        await loc.scroll_into_view_if_needed()
        await loc.click()

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=True)

    async def post_submit_state(self) -> dict:
        return await self.page.evaluate(POST_SUBMIT_JS)

    async def pause(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def wait_for_outcome(driver: FormDriver, timeout_s: float = 30.0, step_s: float = 1.5) -> tuple[str, str]:
    """After clicking submit: ("confirmed"|"challenge"|"error"|"unknown", detail)."""
    waited = 0.0
    last: dict = {}
    while waited <= timeout_s:
        state = await driver.post_submit_state()
        last = state
        if CONFIRM_URL.search(state.get("url", "")) or CONFIRM.search(state.get("text", "")):
            return "confirmed", (CONFIRM.search(state.get("text", "")) or CONFIRM_URL.search(state["url"])).group(0)
        if state.get("challenge"):
            return "challenge", "a CAPTCHA challenge appeared after submit"
        await driver.pause(step_s)
        waited += step_s
    if last.get("errors"):
        return "error", "; ".join(last["errors"])[:300]
    return "unknown", "no confirmation message detected"


def human_pause(bounds: list[float] | tuple[float, float]) -> float:
    lo, hi = float(bounds[0]), float(bounds[1])
    return random.uniform(lo, hi)

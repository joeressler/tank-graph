"""Optional Chromium bootstrap for wiki-host pass cookies.

requests never executes the JS cookie challenge. This module is not the crawler:
it keeps one Chromium session, waits for MediaWiki content, and exports the
wiki-host Cookie header for reuse on the shared HTTP client.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .config import ExtractorConfig
from .errors import BotInterstitialError, ConfigurationError
from .interstitial import (
    cookie_pairs,
    is_bot_interstitial,
    looks_like_mediawiki_html,
    looks_like_mediawiki_json,
    validate_wiki_cookie,
)

_DEFAULT_WAIT_MS = 45_000
_COOKIE_PROBE_QUERY = "action=query&format=json&list=allpages&aplimit=1"
_STEALTH_INIT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)
_LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)
_READY_JS = """() => {
  const text = ((document.body && document.body.innerText) || "").trim();
  if (text.startsWith("{") || text.startsWith("[")) return true;
  return !!document.querySelector("#mw-content-text");
}"""
_LOG = logging.getLogger(__name__)


def _playwright_sync_api() -> tuple[
    Callable[[], Any],
    type[BaseException],
    type[BaseException],
]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise ConfigurationError(
            "Playwright is not installed; install it before bootstrapping wiki cookies"
        ) from error
    return sync_playwright, PlaywrightError, PlaywrightTimeout


def require_playwright_chromium(
    *,
    playwright_api: Callable[[], tuple[Callable[[], Any], type[BaseException], type[BaseException]]]
    | None = None,
) -> None:
    """Fail before launch when Chromium cannot be started."""

    try:
        factory, playwright_error, _timeout = (
            playwright_api() if playwright_api is not None else _playwright_sync_api()
        )
    except ImportError as error:
        raise ConfigurationError(
            "Playwright is not installed; install it before bootstrapping wiki cookies"
        ) from error
    runtime = factory().start()
    try:
        try:
            browser = runtime.chromium.launch(headless=True)
        except playwright_error as error:
            raise ConfigurationError("Playwright Chromium is unavailable") from error
        browser.close()
    finally:
        stop = getattr(runtime, "stop", None)
        if stop is not None:
            stop()


def _cookie_header(cookies: list[Any]) -> str:
    parts: list[str] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if isinstance(name, str) and name and isinstance(value, str):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _cookie_refresh_url(config: ExtractorConfig, page_url: str | None) -> str:
    origin = page_url or f"{config.wiki_endpoint}?{_COOKIE_PROBE_QUERY}"
    if not config.is_wiki_host(origin):
        raise ConfigurationError("cookie bootstrap URL must stay on the configured wiki host")
    if urlsplit(origin).path.rstrip("/").endswith("Special:AllPages"):
        raise ConfigurationError("do not bootstrap cookies from HTML Special:AllPages")
    return origin


def _playwright_cookie_payload(header: str, origin: str) -> list[dict[str, str]]:
    domain = urlsplit(origin).hostname
    if not domain:
        return []
    return [
        {"name": name, "value": value, "domain": domain, "path": "/"}
        for name, value in cookie_pairs(header)
    ]


def _page_is_ready(html: str, text: str) -> bool:
    if is_bot_interstitial(html) or is_bot_interstitial(text):
        return False
    return (
        looks_like_mediawiki_json(html)
        or looks_like_mediawiki_json(text)
        or looks_like_mediawiki_html(html)
    )


class PlaywrightCookieBroker:
    """Keep one Chromium context and export a fresh wiki Cookie header on demand."""

    def __init__(
        self,
        config: ExtractorConfig,
        *,
        page_url: str | None = None,
        timeout_ms: int = _DEFAULT_WAIT_MS,
        playwright_api: Callable[
            [], tuple[Callable[[], Any], type[BaseException], type[BaseException]]
        ]
        | None = None,
        headless: bool = True,
    ) -> None:
        self.config = config
        self._page_url = page_url
        self._timeout_ms = timeout_ms
        self._playwright_api = playwright_api
        self._headless = headless
        self._headed_fallback_used = False
        self._runtime: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._playwright_error: type[BaseException] = Exception
        self._playwright_timeout: type[BaseException] = Exception

    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        try:
            factory, playwright_error, playwright_timeout = (
                self._playwright_api()
                if self._playwright_api is not None
                else _playwright_sync_api()
            )
        except ImportError as error:
            raise ConfigurationError(
                "Playwright is not installed; install it before bootstrapping wiki cookies"
            ) from error
        self._playwright_error = playwright_error
        self._playwright_timeout = playwright_timeout
        runtime = factory().start()
        self._runtime = runtime
        try:
            self._browser = runtime.chromium.launch(
                headless=self._headless,
                args=list(_LAUNCH_ARGS),
                ignore_default_args=["--enable-automation"],
            )
        except playwright_error as error:
            self.close()
            raise ConfigurationError("Playwright Chromium is unavailable") from error

    def _ensure_page(self, existing_cookie: str | None) -> Any:
        if self._page is not None and self._context is not None:
            return self._page
        self._ensure_browser()
        browser = self._browser
        if browser is None:
            raise ConfigurationError("Playwright Chromium is unavailable")
        origin = _cookie_refresh_url(self.config, self._page_url)
        context = browser.new_context(
            user_agent=self.config.user_agent,
            locale="en-US",
            viewport={"width": 1280, "height": 720},
        )
        init = getattr(context, "add_init_script", None)
        if callable(init):
            with contextlib.suppress(Exception):
                init(_STEALTH_INIT)
        if existing_cookie:
            adder = getattr(context, "add_cookies", None)
            if adder is not None:
                adder(_playwright_cookie_payload(existing_cookie, origin))
        self._context = context
        self._page = context.new_page()
        return self._page

    def _goto_probe(self, page: Any) -> None:
        origin = _cookie_refresh_url(self.config, self._page_url)
        page.goto(origin, wait_until="domcontentloaded", timeout=self._timeout_ms)
        waiter = getattr(page, "wait_for_function", None)
        if callable(waiter):
            waiter(_READY_JS, timeout=self._timeout_ms)
        else:
            page.wait_for_selector("#mw-content-text", timeout=self._timeout_ms)
        html = ""
        content = getattr(page, "content", None)
        if callable(content):
            html = str(content())
        text = html
        inner = getattr(page, "inner_text", None)
        if callable(inner):
            with contextlib.suppress(Exception):
                text = str(inner("body"))
        if not _page_is_ready(html, text):
            raise BotInterstitialError(
                origin,
                "Playwright loaded a page without MediaWiki content",
                1,
            )

    def _export(self) -> str:
        context = self._context
        if context is None:
            raise ConfigurationError("Playwright cookie context is not open")
        return validate_wiki_cookie(_cookie_header(list(context.cookies())))

    def refresh(
        self,
        existing_cookie: str | None = None,
        *,
        force_navigate: bool = False,
    ) -> str:
        """Reuse one Chromium context; navigate only when the page is missing or stale."""

        origin = _cookie_refresh_url(self.config, self._page_url)
        created = self._page is None
        page = self._ensure_page(existing_cookie)
        should_navigate = force_navigate or created
        try:
            if not should_navigate:
                return self._export()
            self._goto_probe(page)
            return self._export()
        except self._playwright_timeout as error:
            if not self._headed_fallback_used and self._headless:
                _LOG.warning(
                    "headless cookie refresh failed; retrying with a visible Chromium window"
                )
                self._headed_fallback_used = True
                self._headless = False
                self.close()
                return self.refresh(existing_cookie=existing_cookie, force_navigate=True)
            raise BotInterstitialError(
                origin,
                "Playwright did not reach MediaWiki JSON or #mw-content-text; "
                "JS cookie challenge did not complete",
                1,
            ) from error

    def close(self) -> None:
        page = self._page
        self._page = None
        if page is not None:
            closer = getattr(page, "close", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    closer()
        context = self._context
        self._context = None
        if context is not None:
            with contextlib.suppress(Exception):
                context.close()
        browser = self._browser
        self._browser = None
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            stop = getattr(runtime, "stop", None)
            if stop is not None:
                with contextlib.suppress(Exception):
                    stop()


def export_wiki_cookies(
    config: ExtractorConfig,
    *,
    page_url: str | None = None,
    timeout_ms: int = _DEFAULT_WAIT_MS,
    existing_cookie: str | None = None,
    playwright_api: Callable[[], tuple[Callable[[], Any], type[BaseException], type[BaseException]]]
    | None = None,
) -> str:
    """Open the wiki API, wait for MediaWiki JSON or article HTML, and export Cookie."""

    broker = PlaywrightCookieBroker(
        config,
        page_url=page_url,
        timeout_ms=timeout_ms,
        playwright_api=playwright_api,
    )
    try:
        return broker.refresh(existing_cookie=existing_cookie)
    finally:
        broker.close()

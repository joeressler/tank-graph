from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tank_graph_extractor.browser import (
    PlaywrightCookieBroker,
    export_wiki_cookies,
    require_playwright_chromium,
)
from tank_graph_extractor.config import ExtractorConfig
from tank_graph_extractor.errors import BotInterstitialError, ConfigurationError

ARTICLE_HTML = (
    "<html><body><div id='mw-content-text'>"
    "<div class='mw-parser-output'>ok</div></div></body></html>"
)
WAIT_HTML = "<html><head><title>Loading site please wait...</title></head><body></body></html>"
PROBE_URL = (
    "https://wiki.wargaming.net/api.php?action=query&format=json&list=allpages&aplimit=1"
)


class FakePlaywrightTimeout(Exception):
    pass


class FakePlaywrightError(Exception):
    pass


class FakePlaywrightRuntime:
    def __init__(
        self,
        *,
        launch_error: BaseException | None = None,
        wait_error: BaseException | None = None,
        html: str = ARTICLE_HTML,
        cookies: list[dict[str, str]] | None = None,
    ) -> None:
        self.launch_error = launch_error
        self.wait_error = wait_error
        self.html = html
        self.exported_cookies = cookies or [
            {"name": "SPSI", "value": "1"},
            {"name": "SPSE", "value": "2"},
            {"name": "spcsrf", "value": "3"},
        ]
        self.headless: bool | None = None
        self.user_agent: str | None = None
        self.gotos: list[str] = []
        self.waited_selectors: list[str] = []
        self.wait_for_function_calls: list[str] = []
        self.added_cookies: list[dict[str, str]] = []
        self.closed: list[str] = []
        self.stopped = False
        self.launches = 0
        self.chromium = _FakeBrowser(self)

    def start(self) -> FakePlaywrightRuntime:
        return self

    def stop(self) -> None:
        self.stopped = True
        self.closed.append("playwright")


class _FakePage:
    def __init__(self, runtime: FakePlaywrightRuntime) -> None:
        self._runtime = runtime

    def goto(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout: int | None = None,
    ) -> None:
        del wait_until, timeout
        self._runtime.gotos.append(url)

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        del timeout
        self._runtime.waited_selectors.append(selector)
        if self._runtime.wait_error is not None:
            raise self._runtime.wait_error

    def wait_for_function(self, script: str, timeout: int | None = None) -> None:
        del timeout
        self._runtime.wait_for_function_calls.append(script)
        if self._runtime.wait_error is not None:
            raise self._runtime.wait_error

    def inner_text(self, selector: str = "body") -> str:
        del selector
        return self._runtime.html

    def content(self) -> str:
        return self._runtime.html

    def close(self) -> None:
        self._runtime.closed.append("page")


class _FakeContext:
    def __init__(self, runtime: FakePlaywrightRuntime) -> None:
        self._runtime = runtime

    def new_page(self) -> _FakePage:
        return _FakePage(self._runtime)

    def cookies(self) -> list[dict[str, str]]:
        return list(self._runtime.exported_cookies)

    def add_cookies(self, cookies: list[dict[str, str]]) -> None:
        self._runtime.added_cookies.extend(cookies)

    def add_init_script(self, script: str) -> None:
        del script

    def close(self) -> None:
        self._runtime.closed.append("context")


class _FakeBrowser:
    def __init__(self, runtime: FakePlaywrightRuntime) -> None:
        self._runtime = runtime

    def launch(self, headless: bool = True, **kwargs: Any) -> _FakeBrowser:
        del kwargs
        self._runtime.headless = headless
        self._runtime.launches += 1
        if self._runtime.launch_error is not None:
            raise self._runtime.launch_error
        return self

    def new_context(self, user_agent: str | None = None, **kwargs: Any) -> _FakeContext:
        del kwargs
        self._runtime.user_agent = user_agent
        return _FakeContext(self._runtime)

    def close(self) -> None:
        self._runtime.closed.append("browser")


def install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    runtime: FakePlaywrightRuntime | None = None,
) -> FakePlaywrightRuntime:
    active = runtime or FakePlaywrightRuntime()

    def api() -> tuple[
        Callable[[], FakePlaywrightRuntime],
        type[BaseException],
        type[BaseException],
    ]:
        return lambda: active, FakePlaywrightError, FakePlaywrightTimeout

    monkeypatch.setattr("tank_graph_extractor.browser._playwright_sync_api", api)
    return active


def test_require_playwright_missing_package_fails_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing() -> tuple[Any, type[BaseException], type[BaseException]]:
        raise ImportError("playwright is not installed")

    monkeypatch.setattr("tank_graph_extractor.browser._playwright_sync_api", missing)
    with pytest.raises(ConfigurationError, match="Playwright is not installed"):
        require_playwright_chromium()


def test_require_playwright_missing_chromium_fails_before_live_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_playwright(
        monkeypatch,
        FakePlaywrightRuntime(launch_error=FakePlaywrightError("executable not found")),
    )
    with pytest.raises(ConfigurationError, match="Playwright Chromium is unavailable"):
        require_playwright_chromium()


def test_cookie_bootstrap_waits_for_mediawiki_and_exports_wiki_host_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = install_fake_playwright(monkeypatch)
    live = ExtractorConfig()
    header = export_wiki_cookies(live)
    assert header == "SPSI=1; SPSE=2; spcsrf=3"
    assert runtime.headless is True
    assert runtime.user_agent == live.user_agent
    assert runtime.gotos == [PROBE_URL]
    assert runtime.wait_for_function_calls
    assert runtime.stopped is True


def test_cookie_bootstrap_timeout_does_not_retry_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_playwright(
        monkeypatch,
        FakePlaywrightRuntime(
            wait_error=FakePlaywrightTimeout("Timeout 45000ms exceeded"),
            html=WAIT_HTML,
        ),
    )
    with pytest.raises(BotInterstitialError, match="MediaWiki JSON or #mw-content-text"):
        export_wiki_cookies(ExtractorConfig())


def test_cookie_broker_reuses_chromium_across_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = install_fake_playwright(monkeypatch)
    broker = PlaywrightCookieBroker(ExtractorConfig())
    assert broker.refresh() == "SPSI=1; SPSE=2; spcsrf=3"
    assert broker.refresh() == "SPSI=1; SPSE=2; spcsrf=3"
    assert runtime.launches == 1
    assert runtime.gotos == [PROBE_URL]
    assert broker.refresh(force_navigate=True) == "SPSI=1; SPSE=2; spcsrf=3"
    assert runtime.gotos == [PROBE_URL, PROBE_URL]
    broker.close()
    assert runtime.stopped is True
    assert "page" in runtime.closed
    assert "context" in runtime.closed
    assert "browser" in runtime.closed


def test_cookie_refresh_seeds_existing_cookies_and_accepts_api_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = install_fake_playwright(
        monkeypatch,
        FakePlaywrightRuntime(html='{"query":{"allpages":[]}}'),
    )
    live = ExtractorConfig()
    header = export_wiki_cookies(live, existing_cookie="SPSI=seed; SPSE=seed")
    assert header == "SPSI=1; SPSE=2; spcsrf=3"
    assert runtime.gotos == [PROBE_URL]
    assert runtime.added_cookies[0]["name"] == "SPSI"
    assert runtime.added_cookies[0]["domain"] == "wiki.wargaming.net"

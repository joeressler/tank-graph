from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tank_graph_extractor.config import ExtractorConfig
from tank_graph_extractor.extractor import run_extraction

SCHEMA = Path(__file__).parents[2] / "docs" / "specs" / "tanks-data.schema.json"


class Response:
    def __init__(
        self,
        *,
        url: str,
        payload: Any | None = None,
        text: str = "",
        status_code: int = 200,
    ) -> None:
        self.url = url
        self._payload = payload
        self.text = text
        self.content = text.encode()
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FixtureClient:
    retry_count = 0

    def __init__(self, config: ExtractorConfig) -> None:
        self.config = config

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        accepted_statuses: object = (),
        allow_robots: bool = False,
        source_kind: str | None = None,
    ) -> Response:
        if url.endswith("/robots.txt"):
            return Response(url=url, text="User-agent: *\nAllow: /\n")
        if url == self.config.guide_urls[0]:
            return Response(
                url=url,
                text="""
                    <html><title>Heavy Tank Tactics</title><main>
                    <h1>Heavy Tanks</h1>
                    <h2>Battlefield tactics</h2>
                    <p>Use sidescraping to protect your hull armor while you
                    trade damage from cover and avoid exposing weak positions.</p>
                    </main></html>
                """,
            )
        if params and params.get("list") == "categorymembers":
            if params["cmtitle"] == "Category:Tanks":
                members = [
                    {
                        "pageid": 20,
                        "ns": 14,
                        "type": "subcat",
                        "title": "Category:Heavy Tanks",
                    }
                ]
            else:
                members = [
                    {
                        "pageid": 4,
                        "ns": 0,
                        "type": "page",
                        "title": "Tank:Tiger I",
                    }
                ]
            return Response(
                url=url,
                payload={"query": {"categorymembers": members}},
            )
        if params and params.get("prop") == "revisions|pageprops":
            return Response(
                url=url,
                payload={
                    "query": {
                        "pages": {
                            "4": {
                                "pageid": 4,
                                "title": "Tank:Tiger I",
                                "pageprops": {"displaytitle": "Tiger I"},
                                "revisions": [
                                    {
                                        "*": (
                                            "{{Vehicle|id=G04_PzVI_Tiger_I|"
                                            "name=Tiger I|class=heavy tank|"
                                            "nation=germany|tier=7}}\n"
                                            "== Performance ==\n"
                                            "Use long-range sniping because the gun "
                                            "has high accuracy."
                                        )
                                    }
                                ],
                            }
                        }
                    }
                },
            )
        raise AssertionError(f"unexpected request: {url} {params}")

    def close(self) -> None:
        return None


class Doc:
    ents: tuple[object, ...] = ()
    noun_chunks: tuple[object, ...] = ()

    def __init__(self, text: str) -> None:
        self.text = text
        self.sents = (SimpleNamespace(text=text),)

    def __iter__(self) -> object:
        return iter(())


class Nlp:
    def pipe(self, texts: list[str], batch_size: int = 64) -> list[Doc]:
        return [Doc(text) for text in texts]

    def __call__(self, text: str) -> Doc:
        return Doc(text)


def test_complete_fixture_pipeline_writes_schema_valid_json(tmp_path: Path) -> None:
    output = tmp_path / "tanks_data.json"
    config = ExtractorConfig(
        wiki_endpoint="https://wiki.test/api.php",
        guide_root="https://guide.test/en/content/guide/",
        guide_paths=("newcomers-guide/getting_started/",),
        output_path=output,
        test_mode=True,
        request_interval=0,
    )
    summary = run_extraction(
        config,
        schema_path=SCHEMA,
        client=FixtureClient(config),  # type: ignore[arg-type]
        sentence_nlp=Nlp(),
    )

    records = json.loads(output.read_text(encoding="utf-8"))
    assert summary.accepted_tank_count == 1
    assert summary.category_count == 2
    assert records[0]["name"] == "G04_PzVI_Tiger_I"
    assert records[0]["strategies"] == [
        "Use long-range sniping because the gun has high accuracy.",
        (
            "Use sidescraping to protect your hull armor while you trade damage "
            "from cover and avoid exposing weak positions."
        ),
    ]
    assert records[0]["keywords"] == ["sidescraping", "sniping"]


def test_extractor_emits_progress_logs_for_pipeline_stages(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    output = tmp_path / "tanks_data.json"
    config = ExtractorConfig(
        wiki_endpoint="https://wiki.test/api.php",
        guide_root="https://guide.test/en/content/guide/",
        guide_paths=("newcomers-guide/getting_started/",),
        output_path=output,
        test_mode=True,
        request_interval=0,
    )
    with caplog.at_level("INFO"):
        run_extraction(
            config,
            schema_path=SCHEMA,
            client=FixtureClient(config),  # type: ignore[arg-type]
            sentence_nlp=Nlp(),
        )
    text = caplog.text
    assert "startup: interval=0.0s" in text
    assert "policy: checking robots.txt" in text
    assert "guides: GET" in text
    assert "wiki: crawling taxonomy" in text
    assert "wiki: 1/1 GET Tank:Tiger I" in text
    assert "assembly: merging 1 tanks" in text
    assert "output: published" in text

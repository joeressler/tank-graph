"""Non-interactive command-line entry point for the extractor."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import (
    DEFAULT_GUIDE_PATHS,
    DEFAULT_GUIDE_ROOT,
    DEFAULT_WIKI_ENDPOINT,
    LIVE_NATIONS,
    LIVE_ROOT_CATEGORY,
    ExtractorConfig,
)
from .errors import TankGraphError
from .extractor import run_extraction


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tank-graph-extract",
        description="Build deterministic World of Tanks records from approved sources.",
        epilog=(
            "Example:\n"
            "  tank-graph-extract extract "
            "--contact joe.a.ressler+tankgraph@gmail.com "
            "--output data/tanks_data.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    extract = subparsers.add_parser(
        "extract",
        help="collect, validate, and atomically publish the dataset",
        description=(
            "Collect the approved wiki and guide sources. This command never "
            "prompts and never downloads the spaCy model."
        ),
        epilog=(
            "Examples:\n"
            "  tank-graph-extract extract "
            "--contact joe.a.ressler+tankgraph@gmail.com\n"
            "  python -m tank_graph_extractor extract "
            "--contact joe.a.ressler+tankgraph@gmail.com "
            "--output data/tanks_data.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    extract.add_argument(
        "--contact",
        default=os.environ.get("TANK_GRAPH_CONTACT"),
        help="maintainer email or HTTPS project URL (or TANK_GRAPH_CONTACT)",
    )
    extract.add_argument(
        "--bot-version",
        default=__version__,
        help=f"version included in the User-Agent (default: {__version__})",
    )
    extract.add_argument("--wiki-endpoint", default=DEFAULT_WIKI_ENDPOINT)
    extract.add_argument("--guide-root", default=DEFAULT_GUIDE_ROOT)
    extract.add_argument(
        "--root-category",
        default=LIVE_ROOT_CATEGORY,
        help=(
            "taxonomy root; defaults to Category:USA Tanks as a temporary subset "
            "for Rust bootstrap. Pass Category:Tanks with --nation ALL for the full tree"
        ),
    )
    extract.add_argument(
        "--nation",
        action="append",
        dest="nations",
        help=(
            "keep only these canonical nations; default USA. Repeat to include more, "
            "or pass ALL to disable the temporary nation filter"
        ),
    )
    extract.add_argument(
        "--guide-path",
        action="append",
        dest="guide_paths",
        help="approved relative guide path; repeat to replace the default allowlist",
    )
    extract.add_argument(
        "--wiki-cookie",
        default=os.environ.get("WIKI_COOKIE"),
        help=(
            "full Cookie header from wiki.wargaming.net or wiki.worldoftanks.com "
            "after the article loads (or WIKI_COOKIE)"
        ),
    )
    extract.add_argument(
        "--bootstrap-wiki-cookies",
        action="store_true",
        help=(
            "open Chromium once, wait for #mw-content-text, export wiki-host cookies, "
            "then continue over HTTP"
        ),
    )
    extract.add_argument(
        "--request-interval",
        type=float,
        default=5.0,
        help="seconds between request attempts (production minimum 5.0)",
    )
    extract.add_argument(
        "--cookie-refresh-every",
        type=int,
        default=5,
        help=(
            "0 disables Playwright cookie recovery. Any positive value allows "
            "Playwright only when HTTP receives a wait-page interstitial, not "
            "on a request cadence"
        ),
    )
    extract.add_argument("--connect-timeout", type=float, default=10.0)
    extract.add_argument("--read-timeout", type=float, default=30.0)
    extract.add_argument("--max-response-bytes", type=int, default=10 * 1024 * 1024)
    extract.add_argument("--output", type=Path, default=Path("data/tanks_data.json"))
    extract.add_argument(
        "--schema",
        type=Path,
        default=Path("docs/specs/tanks-data.schema.json"),
        help="Draft 2020-12 interchange schema",
    )
    extract.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def _configuration_from_args(args: argparse.Namespace) -> ExtractorConfig:
    if not args.contact:
        raise ValueError(
            "missing maintainer contact; pass --contact <email-or-https-url> "
            "or set TANK_GRAPH_CONTACT"
        )
    from .wiki import resolve_nation_filter

    nations = resolve_nation_filter(args.nations if args.nations is not None else LIVE_NATIONS)
    return ExtractorConfig(
        wiki_endpoint=args.wiki_endpoint,
        guide_root=args.guide_root,
        root_category=args.root_category,
        user_agent=f"WoTGraphBot/{args.bot_version} (contact: {args.contact})",
        request_interval=args.request_interval,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_response_bytes=args.max_response_bytes,
        output_path=args.output,
        guide_paths=tuple(args.guide_paths or DEFAULT_GUIDE_PATHS),
        wiki_cookie=args.wiki_cookie or None,
        cookie_refresh_every=args.cookie_refresh_every,
        nations=nations,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        config = _configuration_from_args(args)
        if args.bootstrap_wiki_cookies:
            from .browser import export_wiki_cookies

            config = replace(config, wiki_cookie=export_wiki_cookies(config))
        summary = run_extraction(config, schema_path=args.schema)
    except (TankGraphError, ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")

    print(summary.to_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())

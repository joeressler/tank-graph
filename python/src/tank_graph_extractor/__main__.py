"""Non-interactive command-line entry point for the extractor."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import (
    DEFAULT_GUIDE_PATHS,
    DEFAULT_GUIDE_ROOT,
    DEFAULT_WIKI_ENDPOINT,
    PRODUCTION_REQUEST_INTERVAL,
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
    extract.add_argument("--root-category", default="Category:Tanks")
    extract.add_argument(
        "--guide-path",
        action="append",
        dest="guide_paths",
        help="approved relative guide path; repeat to replace the default allowlist",
    )
    extract.add_argument(
        "--request-interval",
        type=float,
        default=PRODUCTION_REQUEST_INTERVAL,
        help=(
            "minimum seconds between sequential request attempts "
            f"(production default and floor: {PRODUCTION_REQUEST_INTERVAL:.1f})"
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
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    try:
        config = _configuration_from_args(args)
        summary = run_extraction(config, schema_path=args.schema)
    except (TankGraphError, ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")

    print(summary.to_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())

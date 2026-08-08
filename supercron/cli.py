"""Command-line entry point that runs the supercron daemon and web UI.

Usage::

    supercron --cron-dir /cron --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence

from .config import ConfigError, read_config
from .scheduler import Daemon
from .server import Server

log = logging.getLogger("supercron")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supercron", description="Persistent-container cron daemon with web UI"
    )
    parser.add_argument(
        "--cron-dir",
        default=".",
        help="directory containing config.toml (default: current directory)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="web UI bind host")
    parser.add_argument("--port", type=int, default=8080, help="web UI bind port")
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="full debug logging (default is INFO)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = read_config(args.cron_dir)
    except ConfigError as exc:
        print(f"supercron: {exc}", file=sys.stderr)
        return 2

    daemon = Daemon(args.cron_dir, config=config)
    daemon.recover()
    daemon.refresh()
    daemon.start()

    server = Server(daemon, host=args.host, port=args.port)
    server.start()
    log.info("supercron ready; web UI at http://%s:%s", args.host, server.port)

    stop = threading.Event()

    def _shutdown(_signum: int, _frame: object) -> None:
        stop.set()
        log.info("shutting down")

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        server.stop()
        daemon.stop()
    return 0

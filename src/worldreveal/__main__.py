"""CLI entrypoint — `python -m worldreveal` or `worldreveal` (after `pip install -e .`)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .auth import get_credentials
from .config import Config
from .drive_client import DriveClient
from .logging_setup import configure_logging
from .monitor import Monitor
from .sheets_client import SheetsClient

log = logging.getLogger("worldreveal")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="worldreveal",
        description="Mirror YouTube videos listed in a Google Sheet into a Google Drive folder tree.",
    )
    p.add_argument(
        "--config",
        default="config.yaml",
        type=Path,
        help="Path to config.yaml (default: ./config.yaml)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single sweep and exit (useful for cron or manual testing).",
    )
    p.add_argument(
        "--auth-only",
        action="store_true",
        help="Run the OAuth flow, cache the token, and exit.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = Config.load(args.config)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    configure_logging(cfg.log_level, cfg.log_file)

    try:
        creds = get_credentials(cfg.credentials_dir)
    except FileNotFoundError as e:
        log.error(str(e))
        return 2
    except Exception:
        log.exception("OAuth failed.")
        return 2

    if args.auth_only:
        log.info("Auth complete; token cached.")
        return 0

    try:
        sheets = SheetsClient(creds, cfg.spreadsheet_id, cfg.columns)
        drive = DriveClient(creds, cfg.drive_root_folder_id)
    except Exception:
        log.exception("Failed to construct Google API clients.")
        return 1

    log.info("Connected to spreadsheet: %r", sheets.spreadsheet_title)

    monitor = Monitor(cfg, sheets, drive)
    monitor.install_signal_handlers()

    try:
        if args.once:
            monitor.sweep_once()
        else:
            monitor.run_forever()
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    except Exception:
        log.exception("Fatal error in monitor.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Config loading — YAML file with env-var overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ColumnNames:
    link: str = "Link"
    download_flag: str = "Download Video?"
    in_folder: str = "In Folder"
    drive_link: str = "Drive Link"
    team_number: str = "Team Number"


@dataclass
class Config:
    spreadsheet_id: str
    drive_root_folder_id: str
    skip_sheets: list[str] = field(default_factory=list)
    columns: ColumnNames = field(default_factory=ColumnNames)
    poll_interval_seconds: int = 300
    concurrency: int = 3
    download_dir: Path = Path("./downloads")
    log_level: str = "INFO"
    log_file: Path | None = Path("./logs/worldreveal.log")
    credentials_dir: Path = Path("./credentials")
    # Optional path to a Netscape-format cookies file. Required when running
    # from a datacenter IP (e.g. Hetzner) — YouTube bot-challenges unsigned
    # datacenter requests and demands cookies from a logged-in session.
    cookies_file: Path | None = None
    # Optional HTTP/HTTPS/SOCKS proxy URL for yt-dlp only. Use a residential
    # proxy on datacenter IPs to stop YouTube bot-flagging. Format examples:
    #   http://user:pass@host:port
    #   socks5://user:pass@host:port
    proxy: str | None = None

    @classmethod
    def load(cls, path: Path | str = "config.yaml") -> "Config":
        raw: dict = {}
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

        def env_override(key: str, default):
            return os.environ.get(f"WORLDREVEAL_{key.upper()}", default)

        spreadsheet_id = env_override("spreadsheet_id", raw.get("spreadsheet_id"))
        drive_root = env_override("drive_root_folder_id", raw.get("drive_root_folder_id"))
        if not spreadsheet_id or not drive_root:
            raise ValueError(
                "spreadsheet_id and drive_root_folder_id are required (in config.yaml or env)."
            )

        cols_raw = raw.get("columns") or {}
        columns = ColumnNames(
            link=cols_raw.get("link", "Link"),
            download_flag=cols_raw.get("download_flag", "Download Video?"),
            in_folder=cols_raw.get("in_folder", "In Folder"),
            drive_link=cols_raw.get("drive_link", "Drive Link"),
            team_number=cols_raw.get("team_number", "Team Number"),
        )

        log_file_raw = env_override("log_file", raw.get("log_file", "./logs/worldreveal.log"))
        log_file = Path(log_file_raw) if log_file_raw else None

        cookies_raw = env_override("cookies_file", raw.get("cookies_file"))
        cookies_file = Path(cookies_raw) if cookies_raw else None

        proxy_raw = env_override("proxy", raw.get("proxy"))
        proxy = str(proxy_raw).strip() if proxy_raw else None

        return cls(
            spreadsheet_id=spreadsheet_id,
            drive_root_folder_id=drive_root,
            skip_sheets=list(raw.get("skip_sheets") or []),
            columns=columns,
            poll_interval_seconds=int(env_override("poll_interval_seconds", raw.get("poll_interval_seconds", 300))),
            concurrency=int(env_override("concurrency", raw.get("concurrency", 3))),
            download_dir=Path(env_override("download_dir", raw.get("download_dir", "./downloads"))),
            log_level=str(env_override("log_level", raw.get("log_level", "INFO"))).upper(),
            log_file=log_file,
            credentials_dir=Path(env_override("credentials_dir", raw.get("credentials_dir", "./credentials"))),
            cookies_file=cookies_file,
            proxy=proxy,
        )

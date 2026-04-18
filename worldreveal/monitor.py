"""Main monitor loop — polls the sheet, dispatches downloads, uploads to Drive."""

from __future__ import annotations

import logging
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .drive_client import DriveClient
from .sheets_client import SheetRow, SheetTab, SheetsClient
from .youtube_client import (
    NotAVideoError,
    VideoInfo,
    download_video,
    fetch_video_info,
    looks_like_channel_or_playlist,
)

log = logging.getLogger(__name__)

# Drive / Finder / macOS reject these characters in filenames.
_FILENAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename_part(value: str, fallback: str = "") -> str:
    cleaned = _FILENAME_BAD.sub("_", value).strip().rstrip(".")
    return cleaned or fallback


def _build_drive_filename(team_number: str, title: str, video_id: str) -> str:
    """<teamNumber>_<youtubeVideoName>.mp4, with unsafe chars scrubbed."""
    team = _safe_filename_part(team_number)
    name = _safe_filename_part(title, fallback=video_id)
    stem = f"{team}_{name}" if team else name
    return f"{stem[:180]}.mp4"


@dataclass
class Job:
    tab: SheetTab
    row: SheetRow
    drive_folder_id: str


class Monitor:
    def __init__(
        self,
        cfg: Config,
        sheets: SheetsClient,
        drive: DriveClient,
    ):
        self._cfg = cfg
        self._sheets = sheets
        self._drive = drive
        self._stop_event = threading.Event()
        # Track video IDs in flight so parallel workers on the same sheet don't dupe.
        self._in_flight_lock = threading.Lock()
        self._in_flight: set[str] = set()

    # ---------------------------------------------------------------- lifecycle

    def install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.warning("Received signal %s; shutting down after current sweep.", signum)
            self._stop_event.set()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def run_forever(self) -> None:
        try:
            import yt_dlp  # lazy: avoid import cost at module load

            ytdlp_version = getattr(yt_dlp.version, "__version__", "unknown")
        except Exception:
            ytdlp_version = "unknown"
        log.info(
            "Starting monitor: poll every %ds, concurrency=%d, yt-dlp=%s, spreadsheet=%s",
            self._cfg.poll_interval_seconds,
            self._cfg.concurrency,
            ytdlp_version,
            self._cfg.spreadsheet_id,
        )
        if self._cfg.cookies_file:
            exists = Path(self._cfg.cookies_file).exists()
            log.info(
                "YouTube cookies: %s (%s)",
                self._cfg.cookies_file,
                "loaded" if exists else "MISSING — running without cookies",
            )
        else:
            log.info("YouTube cookies: not configured (fine on residential IPs; "
                     "datacenter IPs usually need cookies).")
        if self._cfg.proxy:
            # Don't log credentials if they're embedded in the URL.
            redacted = re.sub(r"://[^@/]+@", "://***@", self._cfg.proxy)
            log.info("yt-dlp proxy: %s", redacted)
        else:
            log.info("yt-dlp proxy: not configured (direct connection).")
        while not self._stop_event.is_set():
            start = time.monotonic()
            try:
                self.sweep_once()
            except Exception:
                log.exception("Sweep failed; will retry after interval.")
            elapsed = time.monotonic() - start
            remaining = max(0.0, self._cfg.poll_interval_seconds - elapsed)
            log.info("Sweep finished in %.1fs; next sweep in %.0fs.", elapsed, remaining)
            if self._stop_event.wait(remaining):
                break
        log.info("Monitor stopped.")

    # -------------------------------------------------------------- single pass

    def sweep_once(self) -> None:
        tab_titles = [
            t for t in self._sheets.list_tab_titles() if t not in self._cfg.skip_sheets
        ]
        log.info("Scanning %d tab(s): %s", len(tab_titles), ", ".join(tab_titles))

        jobs: list[Job] = []
        for title in tab_titles:
            try:
                tab = self._sheets.load_tab(title)
            except Exception:
                log.exception("Failed to load tab %r; skipping.", title)
                continue

            if not tab.rows:
                continue

            try:
                folder_id = self._drive.ensure_subfolder(title)
            except Exception:
                log.exception("Failed to ensure Drive subfolder for tab %r; skipping.", title)
                continue

            try:
                existing_by_video_id = self._drive.video_id_index(folder_id)
            except Exception:
                log.exception("Failed to list Drive folder for tab %r; skipping.", title)
                continue

            jobs.extend(self._plan_tab(tab, folder_id, existing_by_video_id))

        if not jobs:
            log.info("Nothing to do this sweep.")
            return

        log.info("Dispatching %d download job(s) with concurrency=%d.", len(jobs), self._cfg.concurrency)
        self._execute(jobs)

    # ---------------------------------------------------------------- planning

    def _plan_tab(
        self,
        tab: SheetTab,
        folder_id: str,
        existing_by_video_id: dict[str, dict],
    ) -> list[Job]:
        """Reconcile sheet rows against Drive state; return the list of rows to download."""
        reconcile: list[tuple[int, str]] = []  # (row_number, drive_link)
        jobs: list[Job] = []
        skipped_non_video = 0

        for row in tab.rows:
            url = row.link
            if not url:
                continue

            if looks_like_channel_or_playlist(url):
                skipped_non_video += 1
                log.debug("[%s!%d] Skipping channel/playlist URL: %s", tab.title, row.row_number, url)
                continue

            # We need the video ID to check Drive. Cheap path: regex it from the URL.
            vid = _extract_video_id(url)
            if vid and vid in existing_by_video_id:
                existing = existing_by_video_id[vid]
                link = existing.get("webViewLink") or _drive_view_link(existing["id"])
                if row.drive_link != link:
                    reconcile.append((row.row_number, link))
                continue

            if not row.download_flag:
                continue

            jobs.append(Job(tab=tab, row=row, drive_folder_id=folder_id))

        if reconcile:
            try:
                self._sheets.batch_update_drive_links(tab, reconcile)
            except Exception:
                log.exception("Failed to reconcile Drive links for tab %r.", tab.title)

        # Single-line per-tab summary at INFO. The per-row skip details are
        # available at DEBUG for anyone who needs them.
        if jobs or reconcile or skipped_non_video:
            log.info(
                "[%s] plan: %d to download, %d already in Drive, %d non-video skipped.",
                tab.title,
                len(jobs),
                len(reconcile),
                skipped_non_video,
            )

        return jobs

    # ---------------------------------------------------------------- execution

    def _execute(self, jobs: list[Job]) -> None:
        with ThreadPoolExecutor(max_workers=self._cfg.concurrency, thread_name_prefix="wr-worker") as ex:
            futures = {ex.submit(self._handle_job, j): j for j in jobs}
            for fut in as_completed(futures):
                job = futures[fut]
                try:
                    fut.result()
                except Exception:
                    log.exception("[%s!%d] Job failed.", job.tab.title, job.row.row_number)

    def _handle_job(self, job: Job) -> None:
        tab, row = job.tab, job.row
        tag = f"[{tab.title}!{row.row_number}]"

        # Resolve metadata first so we can early-dedupe by the authoritative video ID.
        try:
            info: VideoInfo = fetch_video_info(
                row.link,
                cookies_file=self._cfg.cookies_file,
                proxy=self._cfg.proxy,
            )
        except NotAVideoError as e:
            log.info("%s Skipping — %s", tag, e)
            return
        except Exception:
            log.exception("%s Failed to fetch metadata for %s", tag, row.link)
            return

        with self._in_flight_lock:
            if info.video_id in self._in_flight:
                log.info("%s %s already being processed by another worker; skipping.", tag, info.video_id)
                return
            self._in_flight.add(info.video_id)

        try:
            # Re-check Drive for this video ID — another sweep may have uploaded it.
            try:
                existing = self._drive.video_id_index(job.drive_folder_id).get(info.video_id)
            except Exception:
                log.exception("%s Failed to re-check Drive folder; proceeding to download.", tag)
                existing = None

            if existing:
                link = existing.get("webViewLink") or _drive_view_link(existing["id"])
                log.info("%s Already in Drive (%s); updating sheet only.", tag, existing["id"])
                self._safe_update_sheet(tab, row.row_number, link)
                return

            local_path: Path | None = None
            try:
                log.info("%s Downloading %r (%s)", tag, info.title, info.video_id)
                info, local_path = download_video(
                    row.link,
                    self._cfg.download_dir,
                    cookies_file=self._cfg.cookies_file,
                    proxy=self._cfg.proxy,
                )

                filename = _build_drive_filename(row.team_number, info.title, info.video_id)
                log.info("%s Uploading to Drive as %r", tag, filename)
                uploaded = self._drive.upload_video(
                    local_path=local_path,
                    folder_id=job.drive_folder_id,
                    drive_filename=filename,
                    youtube_video_id=info.video_id,
                )
                link = uploaded.get("webViewLink") or _drive_view_link(uploaded["id"])
                self._safe_update_sheet(tab, row.row_number, link)
                log.info("%s Done — %s", tag, link)
            finally:
                if local_path and local_path.exists():
                    try:
                        local_path.unlink()
                        log.debug("%s Removed local file %s", tag, local_path)
                    except Exception:
                        log.exception("%s Failed to remove local file %s", tag, local_path)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(info.video_id)

    def _safe_update_sheet(self, tab: SheetTab, row_number: int, drive_link: str) -> None:
        try:
            self._sheets.update_row_after_upload(tab, row_number, drive_link)
        except Exception:
            log.exception("Failed to update sheet %s row %d", tab.title, row_number)


# --------------------------------------------------------------------- helpers


_VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})"
)


def _extract_video_id(url: str) -> str | None:
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def _drive_view_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"

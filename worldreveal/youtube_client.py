"""yt-dlp wrapper — classify URLs, fetch metadata, download to MP4."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

log = logging.getLogger(__name__)


# URL patterns that unambiguously point at a single video.
_VIDEO_URL_PATTERNS = [
    re.compile(r"youtube\.com/watch\?", re.IGNORECASE),
    re.compile(r"youtu\.be/", re.IGNORECASE),
    re.compile(r"youtube\.com/shorts/", re.IGNORECASE),
    re.compile(r"youtube\.com/live/", re.IGNORECASE),
    re.compile(r"youtube\.com/embed/", re.IGNORECASE),
]

# Patterns that are definitively NOT a single video.
_NON_VIDEO_URL_PATTERNS = [
    re.compile(r"youtube\.com/(c/|channel/|user/|@)", re.IGNORECASE),
    re.compile(r"youtube\.com/playlist", re.IGNORECASE),
]


@dataclass
class VideoInfo:
    video_id: str
    title: str
    uploader: str | None
    duration_seconds: int | None
    webpage_url: str


class NotAVideoError(ValueError):
    """Raised when a URL points at a channel, playlist, or similar (not a single video)."""


def looks_like_channel_or_playlist(url: str) -> bool:
    """Fast path URL classifier — true if obviously not a single video."""
    for pat in _NON_VIDEO_URL_PATTERNS:
        if pat.search(url):
            return True
    return False


def looks_like_video(url: str) -> bool:
    for pat in _VIDEO_URL_PATTERNS:
        if pat.search(url):
            return True
    return False


def _ydl_opts_metadata() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }


def _ydl_opts_download(output_dir: Path) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # H.264 (avc1) video + AAC (mp4a) audio only — no VP9, no AV1, no Opus.
        # YouTube serves H.264 up to 1080p; higher resolutions are VP9/AV1 only,
        # so this effectively caps quality at 1080p (fine for reveal videos).
        # If none of these selectors match, the download fails loudly rather
        # than silently falling back to a different codec.
        "format": (
            "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]"
            "/bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]"
            "/best[vcodec^=avc1][acodec^=mp4a]"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "restrictfilenames": False,
        "overwrites": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "postprocessors": [
            # Remux-only (no re-encode). The format selector already guarantees
            # H.264/AAC streams, so ffmpeg just changes the container if needed.
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
        ],
    }


def fetch_video_info(url: str) -> VideoInfo:
    """Extract metadata without downloading. Raises NotAVideoError for channels/playlists."""
    if looks_like_channel_or_playlist(url):
        raise NotAVideoError(f"URL is a channel or playlist: {url}")

    with YoutubeDL(_ydl_opts_metadata()) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except DownloadError as e:
            raise NotAVideoError(f"yt-dlp could not resolve {url}: {e}") from e

    if not info:
        raise NotAVideoError(f"yt-dlp returned no info for {url}")

    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        raise NotAVideoError(f"URL resolved to a playlist/channel: {url}")

    video_id = info.get("id")
    title = info.get("title")
    if not video_id or not title:
        raise NotAVideoError(f"Missing id/title from yt-dlp for {url}")

    return VideoInfo(
        video_id=video_id,
        title=title,
        uploader=info.get("uploader"),
        duration_seconds=info.get("duration"),
        webpage_url=info.get("webpage_url") or url,
    )


def download_video(url: str, output_dir: Path) -> tuple[VideoInfo, Path]:
    """Download the video into output_dir. Returns (info, final local path)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    with YoutubeDL(_ydl_opts_download(output_dir)) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except DownloadError as e:
            raise RuntimeError(f"yt-dlp download failed for {url}: {e}") from e

    if not info:
        raise RuntimeError(f"yt-dlp returned no info for {url}")
    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        raise NotAVideoError(f"URL resolved to a playlist/channel: {url}")

    video_id = info["id"]
    # yt-dlp's requested_downloads is the authoritative path after postprocessing.
    candidates = [Path(rd["filepath"]) for rd in info.get("requested_downloads") or [] if rd.get("filepath")]
    if not candidates:
        # Fall back to the outtmpl pattern.
        candidates = list(output_dir.glob(f"{video_id}.*"))
    # Prefer .mp4 if both merged and intermediate files exist.
    mp4 = next((c for c in candidates if c.suffix.lower() == ".mp4" and c.exists()), None)
    local_path = mp4 or next((c for c in candidates if c.exists()), None)
    if local_path is None:
        raise RuntimeError(f"Could not locate downloaded file for {video_id} in {output_dir}")

    vi = VideoInfo(
        video_id=video_id,
        title=info.get("title") or video_id,
        uploader=info.get("uploader"),
        duration_seconds=info.get("duration"),
        webpage_url=info.get("webpage_url") or url,
    )
    log.info("Downloaded %s (%s) -> %s", vi.title, vi.video_id, local_path)
    return vi, local_path

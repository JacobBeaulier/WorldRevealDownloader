"""yt-dlp wrapper — classify URLs, fetch metadata, download to MP4 (H.264)."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
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


def _apply_cookies(opts: dict, cookies_file: Path | None) -> dict:
    """Attach a cookies file to a yt-dlp opts dict if one is configured and readable."""
    if cookies_file is None:
        return opts
    p = Path(cookies_file)
    if not p.exists():
        log.warning("cookies_file %s does not exist; running without cookies.", p)
        return opts
    opts["cookiefile"] = str(p)
    return opts


def _ydl_opts_metadata(cookies_file: Path | None = None) -> dict:
    return _apply_cookies(
        {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
        },
        cookies_file,
    )


def _ydl_opts_download(output_dir: Path, cookies_file: Path | None = None) -> dict:
    return _apply_cookies(
        {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Highest available quality regardless of codec. If the result is not
            # H.264/AAC, we transcode to H.264 MP4 after yt-dlp finishes. Preferring
            # H.264 first still avoids a transcode when YouTube offers it natively.
            "format": (
                "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]"
                "/bestvideo+bestaudio/best"
            ),
            "merge_output_format": "mp4",
            "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
            "restrictfilenames": False,
            "overwrites": True,
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "postprocessors": [
                # Remux-only: if the streams are MP4-compatible (H.264/AAC), ffmpeg
                # just swaps the container. Otherwise the file keeps its original
                # container (webm/mkv) and we re-encode in a dedicated pass below.
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
            ],
        },
        cookies_file,
    )


def fetch_video_info(url: str, cookies_file: Path | None = None) -> VideoInfo:
    """Extract metadata without downloading. Raises NotAVideoError for channels/playlists."""
    if looks_like_channel_or_playlist(url):
        raise NotAVideoError(f"URL is a channel or playlist: {url}")

    with YoutubeDL(_ydl_opts_metadata(cookies_file)) as ydl:
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


def download_video(
    url: str,
    output_dir: Path,
    cookies_file: Path | None = None,
) -> tuple[VideoInfo, Path]:
    """Download the video into output_dir. Returns (info, final local path)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    with YoutubeDL(_ydl_opts_download(output_dir, cookies_file)) as ydl:
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

    # If the pulled streams aren't H.264/AAC, re-encode to H.264 MP4 before
    # the file ever leaves the machine. Already-H.264 files are a no-op.
    local_path = _ensure_h264_mp4(local_path)
    return vi, local_path


# --------------------------------------------------------------- transcoding


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _probe_codecs(path: Path) -> tuple[str | None, str | None]:
    """Return (video_codec, audio_codec) for path, or (None, None) on probe failure."""
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not found on PATH (install ffmpeg).")

    def _codec(stream_selector: str) -> str | None:
        res = _run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", stream_selector,
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        if res.returncode != 0:
            return None
        out = res.stdout.strip()
        return out or None

    return _codec("v:0"), _codec("a:0")


def _ensure_h264_mp4(path: Path) -> Path:
    """Return path if it's already H.264 MP4; otherwise re-encode in place."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH (install ffmpeg).")

    vcodec, acodec = _probe_codecs(path)
    is_mp4 = path.suffix.lower() == ".mp4"

    if vcodec == "h264" and acodec == "aac" and is_mp4:
        log.debug("%s is already H.264/AAC MP4; no transcode needed.", path.name)
        return path

    # What action we'll take, for the log line:
    needs_video_reencode = vcodec != "h264"
    needs_audio_reencode = acodec not in {"aac", "mp4a"}
    action = []
    if needs_video_reencode:
        action.append(f"video {vcodec or '?'}→h264")
    else:
        action.append("video copy")
    if needs_audio_reencode:
        action.append(f"audio {acodec or '?'}→aac")
    else:
        action.append("audio copy")

    target = path.with_suffix(".h264.mp4")
    log.info("Transcoding %s (%s)", path.name, ", ".join(action))

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-stats",
        "-y",
        "-i", str(path),
        "-map", "0:v:0",
        "-map", "0:a:0?",  # optional: video may have no audio track
    ]
    if needs_video_reencode:
        cmd += [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
        ]
    else:
        cmd += ["-c:v", "copy"]
    if needs_audio_reencode:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c:a", "copy"]
    cmd += ["-movflags", "+faststart", str(target)]

    start = time.monotonic()
    res = _run(cmd)
    elapsed = time.monotonic() - start
    if res.returncode != 0:
        # Clean up partial output.
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise RuntimeError(
            f"ffmpeg transcode failed for {path.name} ({res.returncode}): {res.stderr.strip()[:500]}"
        )

    # Replace the original with the transcoded file.
    final = path.with_suffix(".mp4")
    try:
        path.unlink()
    except OSError:
        pass
    target.replace(final)
    log.info("Transcoded %s in %.1fs -> %s", path.name, elapsed, final.name)
    return final

"""Google Drive client — folder lookup/create, resumable upload, dedupe by video ID."""

from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path
from typing import Callable

import httplib2
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaFileUpload

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
YOUTUBE_ID_PROPERTY = "youtube_video_id"

# Errors that warrant a retry with exponential backoff.
_RETRIABLE_STATUS = {429, 500, 502, 503, 504}


def _with_retries(fn: Callable, *, attempts: int = 5, base_delay: float = 2.0):
    """Call fn() with exponential backoff on transient Drive errors."""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status not in _RETRIABLE_STATUS:
                raise
            last_err = e
        except (TimeoutError, ConnectionError) as e:
            last_err = e
        delay = base_delay * (2**i) + random.uniform(0, 1)
        log.warning("Drive call failed (attempt %d/%d), retrying in %.1fs: %s", i + 1, attempts, delay, last_err)
        time.sleep(delay)
    assert last_err is not None
    raise last_err


class DriveClient:
    def __init__(self, creds: Credentials, root_folder_id: str):
        # googleapiclient's default Http is not thread-safe. Give every request its own
        # AuthorizedHttp so parallel workers don't race on a shared connection.
        self._creds = creds

        def _build_request(_http, *args, **kwargs):
            return HttpRequest(AuthorizedHttp(creds, http=httplib2.Http()), *args, **kwargs)

        self._svc = build(
            "drive",
            "v3",
            credentials=creds,
            requestBuilder=_build_request,
            cache_discovery=False,
        )
        self._root_folder_id = root_folder_id
        self._folder_cache: dict[str, str] = {}  # folder name -> id, under root
        self._folder_cache_lock = threading.Lock()

    # ------------------------------------------------------------------ folders

    def ensure_subfolder(self, name: str) -> str:
        """Return the ID of <root>/<name>, creating it if missing."""
        with self._folder_cache_lock:
            cached = self._folder_cache.get(name)
        if cached:
            return cached

        q = (
            f"'{self._root_folder_id}' in parents "
            f"and name = '{_escape(name)}' "
            f"and mimeType = '{FOLDER_MIME}' "
            "and trashed = false"
        )

        def _list():
            return self._svc.files().list(
                q=q,
                fields="files(id,name)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()

        res = _with_retries(_list)
        files = res.get("files", [])
        if files:
            folder_id = files[0]["id"]
            log.debug("Found Drive subfolder %r -> %s", name, folder_id)
        else:
            def _create():
                return self._svc.files().create(
                    body={
                        "name": name,
                        "mimeType": FOLDER_MIME,
                        "parents": [self._root_folder_id],
                    },
                    fields="id",
                    supportsAllDrives=True,
                ).execute()

            created = _with_retries(_create)
            folder_id = created["id"]
            log.info("Created Drive subfolder %r -> %s", name, folder_id)

        with self._folder_cache_lock:
            self._folder_cache[name] = folder_id
        return folder_id

    # -------------------------------------------------------------------- files

    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        """Return every non-trashed file in folder (id, name, appProperties)."""
        files: list[dict] = []
        page_token: str | None = None
        while True:
            def _list(token=page_token):
                return self._svc.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id,name,appProperties,webViewLink,mimeType)",
                    pageSize=200,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()

            res = _with_retries(_list)
            files.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return files

    def video_id_index(self, folder_id: str) -> dict[str, dict]:
        """Return {youtube_video_id: file_record} for files tagged with our appProperty."""
        out: dict[str, dict] = {}
        for f in self.list_files_in_folder(folder_id):
            vid = (f.get("appProperties") or {}).get(YOUTUBE_ID_PROPERTY)
            if vid:
                out[vid] = f
        return out

    def upload_video(
        self,
        local_path: Path,
        folder_id: str,
        drive_filename: str,
        youtube_video_id: str,
    ) -> dict:
        """Upload local_path into folder, tagging it with the YouTube video ID."""
        media = MediaFileUpload(
            str(local_path),
            mimetype="video/mp4",
            resumable=True,
            chunksize=8 * 1024 * 1024,
        )
        body = {
            "name": drive_filename,
            "parents": [folder_id],
            "appProperties": {YOUTUBE_ID_PROPERTY: youtube_video_id},
        }

        def _start():
            return self._svc.files().create(
                body=body,
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )

        request = _start()
        response = None
        last_progress = -1
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    if pct // 10 != last_progress // 10:
                        log.info("Uploading %s: %d%%", drive_filename, pct)
                        last_progress = pct
            except HttpError as e:
                status_code = getattr(e.resp, "status", None)
                if status_code not in _RETRIABLE_STATUS:
                    raise
                log.warning("Resumable upload hiccup (%s); sleeping before retry.", status_code)
                time.sleep(5)
        log.info("Uploaded %s -> %s", drive_filename, response.get("webViewLink"))
        return response


def _escape(s: str) -> str:
    """Escape a value for use inside single-quoted Drive query strings."""
    return s.replace("\\", "\\\\").replace("'", "\\'")

# WorldReveal Downloader

Monitors a Google Sheet, downloads YouTube videos listed in it, and uploads each
one to a Google Drive sub-folder that mirrors the sheet's tab name.

- One tab per Drive sub-folder.
- Download state is determined by **actual presence in Drive** (not a checkbox).
  Files are tagged with the YouTube video ID in Drive's `appProperties`, so the
  tool can recognize its own uploads across runs.
- When a video is present in Drive, the sheet's `Drive Link` column is updated
  with a link and `In Folder` is ticked.
- URLs that resolve to channels or playlists are logged and skipped.
- Monitor loop runs on an interval; downloads + uploads run 3-way concurrently.

## Requirements

- macOS / Linux, Python 3.10+
- `ffmpeg` on your `PATH` (required by yt-dlp for merging video+audio into MP4).
  On macOS: `brew install ffmpeg`.

## Setup

```bash
# 1. Clone and install.
git clone <this repo>
cd WorldRevealDownloader
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Create your config.
cp config.example.yaml config.yaml
# Edit config.yaml if you want to change defaults.

# 3. Create a Google Cloud OAuth client:
#    - https://console.cloud.google.com/apis/credentials
#    - Create OAuth client ID -> Application type: "Desktop app".
#    - Enable APIs: Google Sheets API, Google Drive API.
#    - Download the client JSON and save it as:
#         credentials/oauth_client.json
#
# 4. One-time auth (opens browser, caches token to credentials/token.json):
worldreveal --auth-only
```

## Running

```bash
# Long-running monitor (default: polls every 5 min, Ctrl-C to stop).
worldreveal

# One-shot sweep (useful for cron / testing).
worldreveal --once

# Alternative if you didn't pip install -e:
python -m worldreveal --once
```

## How it works

Per sweep, for each non-skipped tab:

1. Load all rows.
2. Ensure the Drive sub-folder `<root>/<tab name>` exists (create if not).
3. List the sub-folder; build a `{youtube_video_id -> file}` index using the
   `appProperties.youtube_video_id` tag set at upload time.
4. For each row:
   - If the URL looks like a channel/playlist, log and skip.
   - If the video already exists in Drive, update `Drive Link` and tick
     `In Folder` (idempotent reconcile).
   - Otherwise, if `Download Video?` is checked, queue a job.
5. Jobs are executed by a thread pool with `concurrency` workers. Each worker:
   - Fetches metadata via `yt-dlp`.
   - Re-checks Drive for the video ID (a concurrent sweep may have beaten it).
   - Downloads the highest-quality MP4 to `download_dir`.
   - Uploads to Drive with `appProperties.youtube_video_id` set.
   - Writes the Drive link back to the sheet and ticks `In Folder`.
   - Deletes the local file.

## Configuration

Everything lives in `config.yaml`; any field can also be overridden by the env
var `WORLDREVEAL_<UPPER_KEY>` (e.g. `WORLDREVEAL_POLL_INTERVAL_SECONDS=60`).

| Key | Default | Notes |
| --- | --- | --- |
| `spreadsheet_id` | — (required) | ID from the Google Sheet URL |
| `drive_root_folder_id` | — (required) | ID of the Drive folder that holds the per-tab sub-folders |
| `skip_sheets` | `[]` | Tab titles to ignore entirely |
| `columns.link` | `Link` | Header of the URL column |
| `columns.download_flag` | `Download Video?` | Header of the "download me" checkbox |
| `columns.in_folder` | `In Folder` | Header of the status checkbox (updated after upload) |
| `columns.drive_link` | `Drive Link` | Created automatically if missing |
| `poll_interval_seconds` | `300` | Time between sweeps |
| `concurrency` | `3` | Max concurrent download+upload pipelines |
| `download_dir` | `./downloads` | Scratch dir; files deleted after upload |
| `log_level` | `INFO` | `DEBUG` for more verbose output |
| `log_file` | `./logs/worldreveal.log` | Rotated at 10 MB, 5 backups |

## Running as a background service on macOS (optional)

If you want it to start at login, use `launchd`. Create
`~/Library/LaunchAgents/com.worldreveal.downloader.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.worldreveal.downloader</string>
  <key>WorkingDirectory</key><string>/Users/YOU/path/to/WorldRevealDownloader</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/path/to/WorldRevealDownloader/.venv/bin/worldreveal</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOU/path/to/WorldRevealDownloader/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/path/to/WorldRevealDownloader/logs/launchd.err.log</string>
</dict>
</plist>
```

Then:

```bash
launchctl load ~/Library/LaunchAgents/com.worldreveal.downloader.plist
```

## Troubleshooting

- **`ffmpeg not found`** — install via Homebrew: `brew install ffmpeg`.
- **OAuth loop / 403** — make sure Sheets + Drive APIs are enabled on the same
  GCP project as your OAuth client, and that your Google account is added as a
  test user while the consent screen is in "Testing" mode.
- **Tab missing columns** — the tool logs a warning and skips that tab. Add
  `Link` and `Download Video?` headers to row 1 and re-run. `Drive Link` is
  created for you automatically.
- **A wrong-filename file was uploaded manually** — the tool matches by the
  `youtube_video_id` appProperty, not filename. Manually-placed files without
  that tag will be treated as absent and re-downloaded. Tag them via the Drive
  API or delete and let the tool re-upload.

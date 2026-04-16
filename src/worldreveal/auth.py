"""OAuth helper — loads/refreshes user credentials for Sheets + Drive.

On first run, automatically opens a browser to Google's consent page, captures
the callback on a local loopback port, and caches the resulting token. No
user-visible command other than starting the script is required — except for
the one-time OAuth client creation in Google Cloud Console (unavoidable; Google
requires every app to identify itself).
"""

from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CLIENT_SECRETS_FILENAME = "oauth_client.json"
TOKEN_FILENAME = "token.json"


class OAuthClientMissingError(FileNotFoundError):
    """Raised when credentials/oauth_client.json hasn't been placed yet."""


def _print_first_time_setup(client_secrets_path: Path) -> None:
    msg = textwrap.dedent(
        f"""
        ============================================================
         First-time setup required (one time, ~3 minutes)
        ============================================================
        This tool needs an OAuth "client" identity registered with
        Google before it can open a sign-in window for you. Do this
        once, then never again:

          1. Go to  https://console.cloud.google.com/projectcreate
             and create a project (any name, e.g. "worldreveal").

          2. Enable these two APIs on that project:
             - https://console.cloud.google.com/apis/library/sheets.googleapis.com
             - https://console.cloud.google.com/apis/library/drive.googleapis.com

          3. Configure the consent screen:
             https://console.cloud.google.com/apis/credentials/consent
             - User type: External
             - Add your Google account as a "Test user"

          4. Create an OAuth client ID:
             https://console.cloud.google.com/apis/credentials
             - "Create Credentials" > "OAuth client ID"
             - Application type: **Desktop app**
             - Download the JSON, then save it as:

                 {client_secrets_path}

          5. Re-run this command. A browser will open automatically,
             you approve once, and you're done.
        ============================================================
        """
    ).strip()
    print(msg, file=sys.stderr)


def get_credentials(credentials_dir: Path) -> Credentials:
    """Return valid OAuth credentials, launching the browser flow on first run."""
    credentials_dir = Path(credentials_dir)
    credentials_dir.mkdir(parents=True, exist_ok=True)

    token_path = credentials_dir / TOKEN_FILENAME
    client_secrets_path = credentials_dir / CLIENT_SECRETS_FILENAME

    creds: Credentials | None = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:
            log.exception("Failed to load cached token; will re-authenticate.")
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            log.info("Refreshing expired OAuth token.")
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:
            log.exception("Token refresh failed; falling back to full auth flow.")

    if not client_secrets_path.exists():
        _print_first_time_setup(client_secrets_path)
        raise OAuthClientMissingError(
            f"OAuth client secrets not found at {client_secrets_path}."
        )

    log.info(
        "No cached token; opening your browser for Google sign-in. "
        "After you approve, this window will continue automatically."
    )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        authorization_prompt_message="",
        success_message=(
            "Sign-in complete. You can close this browser tab and return to the terminal."
        ),
    )
    token_path.write_text(creds.to_json(), encoding="utf-8")
    log.info("Saved OAuth token to %s — future runs will skip the sign-in step.", token_path)
    return creds

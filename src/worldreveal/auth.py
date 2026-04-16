"""OAuth helper — loads/refreshes user credentials for Sheets + Drive."""

from __future__ import annotations

import logging
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


def get_credentials(credentials_dir: Path) -> Credentials:
    """Return valid OAuth credentials, prompting the user on first run."""
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
        raise FileNotFoundError(
            f"OAuth client secrets not found at {client_secrets_path}. "
            "Download an 'OAuth 2.0 Client IDs' JSON (Desktop app) from Google Cloud Console "
            "and save it there."
        )

    log.info("Launching interactive OAuth flow (a browser window will open).")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    log.info("Saved new OAuth token to %s", token_path)
    return creds

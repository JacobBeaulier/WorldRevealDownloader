"""Google Sheets client — read rows, ensure columns exist, write updates."""

from __future__ import annotations

import logging
import string
import threading
from dataclasses import dataclass
from typing import Iterable

import gspread
from google.oauth2.credentials import Credentials

from .config import ColumnNames

log = logging.getLogger(__name__)


@dataclass
class SheetRow:
    """One row from a tab, pre-parsed."""

    sheet_title: str
    row_number: int  # 1-indexed; header is row 1, data starts at 2
    link: str
    download_flag: bool
    in_folder: bool
    drive_link: str


@dataclass
class SheetTab:
    title: str
    header: list[str]
    rows: list[SheetRow]
    column_index: dict[str, int]  # header name -> 1-indexed column


def _column_letter(col_index_1based: int) -> str:
    """Convert 1-indexed column number to A1 letters (1 -> A, 27 -> AA)."""
    letters = ""
    n = col_index_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = string.ascii_uppercase[rem] + letters
    return letters


def _to_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    s = str(val).strip().lower()
    return s in {"true", "yes", "y", "1", "checked", "x"}


class SheetsClient:
    def __init__(self, creds: Credentials, spreadsheet_id: str, columns: ColumnNames):
        self._gc = gspread.authorize(creds)
        self._spreadsheet = self._gc.open_by_key(spreadsheet_id)
        self._columns = columns
        # gspread's underlying http client isn't thread-safe for mutations; serialize writes.
        self._write_lock = threading.Lock()

    @property
    def spreadsheet_title(self) -> str:
        return self._spreadsheet.title

    def list_tab_titles(self) -> list[str]:
        return [ws.title for ws in self._spreadsheet.worksheets()]

    def load_tab(self, title: str) -> SheetTab:
        """Read all data for a tab and ensure the drive_link column exists."""
        ws = self._spreadsheet.worksheet(title)
        all_values = ws.get_all_values()
        if not all_values:
            return SheetTab(title=title, header=[], rows=[], column_index={})

        header = [h.strip() for h in all_values[0]]

        # Ensure "Drive Link" column exists; create it as a new trailing column if not.
        if self._columns.drive_link not in header:
            with self._write_lock:
                new_col_idx = len(header) + 1
                ws.update_cell(1, new_col_idx, self._columns.drive_link)
                log.info(
                    "Added missing column %r to tab %r at col %d",
                    self._columns.drive_link,
                    title,
                    new_col_idx,
                )
            header.append(self._columns.drive_link)
            for row in all_values[1:]:
                row.append("")

        column_index = {name: i + 1 for i, name in enumerate(header)}

        required = [self._columns.link, self._columns.download_flag]
        missing = [c for c in required if c not in column_index]
        if missing:
            log.warning("Tab %r is missing required columns %s; skipping.", title, missing)
            return SheetTab(title=title, header=header, rows=[], column_index=column_index)

        rows: list[SheetRow] = []
        for i, raw in enumerate(all_values[1:], start=2):
            # Pad short rows so index lookups don't blow up.
            padded = list(raw) + [""] * (len(header) - len(raw))
            link = padded[column_index[self._columns.link] - 1].strip()
            if not link:
                continue
            download_flag = _to_bool(padded[column_index[self._columns.download_flag] - 1])
            in_folder = _to_bool(
                padded[column_index[self._columns.in_folder] - 1]
                if self._columns.in_folder in column_index
                else ""
            )
            drive_link = padded[column_index[self._columns.drive_link] - 1].strip()
            rows.append(
                SheetRow(
                    sheet_title=title,
                    row_number=i,
                    link=link,
                    download_flag=download_flag,
                    in_folder=in_folder,
                    drive_link=drive_link,
                )
            )
        return SheetTab(title=title, header=header, rows=rows, column_index=column_index)

    def update_row_after_upload(
        self,
        tab: SheetTab,
        row_number: int,
        drive_link: str,
    ) -> None:
        """Write the drive_link and tick the in_folder checkbox, in a single batch."""
        ws = self._spreadsheet.worksheet(tab.title)
        updates: list[dict] = []

        drive_col = tab.column_index.get(self._columns.drive_link)
        if drive_col:
            updates.append(
                {
                    "range": f"{_column_letter(drive_col)}{row_number}",
                    "values": [[drive_link]],
                }
            )

        in_folder_col = tab.column_index.get(self._columns.in_folder)
        if in_folder_col:
            updates.append(
                {
                    "range": f"{_column_letter(in_folder_col)}{row_number}",
                    "values": [[True]],
                }
            )

        if not updates:
            return

        with self._write_lock:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
        log.info("Updated %s!row %d with Drive link.", tab.title, row_number)

    def batch_update_drive_links(
        self,
        tab: SheetTab,
        updates: Iterable[tuple[int, str]],
    ) -> None:
        """Batched helper used to reconcile existing Drive files to sheet rows."""
        drive_col = tab.column_index.get(self._columns.drive_link)
        in_folder_col = tab.column_index.get(self._columns.in_folder)
        if not drive_col:
            return

        update_list = list(updates)
        if not update_list:
            return

        payload: list[dict] = []
        for row_number, link in update_list:
            payload.append(
                {
                    "range": f"{_column_letter(drive_col)}{row_number}",
                    "values": [[link]],
                }
            )
            if in_folder_col:
                payload.append(
                    {
                        "range": f"{_column_letter(in_folder_col)}{row_number}",
                        "values": [[True]],
                    }
                )

        ws = self._spreadsheet.worksheet(tab.title)
        with self._write_lock:
            ws.batch_update(payload, value_input_option="USER_ENTERED")
        log.info("Reconciled %d existing Drive file(s) to tab %r.", len(update_list), tab.title)

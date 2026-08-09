"""Ingest layer — turn a data source into one normalized `Book`.

`load_book()` is the swappable seam: `CHAI_SOURCE=fixture` runs fully offline;
`CHAI_SOURCE=schwab` pulls the live read-only book, with automatic degraded-mode
fallback to the last snapshot if the live pull fails.
"""
from __future__ import annotations

import config

from .fixture import load_fixture_book
from .models import Balances, Book, OptionPos, StockPos
from .schwab_source import (
    book_from_account_json,
    load_book_from_schwab,
    load_book_from_snapshot,
    load_index_quotes,
)

__all__ = [
    "Balances", "Book", "OptionPos", "StockPos",
    "load_book", "load_fixture_book", "load_book_from_schwab",
    "load_book_from_snapshot", "book_from_account_json", "load_index_quotes",
]


def load_book(source: str | None = None) -> Book:
    """Load the normalized Book from the configured source.

    source: "fixture" | "schwab" (defaults to config.CHAI_SOURCE).
    In "schwab" mode a failed live pull falls back to the last saved snapshot,
    and only if that is missing does it surface the error.
    """
    src = (source or config.CHAI_SOURCE).strip().lower()
    if src == "schwab":
        try:
            return load_book_from_schwab()
        except Exception as exc:  # degraded-mode fallback
            try:
                book = load_book_from_snapshot()
                print(f"[chai] live pull failed ({exc}); using last snapshot.")
                return book
            except Exception:
                raise exc
    return load_fixture_book()

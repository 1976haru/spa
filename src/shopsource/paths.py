from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "shopsource.sqlite3"
EXPORT_DIR = PROJECT_ROOT / "exports"
STORE_DIR = PROJECT_ROOT / "stores"
SOURCE_DIR = PROJECT_ROOT / "source"
AMAZON_SOURCE_DIR = SOURCE_DIR / "amazon"
AMAZON_INBOX_DIR = AMAZON_SOURCE_DIR / "inbox"
AMAZON_SAMPLES_DIR = AMAZON_SOURCE_DIR / "samples_or_docs"


def ensure_dirs() -> None:
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    AMAZON_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    AMAZON_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
